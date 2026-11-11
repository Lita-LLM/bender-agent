"""Канал «Колонки»: Бендер — голосовой сервер колонок с Pulsar.

Колонка сама ловит слово вызова и чистит звук. Бендер получает фразу, распознаёт её Ушами
агента, отдаёт агенту как реплику хозяина в канале speaker, озвучивает итог Голосом агента
и отдаёт колонке ссылку на звук. Первым Бендер говорит через «объявление».
"""
from __future__ import annotations

import asyncio
import json
import re
import time

from ..core.errors import HumanError
from ..tools.registry import ToolContext
from . import esphome as api
from .listen import Phrase, spoken
from .media import Media, local_address
from .stream import Streamer

PROGRESS = 20.0  # колонка ждёт событий не дольше 120 с, напоминаем о себе чаще
RECENT = 600.0   # подтверждения вслух — только колонке, с которой говорили последние 10 минут

PHRASES = {'да всегда': 'always', 'всегда': 'always', 'да в разговоре': 'task', 'в разговоре': 'task', 'в этом разговоре': 'task'}
YES = {'да', 'подтверждаю', 'разрешаю', 'давай', 'ок', 'окей', 'конечно', 'можно'}
NO = {'нет', 'отмена', 'отмени', 'запрещаю', 'не', 'стоп', 'нельзя'}
CHOICES = {'once': 'Разрешил.', 'task': 'Разрешил в этом разговоре.', 'always': 'Разрешил всегда.', 'deny': 'Отказал.'}


def failed(title, hint='Связи → Колонки.'):
    return HumanError('speaker', title, hint)


def stations(cfg) -> list[dict]:
    """Проверенный список колонок из настройки: id, имя, адрес, порт, агент."""
    out, seen = [], set()
    rows = cfg.get('stations') or []
    for _ in range(8):  # значение, испорченное прежним пультом: JSON внутри строки
        if not isinstance(rows, str):
            break
        try:
            rows = json.loads(rows)
        except ValueError:
            rows = []
    for index, row in enumerate(rows if isinstance(rows, list) else []):
        if not isinstance(row, dict):
            continue
        host = str(row.get('host') or '').strip()
        if not host or len(host) > 253 or any(c.isspace() or c in '/@?#' for c in host):
            continue
        try:
            port = int(row.get('port') or 6053)
        except (TypeError, ValueError):
            continue
        if not 0 < port < 65536:
            continue
        name = str(row.get('name') or host)[:60]
        sid = re.sub(r'[^\w-]+', '-', str(row.get('id') or name).lower()).strip('-')[:40] or f'speaker{index + 1}'
        while sid in seen:
            sid += '-2'
        seen.add(sid)
        agent = str(row.get('agent_id') or cfg.get('agent_id') or 'main')
        out.append({'id': sid, 'name': name, 'host': host, 'port': port, 'agent_id': agent})
    return out


def quiet(cfg, now=None) -> bool:
    start, end = cfg['quiet_from'], cfg['quiet_to']
    if start == end:
        return False
    hour = (now or time.localtime()).tm_hour
    return start <= hour < end if start < end else hour >= start or hour < end


def choice(text: str, can_remember: bool = False):
    """Ответ на подтверждение голосом: once / task / always / deny или None, если не понял."""
    words = re.sub(r'[^\w\s]', ' ', text.lower()).split()
    joined = ' '.join(words)
    for phrase, value in PHRASES.items():
        if joined == phrase or joined.startswith(phrase + ' ') or joined.endswith(' ' + phrase):
            return value if value != 'always' or can_remember else 'once'
    if words and words[0] in YES:
        return 'once'
    if words and words[0] in NO:
        return 'deny'
    return None


def follow(cfg, speech: str) -> bool:
    mode = cfg['followup']
    return mode == 'always' or mode == 'question' and speech.rstrip().endswith('?')


