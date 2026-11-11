"""Колонки: протокол ESPHome, конец фразы и полный разговор с поддельной колонкой Pulsar."""
import array
import asyncio
import json
import math
import time
import unittest
import urllib.request

from bender.core.errors import HumanError
from bender.speakers import esphome as api
from bender.speakers.channel import Speakers, Station, follow, quiet, stations
from bender.speakers.listen import Phrase, spoken
from bender.speakers.media import Media

CFG = {'enabled': True, 'stations': [], 'agent_id': 'main', 'listen_seconds': 15, 'silence_ms': 600, 'wait_seconds': 60,
       'followup': 'question', 'announce': 'alarms', 'approvals': 'voice', 'session': 'main', 'home': True, 'model_role': 'fast', 'effort': 'low', 'quiet_from': 23, 'quiet_to': 8, 'media_address': '127.0.0.1', 'media_port': 0, 'stream': False}


def pcm(seconds, amplitude=0, freq=440):
    n = int(16000 * seconds)
    return array.array('h', (int(amplitude * math.sin(2 * math.pi * freq * i / 16000)) for i in range(n))).tobytes()


class CodecTest(unittest.TestCase):
    def test_round_trip(self):
        raw = api.encode((1, True), (2, 300), (3, 'привет'), (4, -1), (5, [(1, 'k'), (2, 'v')]), (6, 1.5))
        fields = api.decode(raw)
        self.assertEqual(fields[1], [1])
        self.assertEqual(fields[2], [300])
        self.assertEqual(api.text(fields, 3), 'привет')
        self.assertEqual(fields[4], [(1 << 64) - 1])
        inner = api.decode(fields[5][0])
        self.assertEqual((api.text(inner, 1), api.text(inner, 2)), ('k', 'v'))

    def test_frame_and_broken(self):
        self.assertEqual(api.frame(7), b'\x00\x00\x07')
        with self.assertRaises(HumanError):
            api.decode(b'\x0a\x05ab')

    def test_noise_frame_is_explained(self):
        async def run():
            reader = asyncio.StreamReader()
            reader.feed_data(b'\x01\x00\x00')
            reader.feed_eof()
            with self.assertRaises(HumanError) as caught:
                await api.read_frame(reader)
            self.assertIn('шифрования', str(caught.exception))
        asyncio.run(run())

    def test_posix_tz(self):
        self.assertRegex(api.posix_tz(), r'^<[+-]\d{2,4}>[+-]\d+(:\d\d)?$')


class LinkAliveTest(unittest.TestCase):
    """Колонка, перезагруженная по питанию, молчит без FIN: Бендер должен это заметить и переподключиться."""

    def run_link(self, answers_ping):
        async def main():
            pings = []

            async def handle(reader, writer):
                try:
                    while True:
                        kind, _ = await api.read_frame(reader)
                        if kind == api.HELLO_REQ:
                            writer.write(api.frame(api.HELLO_RESP, api.encode((1, 1), (2, 10))))
                        elif kind == api.DEVICE_INFO_REQ:
                            writer.write(api.frame(api.DEVICE_INFO_RESP, api.encode((2, 'lite2'))))
                        elif kind == api.PING_REQ:
                            pings.append(1)
                            if answers_ping:
                                writer.write(api.frame(api.PING_RESP))
                        await writer.drain()
                except (asyncio.IncompleteReadError, ConnectionError):
                    pass
            server = await asyncio.start_server(handle, '127.0.0.1', 0)
            link = api.Link('127.0.0.1', server.sockets[0].getsockname()[1])
            link.idle = 0.1
            await link.open()
            frames = link.frames()
            try:
                await asyncio.wait_for(frames.__anext__(), 1.0)
                ended = False
            except StopAsyncIteration:
                ended = True
            except asyncio.TimeoutError:
                ended = False  # жива: проверки проходят, кадров нет
            await link.close()
            server.close()
            return ended, len(pings)
        return asyncio.run(main())

    def test_silent_station_is_dropped(self):
        ended, pings = self.run_link(False)
        self.assertTrue(ended)
        self.assertEqual(pings, 1)

    def test_answering_station_stays(self):
        ended, pings = self.run_link(True)
        self.assertFalse(ended)
        self.assertGreater(pings, 3)


