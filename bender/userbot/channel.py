"""Аккаунт Telegram, закреплённый за агентом: кого видит, кого слушает, кому отвечает.

Свой у каждого агента: настройки (группа userbot — только хозяин), вход в Сейфе, разговоры и хозяин — в хранилище агента.
По умолчанию аккаунт никого не видит и никому не отвечает, пока хозяин не внесёт чат в список.
"""
from __future__ import annotations

import asyncio
import base64
import hmac
import secrets
import time

from ..channels import media
from ..core.errors import HumanError
from ..tools.registry import ToolContext
from ..tools.web import untrusted
from .link import Link, Refused
from .runtime import failed

WAKE = ('never', 'mention', 'always')
KINDS = {'user': 'личный чат', 'bot': 'бот', 'group': 'группа', 'supergroup': 'группа', 'channel': 'канал'}
FILES = {'photo': 'photo', 'voice': 'voice', 'audio': 'audio', 'video': 'video', 'video_note': 'video', 'gif': 'video', 'document': 'document', 'sticker': 'photo'}
YES = {'да': 'once', 'yes': 'once', 'ок': 'once', 'ok': 'once', '+': 'once', 'да, один раз': 'once', 'да один раз': 'once', 'в разговоре': 'task', 'да в разговоре': 'task',
       'всегда': 'always', 'да всегда': 'always', 'нет': 'deny', 'no': 'deny', '-': 'deny'}
HELP = ('Команды этого чата:\n/new — новый разговор\n/stop — прервать работу агента\n/status — состояние\n/estop — стоп-кран: остановить всех агентов\n/resume — снять стоп-кран\n'
        'Остальное управление — в пульте и в общем боте.')