# Пометка хранится в самой реплике: история после записи не меняется, кэш модели цел.
HEARD = 'распознано с голоса, возможны ошибки; ответ — вслух, всё словами, без цифр и значков'


class Station:
    """Одна колонка: соединение, текущий разговор и ожидание ответа агента."""

    def __init__(self, owner: 'Speakers', cfg: dict):
        self.owner, self.cfg = owner, cfg
        self.link: api.Link | None = None
        self.state, self.notice, self.info = 'off', '', {}
        self.task = None
        self.talk = None          # задача текущего разговора
        self.phrase: Phrase | None = None
        self.heard: asyncio.Event | None = None
        self.waiting = None       # (session, future) пока колонка ждёт ответ агента
        self.sid = None           # разговор, куда колонка отдаёт фразы (главный или свой)
        self.card = None          # карточка Предохранителя, которую колонка спросила вслух
        self.last = 0.0
        self.timing = {}  # секунды этапов последнего ответа: где колонка ждёт
        self.handover = None  # переданный агент — со следующей фразы: текущий ход ещё ждёт прежнего
        self.agent = owner.answering().get(cfg['id'])  # передача разговора переживает переподключение и перезапуск
        self.light_key = None  # ключ света колонки (кольцо), если она его показала
        self.fallback = None  # кто отвечает вместо выбранного, пока тот временно недоступен; выбор при этом не теряется

    @property
    def id(self):
        return self.cfg['id']

    @property
    def agent_id(self):
        """Кто отвечает сейчас: запасной (если выбранный недоступен), переданный разговор (hand_over) или агент колонки."""
        return self.fallback or self.agent or self.cfg['agent_id']

    def public(self):
        return {**self.cfg, 'state': self.state, 'notice': self.notice, 'device': self.info,
                'busy': bool(self.talk and not self.talk.done()), 'last': self.last, 'timing': self.timing,
                'answering': self.handover or self.agent_id, 'chosen': self.agent or self.cfg['agent_id'], 'fallback': self.fallback}

    def start(self):
        self.task = asyncio.ensure_future(self.run())

    async def fetched(self, url, wait=10.0):
        """Колонка не пришла за звуком — чаще всего порт раздачи закрыт брандмауэром компьютера с Бендером."""
        await asyncio.sleep(wait)
        if not self.owner.media.was_served(url):
            self.fail('колонка не забрала звук ответа', failed(
                'Колонка не скачала ' + url.rsplit('/', 1)[0] + '/…',
                'Открой этот порт в брандмауэре компьютера с Бендером (например, sudo ufw allow ' + str(self.owner.media.port) + '/tcp) или смени «Адрес раздачи звука» в настройках колонок.'))

    def fail(self, what, exc):
        """Ошибка разговора: колонка проигрывает только звук ошибки, а причина — здесь, в Бортжурнале."""
        self.notice = what + ': ' + str(exc)
        self.reported = None
        self.report('Колонка «' + self.cfg['name'] + '»: ' + self.notice)
        self.owner.event()

    def report(self, text):
        """В Бортжурнал — только смена состояния, не каждая попытка переподключения."""
        if text == getattr(self, 'reported', None):
            return
        self.reported = text
        journal = getattr(self.owner.app, 'journal', None)
        if journal is not None:
            journal.write('channel', text, data={'speaker': self.id, 'state': self.state})

    async def stop(self):
        for task in (self.talk, self.task):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (self.talk, self.task) if t), return_exceptions=True)
        if self.link:
            await self.link.close()
        self.link, self.state = None, 'off'

    async def run(self):
        pause = 5.0
        while True:
            link = api.Link(self.cfg['host'], self.cfg['port'], client='Bender')
            try:
                self.state, self.notice = 'connecting', ''
                self.owner.event()
                self.info = await link.open()
                await self.owner.serve(self.cfg['host'])
                self.owner.selected(self)  # знать свой разговор и после перезапуска Бендера
                self.link = link
                # flags=1: звук по этому же соединению, без UDP
                await link.send(api.SUBSCRIBE_VA_REQ, api.encode((1, True), (2, 1)))
                if self.owner.settings().get('light'):
                    await link.send(api.LIST_ENTITIES_REQ)
                self.state, pause = 'ok', 5.0
                self.report('Колонка «' + self.cfg['name'] + '» подключена: ' + (self.info.get('model') or self.info.get('name') or 'Pulsar'))
                self.owner.event()
                async for kind, fields in link.frames():
                    await self.on_frame(kind, fields)
                self.state, self.notice = 'error', 'Колонка закрыла соединение; переподключаюсь.'
            except asyncio.CancelledError:
                raise
            except HumanError as exc:
                self.state, self.notice = 'error', str(exc)
            except Exception as exc:  # чужой протокол не должен ронять Бендера
                self.state, self.notice = 'error', f'Сбой связи с колонкой: {exc.__class__.__name__}'
            finally:
                self.link = None
                if self.talk and not self.talk.done():
                    self.talk.cancel()
                await link.close()
            self.report('Колонка «' + self.cfg['name'] + '»: ' + self.notice)
            self.owner.event()
            await asyncio.sleep(pause)
            pause = min(60.0, pause * 2)

    COLORS = {'listen': (0.1, 0.3, 1.0), 'think': (1.0, 0.7, 0.0), 'speak': (0.1, 0.9, 0.3), 'off': None}

    async def glow(self, state):
        """Превью: кольцо показывает, что делает агент. Любой сбой света не должен мешать разговору."""
        if self.light_key is None or self.link is None or not self.owner.settings().get('light'):
            return
        try:
            await self.link.send(api.LIGHT_COMMAND, api.light_command(self.light_key, self.COLORS[state]))
        except Exception:  # noqa: BLE001
            pass

    async def interrupt(self):
        """«Стоп» посреди ответа: обрываем разговор колонки и идущий ход агента."""
        if not (self.talk and not self.talk.done()):
            return False
        self.talk.cancel()
        if self.sid:
            try:
                self.owner.app.agents.get(self.agent_id).mind.stop(self.sid)
            except Exception:  # noqa: BLE001
                pass
        self.report('Колонка «' + self.cfg['name'] + '»: остановлена по слову «стоп»')
        await self.glow('off')
        return True

    async def on_frame(self, kind, fields):
        if kind == api.LIST_LIGHT:
            if self.light_key is None:
                self.light_key = (fields.get(2) or [None])[-1]
            return
        if kind == api.VA_REQUEST:
            if not api.number(fields, 1) and not self.heard and self.owner.settings().get('stop_cancels'):
                if await self.interrupt():
                    return
            if api.number(fields, 1):
                if self.talk and not self.talk.done():
                    self.talk.cancel()  # новое «Алиса» перебивает прежний разговор
                self.talk = asyncio.ensure_future(self.converse(api.text(fields, 5)))
            elif self.heard:
                self.heard.set()  # колонка сама закончила слушать (кнопка, стоп-слово)
        elif kind == api.VA_AUDIO and self.phrase is not None:
            chunk = (fields.get(1) or [b''])[-1]
            if not isinstance(chunk, bytes):
                return
            result = self.phrase.feed(chunk)
            if result == 'speech':
                await self.link.event(api.STT_VAD_START)
            elif result in ('end', 'limit', 'silence') and self.heard:
                self.heard.set()

    async def converse(self, wake: str):
        link, cfg = self.link, self.owner.settings()
        self.last = time.time()
        await link.send(api.VA_RESPONSE, api.encode((1, 0)))
        flow = stream = streamer = None
        speaking = False
        if cfg.get('stream', True):
            try:
                flow, stream = self.owner.media.stream('wav')  # ссылка заранее: колонка откроет её по знаку
            except HumanError:
                pass
        await link.event(api.RUN_START, url=flow)
        await self.glow('listen')
        await link.event(api.STT_START)
        self.phrase, self.heard = Phrase(silence_ms=cfg['silence_ms'], limit_s=cfg['listen_seconds']), asyncio.Event()
        try:
            try:
                await asyncio.wait_for(self.heard.wait(), cfg['listen_seconds'] + 5)
            except asyncio.TimeoutError:
                pass
            phrase, self.phrase, self.heard = self.phrase, None, None
            await link.event(api.STT_VAD_END)
            if not phrase.speaking:
                await link.event(api.ERROR, code='stt-no-text-recognized', message='Ничего не услышал')
                return
            ctx, sid = self.owner.selected(self)
            mark = time.monotonic()
            timing = {'heard_seconds': round(phrase.seconds(), 1)}

            def lap(name):
                nonlocal mark
                now = time.monotonic()
                timing[name] = round(now - mark, 2)
                mark = now
            try:
                heard = await ctx.voice.transcribe(phrase.wav(), ext='wav', seconds=round(phrase.seconds(), 2), session_id=sid, from_owner=True)
            except HumanError as exc:
                self.fail('Уши (распознавание) не сработали', exc)
                await link.event(api.ERROR, code='stt-provider-error', message=str(exc)[:200])
                return
            text = heard['text'].strip()
            lap('stt')
            heard_at = mark
            await link.event(api.STT_END, text=text)
            await self.glow('think')
            await link.event(api.INTENT_START)
            if stream is not None:
                streamer = Streamer(ctx, sid, stream, lambda: link.event(api.INTENT_PROGRESS, tts_start_streaming='1'), lambda: self.owner.allowed(ctx, sid))
            answer = self.decide(ctx, text) if self.card else None
            if answer is None:
                answer = await self.ask(ctx, sid, text, cfg)
            lap('model')
            said = await streamer.finish(answer) if streamer else ''
            if answer is None:
                await link.event(api.INTENT_END, speech='')
                return
            if streamer and streamer.started:
                speech, url = said, flow  # уже звучит по предложениям; TTS_END — та же ссылка
                timing['first_audio'] = round(streamer.first - heard_at, 2)
            else:
                speech = spoken(answer)
                url = await self.owner.voice(ctx, sid, speech)
            lap('tts')
            timing['total'] = round(sum(timing[k] for k in ('stt', 'model', 'tts')), 2)
            self.timing = timing
            ctx.journal.write('channel', 'Колонка «' + self.cfg['name'] + '»: ответ через ' + str(timing['total']) + ' с — распознавание ' + str(timing['stt'])
                              + ', модель ' + str(timing['model']) + ', озвучка ' + str(timing['tts'])
                              + (', первый звук через ' + str(timing['first_audio']) + ' с после распознавания' if 'first_audio' in timing else ''),
                              data={'speaker': self.id, 'timing': timing})
            cont = bool(self.card) or follow(cfg, speech) and not self.owner.later.get(sid)
            await link.event(api.INTENT_END, speech=speech, continue_conversation='1' if cont else '0')
            await self.glow('speak')
            speaking = True
            asyncio.get_running_loop().call_later(min(30.0, 2 + len(speech) * 0.08), lambda: asyncio.ensure_future(self.glow('off')))  # колонка играет сама: гасим по длине речи
            await link.event(api.TTS_START, text=speech)
            await link.event(api.TTS_END, url=url)
            asyncio.ensure_future(self.fetched(url))
        except HumanError as exc:
            self.fail('ответ не озвучен', exc)
            await link.event(api.ERROR, code='bender-error', message=str(exc)[:200])
        finally:
            self.phrase = self.heard = None
            if not speaking:
                asyncio.ensure_future(self.glow('off'))
            if streamer:
                streamer.cancel()
            elif stream is not None:
                stream.close()
            self.waiting = None
            try:
                await link.event(api.RUN_END)
            except (HumanError, OSError, ConnectionError):
                pass
            self.owner.event()

    def decide(self, ctx, text):
        """Фраза — ответ на подтверждение, спрошенное вслух. None — карточки уже нет."""
        pid, card = self.card
        item = ctx.guard.pending.get(pid)
        if not item:
            self.card = None
            return None
        value = choice(text, bool(card.get('can_remember')))
        if value is None:
            return 'Не понял. Скажи «да», «да в разговоре» или «нет».'
        self.card = None
        done = ctx.guard.respond(pid, value, by='owner')
        ctx.journal.write('guard', 'Колонка «' + self.cfg['name'] + '»: ответ голосом на подтверждение — ' + value, actor='owner', data={'id': pid, 'speaker': self.id, 'heard': text[:200]})
        return CHOICES[value] if done else 'Уже решено.'

    def ask_aloud(self, card):
        """Спросить подтверждение вслух. Если колонка ждёт ответ агента — это и будет её ответ."""
        summary = str(card.get('summary') or card.get('what') or 'действие')
        question = 'Предохранитель спрашивает: ' + summary.rstrip('.') + '. Скажи «да», ' + ('«да в разговоре» ' if card.get('can_session') else '') + 'или «нет».'
        self.card = (str(card['id']), card)
        waiting = self.waiting
        if waiting and not waiting[1].done():
            self.owner.later[waiting[0]] = True  # итог хода придёт объявлением после ответа
            waiting[1].set_result(question)
            return None
        return question

    async def ask(self, ctx, sid, text, cfg):
        """Реплика агенту и ожидание его ответа, пока колонка готова ждать."""
        waiter = asyncio.get_running_loop().create_future()
        self.waiting = (sid, waiter)
        self.owner.later.pop(sid, None)
        # Адрес разговора (home_channel) колонка не меняет: в общем разговоре Telegram и дальше
        # получает уведомления, а ответ на эту фразу колонка забирает сама — она его ждёт.
        if cfg['home'] and (ctx.store.session(sid) or {}).get('meta', {}).get('home_channel') != 'speaker':
            # колонка — последний канал хозяина: то, что агент скажет сам, прозвучит здесь
            ctx.store.update_session(sid, meta={'home_channel': 'speaker'})
        await ctx.mind.say(sid, '[Колонка «' + self.cfg['name'] + '»: ' + HEARD + '] ' + text, channel='speaker', actor='owner', message_meta={'speaker': self.id}, wait=False,
                           model_role=cfg['model_role'], effort=None if cfg['effort'] == 'default' else cfg['effort'])
        started = time.monotonic()
        while True:
            try:
                return await asyncio.wait_for(asyncio.shield(waiter), PROGRESS)
            except asyncio.TimeoutError:
                if time.monotonic() - started >= cfg['wait_seconds']:
                    self.owner.later[sid] = True  # итог придёт объявлением
                    return 'Это займёт время. Скажу, когда закончу.'
                await self.link.event(api.INTENT_PROGRESS)

    async def announce(self, ctx, sid, text, *, title=''):
        cfg = self.owner.settings()
        if self.link is None or self.state != 'ok':
            raise failed('Колонка «' + self.cfg['name'] + '» не подключена', 'Проверь её в Связях → Колонки.')
        speech = spoken(text)
        if not speech:
            return
        url = await self.owner.voice(ctx, sid, speech)
        await self.link.send(api.VA_ANNOUNCE_REQ, api.encode((1, url), (2, speech), (4, bool(self.card) or follow(cfg, speech))))
        self.last = time.time()