class PhraseTest(unittest.TestCase):
    def test_speech_then_silence(self):
        phrase = Phrase(silence_ms=600)
        events = [phrase.feed(chunk) for chunk in (pcm(0.5, 20), pcm(1.0, 6000), pcm(1.0, 20))]
        self.assertIn('speech', events)
        self.assertEqual(events[-1], 'end')
        self.assertTrue(phrase.wav().startswith(b'RIFF'))
        self.assertAlmostEqual(phrase.seconds(), 2.5, places=1)

    def test_nothing_said(self):
        phrase = Phrase(wait_s=2)
        self.assertEqual(phrase.feed(pcm(3, 20)), 'silence')
        self.assertFalse(phrase.speaking)

    def test_speech_right_after_wake_word(self):
        phrase = Phrase(silence_ms=600)
        self.assertEqual(phrase.feed(pcm(0.2, 6000)), 'speech')
        self.assertEqual(phrase.feed(pcm(1.0, 6000) + pcm(1.0, 20)), 'end')

    def test_quiet_room_then_speech(self):
        phrase = Phrase(silence_ms=600)
        self.assertIsNone(phrase.feed(pcm(1.0, 150)))
        self.assertEqual(phrase.feed(pcm(0.5, 5000)), 'speech')
        self.assertEqual(phrase.feed(pcm(1.0, 150)), 'end')

    def test_limit(self):
        phrase = Phrase(limit_s=5)
        self.assertEqual([r for r in (phrase.feed(pcm(1, 6000)) for _ in range(6)) if r][-1], 'limit')


class TextTest(unittest.TestCase):
    def test_spoken_strips_markup(self):
        text = spoken('## Итог\n**Готово**: смотри [сайт](https://x.y).\n- пункт\n```py\nprint(1)\n```')
        for bad in ('#', '*', 'https', '```', '- '):
            self.assertNotIn(bad, text)
        self.assertIn('Готово', text)
        self.assertIn('код — в пульте', text)
        self.assertTrue(spoken('Слово. ' * 400).endswith('Остальное — в пульте.'))

    def test_spoken_drops_emoji_and_symbols(self):
        text = spoken('Готово ✅🎉 Сегодня +5°C → завтра ☀️ теплее. Задача №3, 2+2=4, рейтинг ★★★☆☆.')
        self.assertEqual(text, 'Готово Сегодня +5 градусов завтра теплее. Задача номер 3, 2+2 равно 4, рейтинг.')

    def test_settings_helpers(self):
        rows = stations({'agent_id': 'coder', 'stations': [{'name': 'Кухня', 'host': '192.0.2.5'}, {'host': 'bad host'}, 'x',
                                                            {'name': 'Кухня', 'host': 'a.local', 'port': 99999}, {'id': 'k', 'host': 'b.local', 'agent_id': 'main'}]})
        self.assertEqual([(r['host'], r['port'], r['agent_id']) for r in rows], [('192.0.2.5', 6053, 'coder'), ('b.local', 6053, 'main')])
        self.assertTrue(quiet(CFG, time.struct_time((2026, 1, 1, 2, 0, 0, 0, 1, 0))))
        self.assertFalse(quiet(CFG, time.struct_time((2026, 1, 1, 12, 0, 0, 0, 1, 0))))
        self.assertTrue(follow(CFG, 'Включить?'))
        self.assertFalse(follow(CFG, 'Готово.'))


class BrokenFieldTest(unittest.TestCase):
    def test_stations_from_double_encoded_text(self):
        rows = [{'name': 'Кухня', 'host': '192.0.2.40'}]
        broken = json.dumps(json.dumps(json.dumps(rows)))
        self.assertEqual([r['host'] for r in stations({'stations': broken})], ['192.0.2.40'])
        self.assertEqual(stations({'stations': '"мусор'}), [])

    def test_json_setting_text_becomes_value(self):
        from bender.core.settings import SCHEMA, _coerce
        field = SCHEMA['speakers'].field('stations')
        rows = [{'name': 'Кухня', 'host': '192.0.2.40'}]
        self.assertEqual(_coerce(field, json.dumps(rows)), rows)
        self.assertEqual(_coerce(field, json.dumps(json.dumps(rows))), rows)
        self.assertEqual(_coerce(field, rows), rows)
        with self.assertRaises(HumanError):
            _coerce(field, '[{"host": }]')