class Userbot:
    RETRY = 5  # секунд до первого перезапуска помощника; дальше удваивается до 5 минут

    def __init__(self, ctx):
        self.ctx = ctx
        self.host = ctx.host
        self.link = None
        self.task = None
        self.worker = None
        self.sender = None
        self.inbox = None
        self.outbox = None
        self.exited = None
        self.state = 'off'
        self.notice = ''
        self.stage = ''
        self.code = None
        self.runtime = False
        self.listening = False
        self.stopping = False
        self.restarting = None
        self.cards = {}
        self.seen = []
        self.background = set()
        self.revoked = False

    # --- состояние -----------------------------------------------------------------------

    def key(self, part):
        return 'userbot:' + self.ctx.agent_id + ':' + part

    def settings(self):
        return self.ctx.settings.group('userbot')

    def me(self):
        return self.ctx.store.meta_get('userbot:me', None)

    def owner(self):
        owner = self.ctx.store.meta_get('userbot:owner', {})
        me = self.me() or {}
        return owner if owner.get('user_id') and owner.get('binding') == me.get('id') else None

    def ready(self):
        return self.state == 'ok' and self.link is not None and self.link.alive()

    def serving(self):
        """Одобрения, вопросы и уведомления этого агента идут в чат аккаунта, а не в общий бот."""
        return self.settings().get('service') == 'userbot' and self.ready() and bool(self.owner())

    def policy(self, chat):
        rows = self.settings().get('chats') or []
        row = next((r for r in rows if isinstance(r, dict) and str(r.get('id')) == str(chat)), {}) if isinstance(rows, list) else {}
        wake = row.get('wake') if row.get('wake') in WAKE else 'never'
        return {'id': chat, 'title': str(row.get('title') or ''), 'see': row.get('see') is True, 'wake': wake if row.get('see') is True else 'never', 'reply': row.get('reply') is True}

    def public(self):
        code = self.code if self.code and self.code['expires'] > time.monotonic() else None
        cfg = self.settings()
        return {'agent_id': self.ctx.agent_id, 'runtime': self.host.userbot_runtime.public(), 'has_api': bool(self.host.secrets.get(self.key('api'))),
                'has_session': bool(self.host.secrets.get(self.key('session'))), 'state': self.state, 'notice': self.notice, 'stage': self.stage, 'me': self.me(), 'owner': self.owner(),
                'code': code['value'] if code else None, 'settings': {**cfg, 'chats': [self.policy(r.get('id')) for r in (cfg.get('chats') or []) if isinstance(r, dict)]}}

    def event(self):
        self.ctx.bus.publish({'type': 'channel', 'channel': 'userbot', 'agent_id': self.ctx.agent_id})

    def allowed_to_run(self):
        return not (getattr(self.host, '_closed', False) or getattr(self.ctx, '_closed', False) or self.host.safe or getattr(self.ctx, 'temporary', False))

    # --- запуск и остановка ----------------------------------------------------------------

    def listen(self):
        if not self.listening:
            self.ctx.bus.listen(self.on_bus)
            self.listening = True

    def start(self):
        self.runtime = True
        if not self.allowed_to_run() or not self.settings().get('enabled') or (self.task and not self.task.done()):
            return
        self.stopping = False
        self.listen()
        self.task = asyncio.ensure_future(self.run())

    def reconfigure(self):
        """Смена настроек: включение и выключение. Список чатов читается на каждое сообщение — перезапуск не нужен."""
        if not self.runtime or getattr(self.ctx, '_closed', False) or (self.task is not None and asyncio.current_task() is self.task):
            return
        running = bool(self.task and not self.task.done())
        if bool(self.settings().get('enabled')) == running or self.stage:
            return
        if self.restarting and not self.restarting.done():
            return

        async def apply():
            await self.stop()
            self.start()
        try:
            self.restarting = asyncio.get_running_loop().create_task(apply())
        except RuntimeError:
            pass

    async def open_link(self):
        if self.link is not None:
            await self.link.close()
        self.exited = asyncio.Event()
        self.link = Link(self.ctx, self.on_helper, self.on_exit)
        await self.link.start()
        api = (self.host.secrets.get(self.key('api')) or '').partition(':')
        if not api[0].isdigit() or not api[2]:
            raise failed('Сохрани api_id и api_hash с my.telegram.org на экране «Связи».')
        return await self.link.call('configure', {'api_id': int(api[0]), 'api_hash': api[2], 'session': self.host.secrets.get(self.key('session')) or ''}, timeout=120)

    async def run(self):
        delay = self.RETRY
        try:
            while not self.stopping and self.settings().get('enabled') and self.allowed_to_run():
                relogin = False
                try:
                    await ToolContext(self.ctx, actor='owner', tool='userbot_connect').check('net.read', 'userbot:account', 'подключить аккаунт Telegram агента')
                    result = await self.open_link()
                    if not result.get('authorized'):
                        relogin = True
                    else:
                        self.ctx.store.meta_set('userbot:me', result.get('me'))
                        self.state = 'ok'
                        self.notice = ''
                        delay = self.RETRY
                        self.pump()
                        self.event()
                        await self.exited.wait()
                        if self.revoked:
                            self.revoked = False
                            relogin = True
                        elif not self.stopping:
                            self.state = 'error'
                            self.notice = 'Помощник аккаунта остановился; запускаю снова.'
                except asyncio.CancelledError:
                    raise
                except Refused as error:
                    relogin = error.reason == 'auth'
                    self.state = 'error'
                    self.notice = self.host.guard.scrub(error.hint)
                except HumanError as error:
                    self.state = 'error'
                    self.notice = self.host.guard.scrub(error.title + '. ' + error.hint)
                except Exception as error:  # noqa: BLE001
                    self.state = 'error'
                    self.notice = 'Аккаунт Telegram не запустился: ' + type(error).__name__
                if relogin:
                    self.ctx.settings.set('userbot', {'enabled': False}, by='owner')
                    self.state = 'login'
                    self.notice = 'Вход в аккаунт больше не действует. Войди заново в «Связях».'
                    self.ctx.journal.write('channel', 'Аккаунт Telegram: вход отозван, аккаунт выключен')
                    break
                if self.stopping:
                    break
                self.event()
                await asyncio.sleep(delay)
                delay = min(delay * 2, 300)
        finally:
            if self.link is not None and not self.stage:
                await asyncio.shield(self.link.close())
            if self.state == 'ok' or self.stopping:
                self.state = 'off'
            self.event()

    def pump(self):
        if self.worker is None or self.worker.done():
            self.inbox = asyncio.Queue(maxsize=500)
            self.worker = asyncio.ensure_future(self.work())
        if self.sender is None or self.sender.done():
            self.outbox = asyncio.Queue(maxsize=64)
            self.sender = asyncio.ensure_future(self.deliver())

    async def stop(self):
        self.stopping = True
        tasks = [t for t in (self.task, self.worker, self.sender, *self.background) if t and t is not asyncio.current_task()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.task = self.worker = self.sender = None
        if self.link is not None:
            await self.link.close()
            self.link = None
        self.cards.clear()
        self.stage = ''
        self.code = None
        self.state = 'off'
        self.event()

    async def close(self):
        self.runtime = False
        if self.restarting and self.restarting is not asyncio.current_task():
            self.restarting.cancel()
            await asyncio.gather(self.restarting, return_exceptions=True)
        await self.stop()

    def on_exit(self, code, log):
        if self.exited is not None:
            self.exited.set()
        if not self.stopping:
            self.ctx.journal.write('channel', 'Аккаунт Telegram: помощник остановился', data={'code': code, 'log': [self.host.guard.scrub(line) for line in log]})

    def on_helper(self, data):
        if data.get('event') == 'message' and isinstance(data.get('message'), dict) and self.inbox is not None:
            try:
                self.inbox.put_nowait(data['message'])
            except asyncio.QueueFull:
                self.notice = 'Слишком много сообщений сразу: часть пропущена.'

    def spawn(self, coro):
        task = asyncio.ensure_future(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)

    # --- вход хозяина (только API пульта) ---------------------------------------------------

    def save_api(self, api_id, api_hash):
        api_id = str(api_id or '').strip()
        api_hash = str(api_hash or '').strip()
        if not api_id.isdigit() or len(api_id) > 12 or len(api_hash) != 32 or any(c not in '0123456789abcdef' for c in api_hash.lower()):
            raise failed('api_id — число, api_hash — 32 знака (0–9, a–f). Их выдаёт my.telegram.org → API development tools.', 'Ключи не похожи на настоящие')
        self.host.secrets.set(self.key('api'), api_id + ':' + api_hash.lower(), note='Аккаунт Telegram агента ' + self.ctx.agent_id + ': api_id и api_hash', allow={'hosts': [], 'tools': []})
        self.event()
        return self.public()

    async def login_start(self, phone):
        phone = ''.join(c for c in str(phone or '') if c.isdigit() or c == '+')
        if len(phone.lstrip('+')) < 7:
            raise failed('Укажи номер с кодом страны, например +79001234567.', 'Нужен номер телефона')
        if not self.allowed_to_run():
            raise failed('В безопасном режиме и у временных агентов аккаунт не подключается.')
        await ToolContext(self.ctx, actor='owner', tool='userbot_login').check('net.send', 'userbot:account', 'войти в аккаунт Telegram: Telegram пришлёт код')
        await self.stop()
        self.stopping = False
        self.stage = 'connect'
        try:
            result = await self.open_link()
            if result.get('authorized'):
                return await self.finish(result)
            await self.link.call('login_start', {'phone': phone}, timeout=60)
        except BaseException:
            await self.stop()
            raise
        self.stage = 'code'
        self.state = 'login'
        self.event()
        return self.public()

    async def login_code(self, code=None, password=None):
        if self.stage not in ('code', 'password') or self.link is None:
            raise failed('Сначала укажи номер и запроси код.')
        try:
            if self.stage == 'code':
                result = await self.link.call('login_code', {'code': str(code or '').strip()}, timeout=60)
            else:
                result = await self.link.call('login_password', {'password': str(password or '')}, timeout=60)
        except Refused:
            raise
        if result.get('password_needed'):
            self.stage = 'password'
            self.event()
            return self.public()
        return await self.finish(result)

    async def finish(self, result):
        me = result.get('me') or {}
        if not result.get('authorized') or not me.get('id'):
            raise failed('Telegram не подтвердил вход. Повтори с начала.')
        for other in self.host.agents.loaded.values():
            if other is not self.ctx and (other.store.meta_get('userbot:me', None) or {}).get('id') == me['id'] and self.host.secrets.get('userbot:' + other.agent_id + ':session'):
                await self.stop()
                raise failed('Этот аккаунт уже подключён агенту «' + other.agent_id + '». Один аккаунт — один агент: иначе ответы задвоятся.', 'Аккаунт занят')
        if result.get('session'):
            self.host.secrets.set(self.key('session'), result['session'], note='Аккаунт Telegram агента ' + self.ctx.agent_id + ': вход', allow={'hosts': [], 'tools': []})
        self.ctx.store.meta_set('userbot:me', me)
        self.ctx.journal.write('channel', 'Аккаунт Telegram подключён агенту', actor='owner', data={'user_id': me.get('id'), 'username': me.get('username')})
        await self.stop()
        self.ctx.settings.set('userbot', {'enabled': True}, by='owner')
        self.runtime = True
        self.start()
        return self.public()

    async def logout(self):
        """Завершить вход в Telegram и стереть его из Сейфа. Ключи api остаются — их стирает forget_api."""
        remote = False
        if self.ready():
            try:
                await self.link.call('logout', timeout=30)
                remote = True
            except HumanError:
                pass
        self.ctx.settings.set('userbot', {'enabled': False}, by='owner')
        await self.stop()
        self.host.secrets.delete(self.key('session'))
        self.ctx.store.meta_set('userbot:owner', {})
        self.ctx.store.meta_set('userbot:me', None)
        self.ctx.journal.write('channel', 'Аккаунт Telegram отключён от агента', actor='owner')
        self.notice = '' if remote else 'Вход стёрт из Сейфа, но аккаунт не был на связи: сеанс «Bender» мог остаться в Telegram. Заверши его в Telegram → Настройки → Устройства.'
        self.event()
        return self.public()

    def forget(self):
        """Удаление агента: вход и ключи не остаются в общем Сейфе."""
        for part in ('session', 'api'):
            self.host.secrets.delete(self.key(part))

    def pair(self):
        if not self.ready():
            raise failed('Сначала подключи аккаунт.')
        self.code = {'value': 'BENDER-' + secrets.token_hex(3).upper(), 'expires': time.monotonic() + 900, 'attempts': 0}
        self.event()
        return self.public()

    def unpair(self):
        self.ctx.store.meta_set('userbot:owner', {})
        self.code = None
        self.event()
        return self.public()

    def set_chat(self, chat, changes):
        try:
            chat = int(chat)
        except (TypeError, ValueError):
            raise failed('Нужен числовой id чата из списка.', 'Неверный чат') from None
        rows = [r for r in (self.settings().get('chats') or []) if isinstance(r, dict)]
        old = next((r for r in rows if str(r.get('id')) == str(chat)), None)
        if changes.get('remove'):
            rows = [r for r in rows if r is not old]
            self.ctx.store.meta_set('userbot:context:' + str(chat), [])
        else:
            row = {'see': False, 'wake': 'never', 'reply': False, **(old or {}), 'id': chat}
            for key in ('see', 'reply'):
                if key in changes:
                    if not isinstance(changes[key], bool):
                        raise failed('Выключатели чата — true или false.', 'Неверная настройка чата')
                    row[key] = changes[key]
            if 'wake' in changes:
                if changes['wake'] not in WAKE:
                    raise failed('Будить: never, mention или always.', 'Неверная настройка чата')
                row['wake'] = changes['wake']
            if 'title' in changes:
                row['title'] = str(changes['title'])[:120]
            if not row['see']:
                row['wake'] = 'never'
                self.ctx.store.meta_set('userbot:context:' + str(chat), [])
            if old is None:
                rows.append(row)
            else:
                rows[rows.index(old)] = row
        self.ctx.settings.set('userbot', {'chats': rows}, by='owner')
        self.event()
        return self.public()

    # --- входящие ---------------------------------------------------------------------------

    async def work(self):
        while True:
            message = await self.inbox.get()
            try:
                await self.incoming(message)
            except asyncio.CancelledError:
                raise
            except HumanError as error:
                self.ctx.journal.write('channel', 'Аккаунт Telegram: сообщение не принято — ' + error.title, data={'hint': self.host.guard.scrub(error.hint)[:300]})
            except Exception as error:  # noqa: BLE001 — одно сообщение не останавливает приём
                self.ctx.journal.write('channel', 'Аккаунт Telegram: сбой при разборе сообщения', data={'error': type(error).__name__})

    async def incoming(self, m):
        chat = m.get('chat') or {}
        sender = m.get('sender') or {}
        cid = chat.get('id')
        me = self.me() or {}
        if cid is None or m.get('service') or m.get('out') or sender.get('id') == me.get('id'):
            return
        mark = (cid, m.get('id'), bool(m.get('edited')))
        if mark in self.seen:
            return
        self.seen = (self.seen + [mark])[-1000:]
        owner = self.owner() or {}
        private = chat.get('type') in ('user', 'bot')
        if not owner and private and not m.get('edited') and await self.try_pair(sender, str(m.get('text') or '')):
            return
        by_owner = bool(owner) and sender.get('id') == owner['user_id']
        if private and by_owner:
            if not m.get('edited'):
                await self.from_owner(m)
            return
        row = self.policy(cid)
        if not row['see']:
            return
        wake = not m.get('edited') and (row['wake'] == 'always' or row['wake'] == 'mention' and (private or m.get('mentioned')))
        if not wake:
            self.keep(cid, m)
            return
        await self.from_chat(m, row, by_owner)

    async def try_pair(self, sender, text):
        code = self.code
        if not code or code['expires'] < time.monotonic() or code['attempts'] >= 5 or not text.strip() or not sender.get('id'):
            return False
        code['attempts'] += 1
        if not hmac.compare_digest(text.strip().upper().encode(), code['value'].encode()) or sender.get('bot'):
            return False
        self.ctx.store.meta_set('userbot:owner', {'user_id': sender['id'], 'name': str(sender.get('name') or '')[:80], 'username': sender.get('username') or '',
                                                  'binding': (self.me() or {}).get('id'), 'bound_at': time.time()})
        self.code = None
        self.ctx.journal.write('channel', 'Аккаунт Telegram: хозяин привязан', actor='owner', data={'user_id': sender['id']})
        await self.send_owner('Привязал: теперь я знаю, что это ты. Пиши сюда — отвечу. /help — команды.')
        self.event()
        return True

    def keep(self, cid, m):
        size = int(self.settings().get('context') or 0)
        key = 'userbot:context:' + str(cid)
        if size <= 0:
            return
        rows = [r for r in self.ctx.store.meta_get(key, []) if r.get('id') != m.get('id')]
        rows.append({'id': m.get('id'), 'name': str((m.get('sender') or {}).get('name') or '')[:80], 'text': (str(m.get('text') or '') or self.media_word(m))[:2000]})
        self.ctx.store.meta_set(key, rows[-size:])

    @staticmethod
    def media_word(m):
        info = m.get('media')
        if not info:
            return ''
        return '[' + {'photo': 'фото', 'voice': 'голосовое', 'audio': 'аудио', 'video': 'видео', 'video_note': 'кружок', 'gif': 'гифка', 'document': 'файл', 'sticker': 'стикер', 'poll': 'опрос',
                      'contact': 'контакт', 'location': 'место'}.get(info.get('kind'), 'вложение') + (' ' + str(info.get('name') or info.get('emoji') or info.get('question') or '')[:120]).rstrip() + ']'

    def session(self, cid, title):
        sessions = self.ctx.store.meta_get('userbot:sessions', {})
        sid = sessions.get(str(cid))
        if not sid or not self.ctx.store.session(sid):
            sid = self.ctx.store.new_session('chat', ('Telegram · ' + str(title or cid))[:80], meta={'home_channel': 'userbot', 'userbot_chat': cid})
            sessions[str(cid)] = sid
            self.ctx.store.meta_set('userbot:sessions', sessions)
        return sid

    def chat_of(self, sid):
        if not sid:
            return None
        for chat, session in self.ctx.store.meta_get('userbot:sessions', {}).items():
            if session == sid:
                return int(chat)
        return None

    async def attach(self, m, sid, *, by_owner):
        """Вложение сообщения — во входящие агента. Сбой не мешает тексту: модель получит пометку."""
        info = m.get('media') or {}
        kind = FILES.get(info.get('kind'))
        if not kind:
            return [], self.media_word(m)
        limit = min(int(self.settings().get('max_file_mb') or 20), int(self.ctx.settings.get('attachments', 'max_mb', 20))) * 1024 * 1024
        try:
            media.require(self.ctx)
            if int(info.get('size') or 0) > limit:
                raise failed('файл больше предела Вложений')
            data = await self.link.call('download', {'chat': m['chat']['id'], 'id': m['id'], 'limit': limit}, timeout=300)
            raw = base64.b64decode(data['data'], validate=True)
            item = await media.save(self.ctx, data.get('name') or info.get('name') or 'файл', raw, kind=kind, source='userbot', session=sid, actor='owner' if by_owner else 'agent',
                                    why='сохранить вложение из Telegram-аккаунта', unattended=not by_owner, tainted=not by_owner)
            return [item], ''
        except (HumanError, ValueError, KeyError) as error:
            return [], self.media_word(m)[:-1] + ' — не скачано: ' + (error.hint if isinstance(error, HumanError) else 'сбой')[:200] + ']'

    async def from_owner(self, m):
        text = str(m.get('text') or '').strip()
        if text.startswith('/') and await self.command(text):
            return
        if await self.card_reply(m, text):
            return
        owner = self.owner()
        sid = self.session(owner['user_id'], 'хозяин')
        attachments, note = await self.attach(m, sid, by_owner=True)
        tainted = bool(attachments) or bool(m.get('forward'))
        body = untrusted('пересланное хозяином', text) if m.get('forward') and text else text
        line = '[TG #' + str(m['id']) + '] ' + '\n'.join(x for x in (body, note) if x)
        await self.ctx.mind.say(sid, line, channel='userbot', actor='owner', tainted=tainted, wait=False,
                                content=media.content(line, attachments, vision=self.ctx.settings.get('attachments', 'vision', True)) if attachments else None,
                                message_meta={'userbot_chat': owner['user_id'], 'userbot_message': m['id'], 'attachments': attachments})

    async def from_chat(self, m, row, by_owner):
        chat = m['chat']
        cid = chat['id']
        sender = m.get('sender') or {}
        sid = self.session(cid, row['title'] or chat.get('title'))
        key = 'userbot:context:' + str(cid)
        context = self.ctx.store.meta_get(key, [])
        attachments, note = await self.attach(m, sid, by_owner=by_owner)
        lines = ['Чат: ' + KINDS.get(chat.get('type'), 'чат') + ' «' + str(chat.get('title') or '')[:120] + '»']
        if context:
            lines.append('Раньше в чате:')
            lines += ['#' + str(r.get('id')) + ' ' + str(r.get('name')) + ': ' + str(r.get('text')) for r in context]
            lines.append('Новое сообщение:')
        body = '\n'.join(x for x in (str(m.get('text') or ''), note) if x) or '[пусто]'
        own = by_owner and not m.get('forward')  # слова самого хозяина — не чужой текст; пересланное им — чужой
        if not own:
            who = 'хозяин переслал' if by_owner else str(sender.get('name') or sender.get('id'))[:80] + (' (@' + sender['username'] + ')' if sender.get('username') else '')
            lines.append('От: ' + who + ('' if not m.get('reply_to') else ' · ответ на #' + str(m['reply_to'])) + (' · переслано' if m.get('forward') else ''))
            lines.append(body)
        tail = 'Твой ответ уйдёт в этот чат.' if row['reply'] else 'Ответы в этот чат хозяин не включал: твой итог увидит только он в пульте.'
        head = '[Аккаунт Telegram · чат ' + str(cid) + ' · #' + str(m['id']) + (' · пишет хозяин' if by_owner else ' · пишет не хозяин') + '] ' + tail
        line = head + '\n' + untrusted('чат Telegram' if own else 'сообщение из Telegram', '\n'.join(lines)) + ('\nХозяин пишет: ' + body if own else '')
        if context:
            self.ctx.store.meta_set(key, [])
        if row['reply']:
            self.spawn(self.typing(cid))
        try:
            await self.ctx.mind.say(sid, line, channel='userbot', actor='owner' if by_owner else 'userbot', tainted=True, unattended=not by_owner, wait=False,
                                    content=media.content(line, attachments, vision=self.ctx.settings.get('attachments', 'vision', True)) if attachments else None,
                                    message_meta={'userbot_chat': cid, 'userbot_message': m['id'], 'attachments': attachments})
        except BaseException:
            if context:
                self.ctx.store.meta_set(key, context)
            raise

    async def typing(self, cid):
        try:
            await self.link.call('chat', {'action': 'typing', 'chat': cid}, timeout=15)
        except (HumanError, AttributeError):
            pass

    # --- команды и карточки хозяина -----------------------------------------------------------

    async def command(self, text):
        name = text.split()[0].lstrip('/').split('@')[0].lower()
        name = {'стоп': 'stop', 'новая': 'new', 'продолжить': 'resume', 'start': 'help'}.get(name, name)
        owner = self.owner()
        if name == 'help':
            reply = HELP
        elif name == 'new':
            sessions = self.ctx.store.meta_get('userbot:sessions', {})
            sessions.pop(str(owner['user_id']), None)
            self.ctx.store.meta_set('userbot:sessions', sessions)
            self.session(owner['user_id'], 'хозяин')
            reply = 'Новый разговор начат.'
        elif name == 'stop':
            stopped = self.ctx.mind.stop()
            reply = '⏹ Остановил ходов: ' + str(stopped) + '.' if stopped else 'Сейчас ничего не выполняется.'
        elif name == 'status':
            listed = [r for r in self.public()['settings']['chats'] if r['see'] or r['reply']]
            busy = sum(1 for t in self.ctx.mind.turns.values() if t.task and not t.task.done())
            reply = ('Агент: ' + self.ctx.agent_id + '. Ходов сейчас: ' + str(busy) + '. Чатов в списке: ' + str(len(listed)) + '. Стоп-кран: ' + ('включён' if self.ctx.guard.halted else 'нет') + '.')
        elif name == 'estop':
            self.host.halt()
            reply = '🛑 Стоп-кран: всё остановлено, агенты не действуют. /resume — снять.'
        elif name == 'resume':
            for agent in self.host.agents.loaded.values():
                agent.guard.resume()
            reply = 'Стоп-кран снят.'
        else:
            return False
        await self.send_owner(reply)
        return True

    async def send_owner(self, text):
        owner = self.owner()
        if not owner or not self.ready():
            return None
        ctx = ToolContext(self.ctx, actor='system', tool='userbot_reply', unattended=True)
        await ctx.check('msg.send', 'userbot:chat:' + str(owner['user_id']), 'написать хозяину в чат аккаунта', to_owner=True)
        result = await self.link.call('send', {'chat': owner['user_id'], 'text': text[:4096], 'preview': False})
        return (result.get('ids') or [None])[0]

    async def card_reply(self, m, text):
        """Ответ словами на карточку одобрения или вопрос агента (у аккаунта нет кнопок)."""
        if not self.cards:
            return False
        pid = next((p for p, c in self.cards.items() if c.get('mid') and c['mid'] == m.get('reply_to')), None)
        word = text.strip().lower().rstrip('.!')
        if pid is None:
            pid = next(reversed(self.cards))
            card = self.cards[pid]
            if card['kind'] == 'approval' and word not in YES:
                return False
            if card['kind'] == 'question' and not (word.isdigit() and card.get('options')) and len(self.cards) > 1:
                return False
        card = self.cards[pid]
        if card['kind'] == 'approval':
            if word not in YES:
                await self.send_owner('Не понял ответ. Напиши: да, да в разговоре, да всегда или нет.')
                return True
            done = self.ctx.guard.respond(pid, YES[word], by='owner')
            await self.send_owner('→ ' + ({'once': 'Разрешено', 'task': 'Разрешено в этом разговоре', 'always': 'Разрешено всегда', 'deny': 'Отказано'}[YES[word]] if done else 'Уже решено'))
        else:
            options = card.get('options') or []
            answer = options[int(word) - 1] if word.isdigit() and 0 < int(word) <= len(options) else text.strip()
            if not answer:
                return False
            done = self.host.answer(pid, answer)
            await self.send_owner('→ ' + ('Ответ: ' + answer[:200] if done else 'Вопрос уже закрыт'))
        self.cards.pop(pid, None)
        return True

    # --- исходящие по событиям ядра -------------------------------------------------------------

    def on_bus(self, ev):
        if not self.ready() or self.stopping or self.outbox is None:
            return
        kind = ev.get('type')
        try:
            if kind == 'answer' and ev.get('channel') == 'userbot' and ev.get('agent_id', self.ctx.agent_id) == self.ctx.agent_id:
                result = ev.get('result') or {}
                cid = self.chat_of(ev.get('session'))
                if cid is not None and result.get('text') and not result.get('sent_all'):
                    self.outbox.put_nowait(('answer', cid, ev.get('session'), result['text']))
            elif kind in ('approval', 'question') and (ev.get('card') or {}).get('agent_id') == self.ctx.agent_id and self.serving():
                self.outbox.put_nowait((kind, ev['card']))
            elif kind == 'notify' and ev.get('agent_id', self.ctx.agent_id) == self.ctx.agent_id and ev.get('channel', 'telegram') == 'telegram' and self.serving():
                self.outbox.put_nowait(('notify', str(ev.get('text') or '')))
            elif kind in ('approval_done', 'question_done'):
                self.cards.pop(str(ev.get('id')), None)
        except asyncio.QueueFull:
            self.notice = 'Очередь отправки аккаунта переполнена; итог сохранён в пульте.'

    async def deliver(self):
        while True:
            item = await self.outbox.get()
            try:
                if item[0] == 'answer':
                    await self.answer(*item[1:])
                elif item[0] == 'notify':
                    await self.send_owner(item[1])
                elif item[0] == 'approval':
                    c = item[1]
                    text = ('⚠ Предохранитель спрашивает: ' + str(c.get('summary') or c.get('what')) + '\n' + str(c.get('reason') or '') + ('\nВ этом ходе агент читал недоверенное.' if c.get('tainted') else '')
                            + '\n\nОтветь на это сообщение: да · да в разговоре · ' + ('да всегда · ' if c.get('can_remember') else '') + 'нет')
                    self.cards[str(c['id'])] = {'kind': 'approval', 'mid': await self.send_owner(text)}
                elif item[0] == 'question':
                    c = item[1]
                    options = [str(o) for o in (c.get('options') or [])][:8]
                    text = '❓ ' + str(c.get('question')) + ('\n' + '\n'.join(str(i) + '. ' + o for i, o in enumerate(options, 1)) + '\n\nОтветь номером.' if options else '\n\nОтветь на это сообщение.')
                    self.cards[str(c['id'])] = {'kind': 'question', 'options': options, 'mid': await self.send_owner(text)}
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 — недоставка не останавливает очередь
                self.notice = 'Сообщение в Telegram не доставлено; оно есть в пульте.'
                self.ctx.journal.write('channel', 'Аккаунт Telegram: не доставлено', data={'error': (error.hint if isinstance(error, HumanError) else type(error).__name__)[:300]})
                self.event()

    async def answer(self, cid, sid, text):
        owner = self.owner() or {}
        tainted = bool(((self.ctx.store.session(sid) or {}).get('meta', {}).get('last_turn') or {}).get('tainted'))
        ctx = ToolContext(self.ctx, actor='system', session_id=sid, tool='userbot_reply', tainted=tainted, unattended=True)
        await ctx.check('msg.send', 'userbot:chat:' + str(cid), 'ответить в чат Telegram-аккаунта', to_owner=cid == owner.get('user_id'))
        for start in range(0, len(text), 4000):
            await self.link.call('send', {'chat': cid, 'text': text[start:start + 4000]})

    # --- решения Предохранителя и вызовы тулов -------------------------------------------------

    def decide(self, action, session):
        """Что агент делает в аккаунте сам, а что — с вопросом хозяину. Цели: userbot:chat:<id>, userbot:account, userbot:raw:<метод>."""
        from ..guard.policy import Decision
        rule = self.ctx.guard.policy._rule_for(action)
        if rule and rule.get('verdict') == 'deny':
            return Decision('deny', 'Правило хозяина запрещает это действие в аккаунте Telegram.', 'userbot-rule', False)
        if action.actor == 'owner':
            return Decision('allow', 'Действие хозяина.', 'userbot-owner', False)
        parts = action.target.split(':')
        kind = parts[1] if len(parts) > 1 else ''
        cfg = self.settings()
        owner = self.owner() or {}
        taint = action.tainted and self.ctx.guard.policy.get_settings().get('taint', True)
        if kind == 'chat':
            try:
                cid = int(parts[2])
            except (ValueError, IndexError):
                return Decision('deny', 'Неверный адрес чата.', 'userbot-chat', False)
            row = self.policy(cid)
            mine = cid == owner.get('user_id')
            if action.actor == 'system' and action.tool == 'userbot_reply':
                return Decision('allow', 'Ответ в чат, который разрешил хозяин.', 'userbot-reply', False) if mine or row['reply'] else \
                    Decision('deny', 'Ответы в этот чат хозяин не включал.', 'userbot-reply', False)
            if action.capability == 'net.read':
                if mine or row['see'] or cfg.get('trust') == 'all':
                    return Decision('allow', 'Чат открыт хозяином.', 'userbot-chat', False)
                return Decision('ask', 'Этого чата нет в списке хозяина: чтение подтверждает он.', 'userbot-chat', False)
            if taint and not mine and self.chat_of(session) != cid:
                return Decision('ask', 'Ход читал недоверенные данные, а сообщение уйдёт в другой чат. Хозяин подтверждает.', 'userbot-taint', False)
            if mine or row['reply'] or cfg.get('trust') == 'all':
                return Decision('allow', 'Чат открыт хозяином.', 'userbot-chat', False)
            return Decision('ask', 'В этот чат хозяин отвечать не разрешал: действие подтверждает он.', 'userbot-chat', False)
        if kind == 'account':
            if action.capability == 'net.read' or (cfg.get('trust') == 'all' and not taint):
                return Decision('allow', 'Хозяин открыл агенту аккаунт.', 'userbot-account', False)
            return Decision('ask', 'Действие меняет аккаунт или состав его чатов: подтверждает хозяин.', 'userbot-account', False)
        if kind == 'raw':
            return Decision('ask', 'Прямой вызов API Telegram всегда подтверждает хозяин.', 'userbot-raw', False)
        return Decision('deny', 'Неизвестное действие аккаунта.', 'userbot', False)

    async def request(self, ctx, capability, target, summary, method, params, *, detail=None, timeout=90):
        """Вызов тула агента: сначала Предохранитель, потом помощник. Отзыв входа выключает аккаунт."""
        if not self.ready():
            raise failed('Аккаунт Telegram сейчас не подключён. Хозяин подключает его в «Связях».')
        owner = self.owner() or {}
        await ctx.check(capability, target, summary, to_owner=target == 'userbot:chat:' + str(owner.get('user_id')), detail=detail or {})
        if not self.ready():
            raise failed('Аккаунт отключился, пока ждали ответа хозяина.')
        try:
            return await self.link.call(method, params, timeout=timeout)
        except Refused as error:
            if error.reason == 'auth' and self.exited is not None:
                self.revoked = True
                self.exited.set()
            raise

    def visible(self, chat_id):
        """Показывать ли чат в списке тула: при «только чаты из списка» остальные агенту не называются."""
        if self.settings().get('trust') == 'all' or chat_id == (self.owner() or {}).get('user_id'):
            return True
        row = self.policy(chat_id)
        return row['see'] or row['reply']