class Speakers:
    def __init__(self, app):
        self.app = app.host
        self.items: dict[str, Station] = {}
        self.media = Media()
        self.later: dict[str, bool] = {}
        self.runtime = False
        self.restarting = None
        self.app.bus.listen(self.on_event)

    def settings(self):
        return self.app.settings.group('speakers')

    def event(self):
        self.app.bus.publish({'type': 'channel', 'channel': 'speaker', 'agent_id': '*'})

    def public(self):
        cfg = self.settings()
        return {'settings': cfg, 'media': {'address': self.media.address, 'port': self.media.port} if self.media.server else None,
                'stations': [self.items[s['id']].public() if s['id'] in self.items else {**s, 'state': 'off', 'notice': ''} for s in stations(cfg)]}

    # ---------------------------------------------------------------- жизненный цикл

    def start(self):
        self.runtime = True
        if getattr(self.app, '_closed', False) or self.app.safe or not self.settings()['enabled']:
            return
        for cfg in stations(self.settings()):
            if cfg['id'] not in self.items:
                station = self.items[cfg['id']] = Station(self, cfg)
                station.start()
        self.event()

    GONE = ('agent_missing', 'agent_archived', 'agent_temporary_done')

    def unavailable(self, agent_id):
        """None — агент доступен. 'gone' — удалён или в архиве: выбор отпадает насовсем. 'later' — временно
        (стая выключена, сбой загрузки): выбор хозяина сохраняется, отвечает запасной."""
        try:
            self.app.agents.get(agent_id)
            return None
        except HumanError as error:
            return 'gone' if error.code in self.GONE else 'later'
        except Exception:  # noqa: BLE001
            return 'later'

    def alive(self, agent_id):
        return self.unavailable(agent_id) is None

    def substitute(self, station, chosen):
        """Главный — только когда никто из выбранного не доступен: сперва агент колонки, потом агент по умолчанию."""
        for candidate in dict.fromkeys((station.cfg['agent_id'], self.settings().get('agent_id') or 'main', 'main')):
            if candidate != chosen and self.alive(candidate):
                return candidate
        return 'main'

    def reconfigure(self):
        """Настройки поменялись. Переподключаются только колонки, у которых сменились адрес или порт:
        остальное (модель, усилие, продолжение разговора) читается на каждой фразе, и разговор не рвётся."""
        if getattr(self.app, '_closed', False) or not self.runtime:
            return
        if self.restarting and not self.restarting.done():
            return
        cfg = self.settings()
        media = (cfg['media_address'], cfg['media_port'])
        if getattr(self, 'media_cfg', media) != media:
            self.media_cfg = media

            async def restart():
                await self.stop()
                self.start()
        else:
            self.media_cfg = media

            async def restart():
                wanted = {c['id']: c for c in stations(cfg)} if cfg['enabled'] and not getattr(self.app, 'safe', False) else {}
                for sid, station in list(self.items.items()):
                    new = wanted.get(sid)
                    if new is None or (new['host'], new['port']) != (station.cfg['host'], station.cfg['port']):
                        del self.items[sid]
                        await station.stop()
                        continue
                    if new['agent_id'] != station.cfg['agent_id']:
                        station.agent = station.handover = None  # хозяин сам назначил колонке агента
                        self.remember(station)
                    station.cfg = new
                if not self.items:
                    await self.media.stop()
                self.start()
                self.event()
        try:
            self.restarting = asyncio.get_running_loop().create_task(restart(), name='speakers-reconfigure')
        except RuntimeError:
            pass

    async def stop(self):
        items, self.items = list(self.items.values()), {}
        await asyncio.gather(*(s.stop() for s in items), return_exceptions=True)
        await self.media.stop()
        self.event()

    async def close(self):
        self.runtime = False
        if self.restarting and self.restarting is not asyncio.current_task():
            self.restarting.cancel()
            await asyncio.gather(self.restarting, return_exceptions=True)
        await self.stop()

    async def serve(self, peer):
        await self.media.start(self.settings()['media_address'] or local_address(peer), self.settings()['media_port'])

    # ---------------------------------------------------------------- разговоры

    def answering(self):
        """Кому колонки переданы (hand_over): {колонка: агент}."""
        store = getattr(self.app, 'store', None)
        return dict(store.meta_get('speakers:answering', {}) or {}) if store else {}

    def remember(self, station):
        store = getattr(self.app, 'store', None)
        if store:
            items = self.answering()
            if station.agent:
                items[station.id] = station.agent
            else:
                items.pop(station.id, None)
            store.meta_set('speakers:answering', items)

    def selected(self, station: Station):
        if station.handover:
            station.agent, station.handover = station.handover, None
            if station.agent == station.cfg['agent_id']:
                station.agent = None
            self.remember(station)
        chosen = station.agent or station.cfg['agent_id']
        problem = self.unavailable(chosen)
        if problem == 'gone' and station.agent:
            station.agent = None  # агента отправили в архив или удалили — колонка возвращается своему
            self.remember(station)
            chosen = station.cfg['agent_id']
            problem = self.unavailable(chosen)
        previous, station.fallback = station.fallback, None
        if problem:
            station.fallback = self.substitute(station, chosen)
            if previous != station.fallback:
                journal = getattr(self.app, 'journal', None)
                if journal:
                    journal.write('speaker', f"Колонка «{station.cfg['name']}»: выбранный агент {chosen} сейчас недоступен, отвечает {station.fallback}. Выбор сохранён.", data={'station': station.id, 'chosen': chosen, 'answering': station.fallback})
        ctx = self.app.agents.get(station.agent_id)
        if self.settings()['session'] == 'main':
            sid = ctx.main_session()  # одна жизнь: колонка продолжает главный разговор
        else:
            key = 'speaker:session:' + station.id
            sid = ctx.store.meta_get(key)
            if not sid or not ctx.store.session(sid):
                sid = ctx.store.new_session('chat', 'Колонка · ' + station.cfg['name'], meta={'home_channel': 'speaker', 'speaker': station.id})
                ctx.store.meta_set(key, sid)
        station.sid = sid
        return ctx, sid

    def awaiting(self, ctx, sid):
        """Колонка, которая прямо сейчас ждёт ответа в этом разговоре."""
        for station in self.items.values():
            waiting = station.waiting
            if station.agent_id == ctx.agent_id and waiting and waiting[0] == sid and not waiting[1].done():
                return station
        return None

    def station_of(self, ctx, sid):
        station = self.awaiting(ctx, sid)
        if station:
            return station
        for station in self.items.values():
            if station.agent_id == ctx.agent_id and station.sid == sid:
                return station
        return None

    async def allowed(self, ctx, sid):
        await ToolContext(ctx, actor='system', session_id=sid, tool='speaker_say').check('msg.send', 'speaker', 'сказать вслух на колонке', to_owner=True)

    async def voice(self, ctx, sid, speech):
        """Озвучить Голосом агента и выдать колонке ссылку на звук."""
        await self.allowed(ctx, sid)
        last = None
        for fmt in dict.fromkeys(('wav', ctx.settings.get('voice', 'format', 'wav'))):
            try:
                result = await ctx.voice.speak(speech, session_id=sid, format=fmt)
                return self.media.put(result['audio'], 'ogg' if fmt == 'opus' else fmt)
            except HumanError as exc:
                last = exc
        raise last

    async def say(self, ctx, text):
        """send_message агента из разговора колонки: сейчас же вслух."""
        station = self.station_of(ctx.app, ctx.session_id)
        if station is None:
            raise failed('Этот разговор не привязан к колонке')
        waiting = station.waiting
        if waiting and waiting[0] == ctx.session_id and not waiting[1].done():
            # колонка ещё ждёт: это и будет её ответ, а итог хода придёт объявлением
            self.later[ctx.session_id] = True
            waiting[1].set_result(text)
            return
        await station.announce(ctx.app, ctx.session_id, text)

    def on_event(self, event):
        kind = event.get('type')
        if kind == 'answer' and self.items:
            asyncio.ensure_future(self.answered(event))
        elif kind == 'notify' and self.items:
            asyncio.ensure_future(self.notified(event))
        elif kind == 'approval' and self.items and self.settings()['approvals'] == 'voice':
            asyncio.ensure_future(self.approval(event.get('card') or {}))
        elif kind == 'approval_done':
            for station in self.items.values():
                if station.card and station.card[0] == str(event.get('id')):
                    station.card = None

    async def approval(self, card):
        """Карточка Предохранителя из разговора колонки — спросить вслух у той колонки, что рядом."""
        if not card.get('id'):
            return
        for station in list(self.items.values()):
            if station.agent_id != card.get('agent_id', 'main') or station.sid != card.get('session') or station.state != 'ok':
                continue
            if not station.waiting and time.time() - station.last > RECENT:
                continue
            question = station.ask_aloud(card)
            if question:
                try:
                    ctx = self.app.agents.get(station.agent_id)
                    await station.announce(ctx, station.sid, question)
                except HumanError as exc:
                    station.fail('подтверждение не спрошено вслух', exc)
            return

    async def answered(self, event):
        result = event.get('result') or {}
        sid = event.get('session')
        for station in list(self.items.values()):
            ctx = self.app.agents.get(station.agent_id)
            if station.sid != sid or event.get('agent_id', ctx.agent_id) != ctx.agent_id:
                continue
            text = result.get('text') or ''
            waiting = station.waiting
            if waiting and waiting[0] == sid and not waiting[1].done():
                waiting[1].set_result(None if result.get('sent_all') else text)
                return
            if result.get('sent_all') or not text.strip():
                self.later.pop(sid, None)
                return
            own = event.get('source') == 'speaker'
            if own and self.later.pop(sid, None):
                await self.speak_or_forward(station, ctx, sid, text, label='Итог')  # долгий ход колонки
            elif not own and event.get('channel') == 'speaker':
                await self.speak_or_forward(station, ctx, sid, text, label='Агент')  # агент заговорил сам
            return

    async def speak_or_forward(self, station, ctx, sid, text, *, label, alarm=False):
        """Сказать из колонки; ночью или без связи — не терять, а отправить в Telegram."""
        busy = station.talk and not station.talk.done()
        if station.state == 'ok' and not busy and (alarm or not quiet(self.settings())):
            try:
                await station.announce(ctx, sid, text)
                return
            except HumanError as exc:
                station.fail(label.lower() + ' не озвучен', exc)
        elif busy:
            # колонка занята разговором: скажем, когда освободится
            await asyncio.gather(station.talk, return_exceptions=True)
            return await self.speak_or_forward(station, ctx, sid, text, label=label, alarm=alarm)
        self.app.bus.publish({'type': 'notify', 'kind': 'speaker', 'agent_id': ctx.agent_id, 'channel': 'telegram',
                              'text': label + ' (колонка «' + station.cfg['name'] + '» сейчас молчит):\n' + text})

    async def notified(self, event):
        cfg = self.settings()
        alarm = event.get('kind') == 'alarm'
        if event.get('channel') == 'speaker':
            # уведомление разговора, чей последний канал — колонка: туда, а не по общему правилу
            agent = event.get('agent_id', 'main')
            for station in list(self.items.values()):
                if station.agent_id == agent and station.sid:
                    ctx = self.app.agents.get(agent)
                    text = ' '.join(str(event.get(k) or '') for k in ('title', 'text')).strip()
                    if text:
                        await self.speak_or_forward(station, ctx, station.sid, text, label='Уведомление', alarm=alarm)
                    return
            return
        if cfg['announce'] == 'off' or cfg['announce'] == 'alarms' and not alarm:
            return
        if quiet(cfg) and not alarm or event.get('kind') == 'speaker':
            return  # 'speaker' — пересланное колонкой в Telegram, обратно не озвучиваем
        text = ' '.join(str(event.get(k) or '') for k in ('title', 'text')).strip()
        if not text:
            return
        for station in list(self.items.values()):
            if station.state != 'ok' or station.talk and not station.talk.done():
                continue
            try:
                ctx, sid = self.selected(station)
                await station.announce(ctx, sid, text)
            except HumanError as exc:
                station.notice = 'Уведомление не озвучено: ' + str(exc)
                self.event()