class LiveAppTest(unittest.IsolatedAsyncioTestCase):
    async def test_enabling_on_the_fly_starts_channel(self):
        import tempfile
        from pathlib import Path
        from bender.core.app import App
        with tempfile.TemporaryDirectory() as tmp:
            app = App(Path(tmp) / 'data')
            app.bus.bind_loop(asyncio.get_running_loop())
            try:
                app._speakers_ready = True  # так делает server_main после запуска каналов
                # из пульта приходит текст JSON — ядро хранит список
                app.settings.set('speakers', {'stations': json.dumps([{'name': 'Кухня', 'host': '127.0.0.1', 'port': 9}]), 'enabled': True}, by='owner')
                self.assertIsInstance(app.settings.get('speakers', 'stations'), list)
                self.assertEqual(list(app.speakers.items), ['кухня'])
            finally:
                await app.close()


class ReconfigureTest(unittest.IsolatedAsyncioTestCase):
    async def test_settings_change_keeps_conversation_and_handover(self):
        import tempfile
        from pathlib import Path
        from bender.core.app import App
        with tempfile.TemporaryDirectory() as tmp:
            app = App(Path(tmp) / 'data')
            app.bus.bind_loop(asyncio.get_running_loop())
            try:
                app._speakers_ready = True
                app.agents.create('Шерлок', agent_id='sherlock', awaken=False)
                app.settings.set('speakers', {'stations': [{'name': 'Кухня', 'host': '127.0.0.1', 'port': 9}], 'enabled': True}, by='owner')
                await asyncio.sleep(0)
                station = app.speakers.items['кухня']
                station.handover = 'sherlock'
                app.speakers.selected(station)
                # правка модели колонки — не повод рвать связь и забывать переданного агента
                app.settings.set('speakers', {'model_role': 'fast', 'effort': 'low'}, by='owner')
                await app.speakers.restarting
                self.assertIs(app.speakers.items['кухня'], station)
                self.assertEqual(station.agent_id, 'sherlock')
                # новый адрес — колонка переподключается, а передача остаётся (хранится в базе)
                app.settings.set('speakers', {'stations': [{'name': 'Кухня', 'host': '127.0.0.2', 'port': 9}]}, by='owner')
                await app.speakers.restarting
                fresh = app.speakers.items['кухня']
                self.assertIsNot(fresh, station)
                self.assertEqual(fresh.agent_id, 'sherlock')
                # хозяин сам назначил колонке агента в настройках — передача снимается
                app.settings.set('speakers', {'stations': [{'name': 'Кухня', 'host': '127.0.0.2', 'port': 9, 'agent_id': 'sherlock'}]}, by='owner')
                await app.speakers.restarting
                self.assertIsNone(fresh.agent)
                self.assertEqual(fresh.cfg['agent_id'], 'sherlock')
                self.assertEqual(app.store.meta_get('speakers:answering'), {})
            finally:
                await app.close()


class SharedConversationTest(unittest.IsolatedAsyncioTestCase):
    async def test_send_message_goes_to_waiting_speaker(self):
        import tempfile
        from pathlib import Path
        from bender.core.app import App
        from bender.tools.registry import ToolContext
        with tempfile.TemporaryDirectory() as tmp:
            app = App(Path(tmp) / 'data')
            app.bus.bind_loop(asyncio.get_running_loop())
            try:
                app.settings.set('guard', {'enabled': False})
                station = Station(app.speakers, {'id': 'кухня', 'name': 'Кухня', 'host': '127.0.0.1', 'port': 9, 'agent_id': 'main'})
                app.speakers.items['кухня'] = station
                ctx, sid = app.speakers.selected(station)
                self.assertEqual(sid, app.main_session())  # по умолчанию — общий разговор
                waiter = asyncio.get_running_loop().create_future()
                station.waiting = (sid, waiter)
                result = await app.tools.run('send_message', {'text': 'Займусь.'}, ToolContext(app, session_id=sid))
                self.assertTrue(result.ok, result.content)
                self.assertEqual(waiter.result(), 'Займусь.')
                self.assertTrue(app.speakers.later[sid])
                self.assertEqual(app.store.session(sid)['meta'].get('home_channel', 'pult'), 'pult')  # адрес разговора не тронут
            finally:
                app.speakers.items.clear()
                await app.close()


class FakePulsar:
    """Колонка Pulsar с точки зрения сети: приветствие, подписка, звук фразы, события."""

    def __init__(self, audio):
        self.audio, self.events, self.announces = audio, [], []
        self.requests = 1  # сколько раз колонка «слышит Алису»: продолжение разговора — ещё раз
        self.subscribed = asyncio.Event()
        self.ended = asyncio.Event()

    async def handle(self, reader, writer):
        async def send(kind, payload=b''):
            writer.write(api.frame(kind, payload))
            await writer.drain()
        try:
            while True:
                kind, payload = await api.read_frame(reader)
                fields = api.decode(payload)
                if kind == api.HELLO_REQ:
                    await send(api.HELLO_RESP, api.encode((1, 1), (2, 10), (3, 'pulsar')))
                elif kind == api.DEVICE_INFO_REQ:
                    await send(api.DEVICE_INFO_RESP, api.encode((2, 'yandex-lite2'), (6, 'Станция Лайт 2'), (13, 'Кухня')))
                elif kind == api.SUBSCRIBE_VA_REQ:
                    self.subscribed.set()
                    await send(api.VA_REQUEST, api.encode((1, True), (5, 'Алиса')))
                elif kind == api.VA_RESPONSE:
                    for start in range(0, len(self.audio), 1024):
                        await send(api.VA_AUDIO, api.encode((1, self.audio[start:start + 1024])))
                elif kind == api.VA_EVENT:
                    data = {}
                    for item in fields.get(2, []):
                        inner = api.decode(item)
                        data[api.text(inner, 1)] = api.text(inner, 2)
                    self.events.append((api.number(fields, 1), data))
                    if api.number(fields, 1) == api.RUN_END:
                        self.requests -= 1
                        if self.requests > 0:
                            await send(api.VA_REQUEST, api.encode((1, True)))
                        else:
                            self.ended.set()
                elif kind == api.VA_ANNOUNCE_REQ:
                    self.announces.append((api.text(fields, 1), api.text(fields, 2)))
                elif kind == api.DISCONNECT_REQ:
                    await send(api.DISCONNECT_RESP)
                    break
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            writer.close()


class Bus:
    def listen(self, fn): pass
    def publish(self, event): pass


class Store:
    def __init__(self): self.meta, self.sessions = {}, {}
    def meta_get(self, key, default=None): return self.meta.get(key, default)
    def session(self, sid): return self.sessions.setdefault(sid, {'meta': {}})
    def update_session(self, sid, meta): self.session(sid)['meta'].update(meta)


class Mind:
    def __init__(self, owner): self.owner, self.said = owner, []

    async def say(self, sid, text, **kw):
        self.said.append((sid, text, kw))
        asyncio.get_running_loop().call_later(0.05, lambda: asyncio.ensure_future(
            self.owner.answered({'type': 'answer', 'channel': 'speaker', 'session': sid, 'result': {'text': self.owner.reply}})))


class Voice:
    def __init__(self): self.heard = []

    async def transcribe(self, audio, **kw):
        self.heard.append((audio[:4], kw))
        return {'text': 'включи свет'}

    async def speak(self, text, **kw):
        return {'audio': b'RIFF' + text.encode(), 'mime': 'audio/wav'}


class Guard:
    def __init__(self): self.pending, self.answers = {}, []

    def respond(self, pid, value, by='owner'):
        self.answers.append((pid, value))
        return self.pending.pop(pid, None) is not None


class Journal:
    def __init__(self): self.rows = []
    def write(self, kind, text, **kw): self.rows.append((kind, text))


class Ctx:
    agent_id = 'main'

    def __init__(self, owner):
        self.store, self.voice, self.mind = Store(), Voice(), Mind(owner)
        self.guard, self.journal = Guard(), Journal()


class FakeSpeakers(Speakers):
    def __init__(self):
        self.items, self.media, self.later, self.runtime, self.restarting = {}, Media(), {}, True, None
        self.app = type('App', (), {'bus': Bus()})()
        self.ctx, self.reply, self.cfg = Ctx(self), '**Готово**, свет включён.', dict(CFG,quiet_from=0,quiet_to=0)
        self.ctx.store.meta['speaker:session:kitchen'] = 'S1'

    def settings(self): return self.cfg
    def event(self): pass
    def selected(self, station):
        station.sid = 'S1'
        return self.ctx, 'S1'
    def station_of(self, ctx, sid): return next(iter(self.items.values()), None)

    async def voice(self, ctx, sid, speech):
        result = await ctx.voice.speak(speech)
        return self.media.put(result['audio'], 'wav')

    async def answered(self, event):
        # тот же разбор, но агенты стаи — одна поддельная
        self.app.agents = type('Agents', (), {'get': lambda _s, _id: self.ctx})()
        await Speakers.answered(self, event)


class ConversationTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakePulsar(pcm(0.4, 20) + pcm(0.8, 6000) + pcm(1.0, 20))
        self.server = await asyncio.start_server(self.fake.handle, '127.0.0.1', 0)
        port = self.server.sockets[0].getsockname()[1]
        self.owner = FakeSpeakers()
        self.station = Station(self.owner, {'id': 'kitchen', 'name': 'Кухня', 'host': '127.0.0.1', 'port': port, 'agent_id': 'main'})
        self.owner.items['kitchen'] = self.station

    async def asyncTearDown(self):
        await self.station.stop()
        await self.owner.media.stop()
        self.server.close()
        await self.server.wait_closed()

    async def test_full_voice_turn(self):
        self.station.start()
        await asyncio.wait_for(self.fake.ended.wait(), 10)
        kinds = [k for k, _ in self.fake.events]
        self.assertEqual(kinds[:4], [api.RUN_START, api.STT_START, api.STT_VAD_START, api.STT_VAD_END])
        self.assertEqual(kinds[-1], api.RUN_END)
        data = {k: d for k, d in self.fake.events}
        self.assertEqual(data[api.STT_END]['text'], 'включи свет')
        self.assertEqual(data[api.INTENT_END]['speech'], 'Готово, свет включён.')
        self.assertEqual(data[api.INTENT_END]['continue_conversation'], '0')
        url = data[api.TTS_END]['url']
        self.assertTrue(url.startswith('http://127.0.0.1:'))
        body = await asyncio.to_thread(lambda: urllib.request.urlopen(url, timeout=5).read())
        self.assertEqual(body, b'RIFF' + 'Готово, свет включён.'.encode())
        self.assertTrue(self.owner.media.was_served(url))
        self.assertEqual(set(self.station.timing), {'heard_seconds', 'stt', 'model', 'tts', 'total'})  # где колонка ждёт
        self.assertEqual(self.owner.ctx.store.session('S1')['meta']['home_channel'], 'speaker')
        sid, text, kw = self.owner.ctx.mind.said[0]
        self.assertEqual((sid, text, kw['channel'], kw['actor']), ('S1', '[Колонка «Кухня»: распознано с голоса, возможны ошибки; ответ — вслух, всё словами, без цифр и значков] включи свет', 'speaker', 'owner'))
        self.assertEqual((kw['model_role'], kw['effort']), ('fast', 'low'))
        self.assertEqual(self.station.state, 'ok')
        self.assertEqual(self.station.info['friendly_name'], 'Кухня')

    async def test_interim_message_answers_now_final_is_announced(self):
        self.owner.cfg['wait_seconds'] = 10

        async def say(sid, text, **kw):
            async def later():
                await asyncio.sleep(0.05)
                await self.owner.say(type('T', (), {'app': self.owner.ctx, 'session_id': sid})(), 'Займусь, скажу, когда закончу.')
                await asyncio.sleep(0.2)
                await self.owner.answered({'type': 'answer', 'channel': 'speaker', 'session': sid, 'result': {'text': 'Всё обновил.'}})
            asyncio.ensure_future(later())
        self.owner.ctx.mind.say = say
        self.station.start()
        await asyncio.wait_for(self.fake.ended.wait(), 10)
        data = {k: d for k, d in self.fake.events}
        self.assertEqual(data[api.INTENT_END]['speech'], 'Займусь, скажу, когда закончу.')
        for _ in range(50):
            if self.fake.announces:
                break
            await asyncio.sleep(0.05)
        self.assertEqual(self.fake.announces[0][1], 'Всё обновил.')

    async def test_errors_are_explained(self):
        reports = []
        self.station.report = reports.append

        async def broken(*a, **kw):
            raise HumanError('voice', 'Голос выключен или агент остановлен', 'Настройки → Голос.')
        self.owner.ctx.voice.transcribe = broken
        self.station.start()
        await asyncio.wait_for(self.fake.ended.wait(), 10)
        self.assertEqual([d['code'] for k, d in self.fake.events if k == api.ERROR], ['stt-provider-error'])
        self.assertTrue(any('Уши' in r and 'Голос выключен' in r for r in reports), reports)
        url = self.owner.media.put(b'x', 'wav')
        await self.station.fetched(url, wait=0)
        self.assertIn('не забрала звук', reports[-1])
        self.assertIn('ufw allow', reports[-1])

    async def test_shared_conversation_announces_only_speaker_turns(self):
        self.station.sid, self.station.link, self.station.state = 'S1', None, 'ok'
        spoken_now = []

        async def announce(ctx, sid, text, **kw):
            spoken_now.append(text)
        self.station.announce = announce
        self.owner.later['S1'] = True
        await self.owner.answered({'type': 'answer', 'channel': 'telegram', 'source': 'telegram', 'session': 'S1', 'result': {'text': 'Ответ в Telegram'}})
        self.assertEqual(spoken_now, [])
        await self.owner.answered({'type': 'answer', 'channel': 'telegram', 'source': 'speaker', 'session': 'S1', 'result': {'text': 'Итог для колонки'}})
        self.assertEqual(spoken_now, ['Итог для колонки'])
        await self.owner.answered({'type': 'answer', 'channel': 'pult', 'source': 'speaker', 'session': 'OTHER', 'result': {'text': 'чужой'}})
        self.assertEqual(spoken_now, ['Итог для колонки'])
        # колонка — последний канал: сердцебиение звучит из неё, а ночью уходит в Telegram
        await self.owner.answered({'type': 'answer', 'channel': 'speaker', 'source': 'heart', 'session': 'S1', 'result': {'text': 'Проверил почту.'}})
        self.assertEqual(spoken_now[-1], 'Проверил почту.')
        published = []
        self.owner.app.bus.publish = published.append
        self.owner.cfg.update(quiet_from=0, quiet_to=23) if time.localtime().tm_hour < 23 else self.owner.cfg.update(quiet_from=23, quiet_to=22)
        await self.owner.notified({'type': 'notify', 'kind': 'heart', 'channel': 'speaker', 'text': 'Ночное'})
        self.assertEqual(spoken_now[-1], 'Проверил почту.')
        self.assertEqual(published[-1]['channel'], 'telegram')
        self.assertIn('Ночное', published[-1]['text'])

    async def test_voice_approval(self):
        self.fake.requests = 2
        card = {'id': 'c1', 'agent_id': 'main', 'session': 'S1', 'summary': 'Обновить Бендер', 'can_session': True}

        async def say(sid, text, **kw):
            self.owner.ctx.guard.pending['c1'] = {'card': card}
            self.owner.on_event({'type': 'approval', 'card': card})
        self.owner.ctx.mind.say = say
        heard = ['обнови себя', 'да']

        async def transcribe(audio, **kw):
            return {'text': heard.pop(0)}
        self.owner.ctx.voice.transcribe = transcribe
        self.owner.app.agents = type('Agents', (), {'get': lambda _s, _id: self.owner.ctx})()
        self.station.start()
        await asyncio.wait_for(self.fake.ended.wait(), 15)
        ends = [d for k, d in self.fake.events if k == api.INTENT_END]
        self.assertIn('Предохранитель спрашивает: Обновить Бендер', ends[0]['speech'])
        self.assertEqual(ends[0]['continue_conversation'], '1')
        self.assertEqual(ends[1]['speech'], 'Разрешил.')
        self.assertEqual(self.owner.ctx.guard.answers, [('c1', 'once')])

    async def test_silence_ends_quietly(self):
        self.fake.audio = pcm(3, 20)
        self.owner.cfg['listen_seconds'] = 5
        self.station.start()
        await asyncio.wait_for(self.fake.ended.wait(), 15)
        errors = [d for k, d in self.fake.events if k == api.ERROR]
        self.assertEqual(errors[0]['code'], 'stt-no-text-recognized')
        self.assertEqual(self.owner.ctx.mind.said, [])


class MediaTest(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_link_is_404(self):
        media = Media()
        await media.start('127.0.0.1', 0)
        try:
            url = media.put(b'abc', 'wav')
            bad = url.rsplit('/', 1)[0] + '/0123.wav'
            def fetch(u):
                try:
                    return urllib.request.urlopen(u, timeout=5).read()
                except urllib.error.HTTPError as exc:
                    return exc.code
            self.assertEqual(await asyncio.to_thread(fetch, url), b'abc')
            self.assertEqual(await asyncio.to_thread(fetch, bad), 404)
            self.assertEqual(await asyncio.to_thread(fetch, 'http://127.0.0.1:%d/' % media.port), 404)
        finally:
            await media.stop()


class StreamTest(unittest.IsolatedAsyncioTestCase):
    async def test_stream_link_plays_while_written(self):
        media = Media()
        await media.start('127.0.0.1', 0)
        try:
            url, stream = media.stream('wav')
            stream.write(b'first')
            reading = asyncio.ensure_future(asyncio.to_thread(lambda: urllib.request.urlopen(url, timeout=5).read()))
            await asyncio.sleep(0.2)
            self.assertFalse(reading.done())  # соединение ждёт продолжения
            stream.write(b'-second')
            stream.close()
            self.assertEqual(await reading, b'first-second')
        finally:
            await media.stop()

    def test_cut_by_sentences(self):
        from bender.speakers.stream import cut
        self.assertEqual(cut('Привет. Сейчас'), len('Привет. '))
        self.assertEqual(cut('Стоит 3.5 тысячи'), 0)  # точка внутри числа — не конец
        self.assertEqual(cut('Один! Два? Три'), len('Один! Два? '))
        self.assertEqual(cut('хвост', final=True), 5)
        long = 'слово, ' * 60
        self.assertGreater(cut(long), 0)

    async def test_voice_turn_speaks_first_sentence_before_answer_ends(self):
        import io
        import wave
        from bender.core.bus import EventBus

        def wav(text):
            out = io.BytesIO()
            with wave.open(out, 'wb') as target:
                target.setnchannels(1); target.setsampwidth(2); target.setframerate(16000); target.writeframes(text.encode())
            return out.getvalue()

        fake = FakePulsar(pcm(0.4, 20) + pcm(0.8, 6000) + pcm(1.0, 20))
        server = await asyncio.start_server(fake.handle, '127.0.0.1', 0)
        owner = FakeSpeakers()
        owner.cfg['stream'] = True
        ctx = owner.ctx
        ctx.bus = EventBus(); ctx.bus.bind_loop(asyncio.get_running_loop())
        spoken_parts, checks = [], []

        async def speak(text, **kw):
            spoken_parts.append(text)
            return {'audio': wav(text), 'mime': 'audio/wav'}
        ctx.voice.speak = speak

        async def allowed(_ctx, _sid):
            checks.append(_sid)
        owner.allowed = allowed
        progress_before_end = []

        async def say(sid, text, **kw):
            async def model():
                ctx.bus.publish({'session': sid, 'type': 'delta', 'kind': 'text', 'text': 'Свет включаю. '})
                ctx.bus.publish({'session': sid, 'type': 'delta', 'kind': 'text', 'text': 'Ещё '})
                await asyncio.sleep(0.4)
                progress_before_end.append(any(d.get('tts_start_streaming') == '1' for k, d in fake.events if k == api.INTENT_PROGRESS))
                ctx.bus.publish({'session': sid, 'type': 'tool_start', 'id': '1', 'name': 'x'})
                ctx.bus.publish({'session': sid, 'type': 'delta', 'kind': 'text', 'text': 'Готово'})
                await owner.answered({'type': 'answer', 'channel': 'speaker', 'session': sid, 'result': {'text': 'Ещё Готово'}})
            asyncio.ensure_future(model())
        ctx.mind.say = say
        station = Station(owner, {'id': 'kitchen', 'name': 'Кухня', 'host': '127.0.0.1', 'port': server.sockets[0].getsockname()[1], 'agent_id': 'main'})
        owner.items['kitchen'] = station
        try:
            station.start()
            await asyncio.wait_for(fake.ended.wait(), 10)
            data = {k: d for k, d in fake.events}
            flow = data[api.RUN_START]['url']
            self.assertEqual(data[api.TTS_END]['url'], flow)  # конец — та же ссылка, повторно колонка не играет
            self.assertEqual(progress_before_end, [True])  # первое предложение зазвучало, пока модель писала
            self.assertEqual(spoken_parts, ['Свет включаю.', 'Ещё', 'Готово'])
            self.assertEqual(checks, ['S1'])
            self.assertEqual(data[api.INTENT_END]['speech'], 'Свет включаю. Ещё Готово')
            body = await asyncio.to_thread(lambda: urllib.request.urlopen(flow, timeout=5).read())
            self.assertEqual(body[:4], b'RIFF')
            self.assertEqual(body[44:], 'Свет включаю.ЕщёГотово'.encode())
            self.assertIn('first_audio', station.timing)
        finally:
            await station.stop()
            await owner.media.stop()
            server.close()
            await server.wait_closed()


if __name__ == '__main__':
    unittest.main()
