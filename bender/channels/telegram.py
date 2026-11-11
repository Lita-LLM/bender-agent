"""Telegram: Бендер в кармане. Bot API, долгий опрос своим HTTP-клиентом — без библиотек и без вебхука
(не нужен белый адрес). Один запрос висит до 50 секунд: в простое — ни ЦП, ни записи на диск.

- Привязка хозяина одноразовым кодом из пульта: `/start КОД`. Чужие сообщения агенту не идут — бот
  вежливо отвечает раз в час и пишет в бортжурнал.
- Сообщение хозяина → главная сессия (один разговор на все каналы), ответ — сюда же. Пока агент думает —
  «печатает…». Пришло во время чужого хода — встаёт в него, ответ придёт, когда ход закончится.
- Одобрения Предохранителя и вопросы агента — кнопками. Напоминания, тревоги и итоги задач сердца —
  сообщениями (выключается).
- Команды: /stop — прервать работу агента, /estop — стоп-кран (блокирует всё до /resume), /resume — снять стоп, /status — самочувствие, /new — новая страница, /help.
- Фото/файлы и голос ограничены настройками; правки и команды остаются служебными фактами.
"""

from __future__ import annotations

import asyncio
import html as htmlmod
import re
import secrets as pysecrets
import time

from ..core.errors import HumanError
from ..net.http_client import NetError, Timeouts

TOKEN_SECRET = "telegram:token"
OWNER_META = "telegram:owner"
OFFSET_META = "telegram:offset"
LIMIT = 4000
CODE_TTL = 15 * 60
STRANGER_REPLY_EVERY = 3600
RICH_MEDIA_MAX = 10  # сколько картинок/файлов Rich-поста скачивать за раз
ORDER_WAIT = 30.0     # сколько сообщение чата ждёт обработки предыдущего, прежде чем пойти само
ANSWER_RETRY = (5.0, 10.0, 20.0)  # повторы ответа после сетевого сбоя
PAUSE_NOTICE = 30.0   # с какой паузы Telegram за частые сообщения показывать её в пульте и журнале
LATE = 60.0           # опоздание сообщения, о котором чёрный ящик пишет в журнал
NET_RETRY_MAX = 30.0  # самая долгая пауза между попытками связаться после сбоя сети (кроме неверного токена)
POLL_GAP = 70.0       # опрос держится до 50 с; дольше без ответа — значит, сообщения не забирались
URGENT = {'stop', 'стоп', 'estop', 'resume', 'продолжить', 'status', 'help', 'start', 'whoami'}  # вне очереди чата

HELP = ("Я — Бендер. Пиши как обычно — отвечу.\n"
        "/stop — прервать работу агента\n/estop — стоп-кран: остановить всё и не давать действовать до /resume\n/resume — снять стоп\n/status — как я себя чувствую\n/new — новая страница разговора (память остаётся)\n/убрать-навык имя — убрать навык из активных\n/help — это сообщение")


def md_to_html(text: str) -> str:
    """Markdown → HTML Telegram; заголовки/таблицы/списки упрощаются вне блоков кода."""
    parts = re.split(r"(```[\s\S]*?```)", text or "")
    out = []
    for p in parts:
        if p.startswith("```") and p.endswith("```") and len(p) >= 6:
            body = p[3:-3]
            body = body.split("\n", 1)[1] if "\n" in body and re.match(r"^[\w+-]*$", body.split("\n", 1)[0]) else body
            out.append(f"<pre>{htmlmod.escape(body.strip(chr(10)))}</pre>")
            continue
        lines = p.split('\n')
        table = False
        for i, line in enumerate(lines):
            separator = bool(re.fullmatch(r'\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*', line.strip()))  # strip: длинная строка пробелов давала квадратичный перебор
            if not table and i + 1 < len(lines) and '|' in line:
                table = bool(re.fullmatch(r'\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*', lines[i + 1].strip()))
            if table and separator:
                lines[i] = ''
            elif table and '|' in line:
                lines[i] = '• ' + ' — '.join(cell.strip() for cell in line.strip().strip('|').split('|'))
            else:
                table = False
                line = re.sub(r'^\s{0,3}#{1,6}\s+(.+?)\s*#*\s*$', r'**\1**', line)
                lines[i] = re.sub(r'^(\s*)[-*]\s+', r'\1• ', line)
        s = htmlmod.escape('\n'.join(lines), quote=False)
        s = re.sub(r"`([^`\n]+)`", lambda m: f"<code>{m.group(1)}</code>", s)
        s = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", lambda m: f'<a href="{htmlmod.escape(m.group(2))}">{m.group(1)}</a>', s)
        out.append(s)
    return "".join(out)


def split(text: str, limit: int = LIMIT) -> list[str]:
    """Длинное — кусками по абзацам/строкам, не посреди слова."""
    text = text or ""
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text.strip() or not chunks:
        chunks.append(text)
    return chunks


class TelegramError(HumanError):
    pass


class Telegram:
    name = "telegram"

    def __init__(self, app):
        self.app = app
        self.task: asyncio.Task | None = None
        self._restarting = None
        self._restart_lock = asyncio.Lock()
        self._stopping = False
        self.me: dict | None = None
        self.state = "off"           # off | connecting | ok | error
        self.last_error: str | None = None
        self.code: tuple[str, float] | None = None
        self._strangers: dict[int, float] = {}
        self._cards: dict[str, tuple[int, int]] = {}   # карточка одобрения/вопроса → (чат, сообщение)
        self._listening = False
        self._bg: set[asyncio.Task] = set()
        self._reply_to: dict[object, int | None] = {}
        self._live = {}
        self._live_next = {}
        self._sent_at = {}  # чат → когда бот последний раз прислал туда обычное сообщение (оно закрывает черновик)
        self._retry_until = {}
        self._lanes = {}             # очередь чата → последняя задача обработки
        self.pause_until = 0.0       # до какого времени Telegram просил не писать (time.time())
        self.polled_at = None        # когда опрос последний раз вернулся (time.time())
        self.outage = None           # идущий обрыв связи: с какого времени, причина, сколько попыток
        self.last_outage = None      # прошлый обрыв — чтобы объяснить, почему сообщения пришли разом
        self._draft_supported = None
        self._capabilities = {}
        self._albums = {}
        self._commands = None
        self._profile = None
        self._groups = None
        self._topics = None

    @property
    def topics(self):
        if self._topics is None:
            from .telegram_topics import Topics
            self._topics=Topics(self)
        return self._topics

    def selected(self,chat):
        from .telegram_routes import selected
        return selected(self,chat)

    def choose(self,chat,ctx,sid=None):
        from .telegram_routes import choose
        return choose(self,chat,ctx,sid)

    def destination(self,ctx,sid):
        from .telegram_routes import destination
        return destination(self,ctx,sid)

    def tool_destination(self,ctx):
        owner=self.owner() or {};private=owner.get('chat_id') or owner.get('user_id')
        route=ctx.turn.get('_telegram_route') or {}
        chat=self.topics.address(route['chat'],route.get('thread')) if route.get('chat') else self.destination(ctx.app,ctx.session_id)
        if chat!=private and ctx.actor!='owner' and ctx.turn.get('_telegram_owner_requested')!=chat:return private
        return chat

    async def authorize_group(self,ctx,chat,summary):
        owner=self.owner() or {};private=owner.get('chat_id') or owner.get('user_id')
        if chat!=private:await ctx.check('msg.send','telegram:group:'+str(chat),summary,to_owner=False)

    @staticmethod
    def route_key(ctx,sid):return (ctx.agent_id,sid)

    @property
    def groups(self):
        if self._groups is None:
            from .telegram_groups import Groups
            self._groups=Groups(self)
        return self._groups

    @property
    def profile(self):
        if self._profile is None:
            from .telegram_profile import Profile
            self._profile=Profile(self)
        return self._profile

    @property
    def commands(self):
        if self._commands is None:
            from .telegram_commands import Commands
            self._commands=Commands(self)
        return self._commands

    # настройки

    def settings(self) -> dict:
        return self.app.settings.group("telegram")

    def token(self) -> str | None:
        return self.app.secrets.get(TOKEN_SECRET)

    def enabled(self) -> bool:
        return bool(self.settings().get("enabled")) and bool(self.token()) and not self.app.safe

    def owner(self) -> dict | None:
        o = self.app.store.meta_get(OWNER_META)
        return o if isinstance(o, dict) and o.get("user_id") else None

    def public(self) -> dict:
        o = self.owner()
        return {"enabled": bool(self.settings().get("enabled")), "has_token": bool(self.token()), "state": self.state, "error": self.last_error,
                "bot": {"username": self.me.get("username"), "name": self.me.get("first_name"),'has_topics_enabled':bool(self.me.get('has_topics_enabled')),'allows_users_to_create_topics':bool(self.me.get('allows_users_to_create_topics'))} if self.me else None,
                'topics':[row for row in self.topics.rows().values() if not row.get('replacement')],
                "owner": {"name": o.get("name"), "username": o.get("username")} if o else None,
                "rich": {"messages":self.capability("sendRichMessage"),"drafts":self.capability("sendRichMessageDraft")},
                "commands": {"ready":bool(self.app.store.meta_get("telegram:commands_hash")),"running":bool(self._commands and self._commands.task and not self._commands.task.done())},
                "code": self.code[0] if self.code and self.code[1] > time.time() else None, "settings": self.settings(),
                "pause": max(0, int(self.pause_until - time.time())),
                "polled": int(time.time() - self.polled_at) if self.polled_at else None,
                "outage": {"seconds": int(time.time() - self.outage["since"]), "reason": self.outage["reason"], "tries": self.outage["tries"]} if self.outage else None}

    # Bot API

    async def api(self, method: str, payload: dict | None = None, *, timeout: float = 30, files=None,_topic_retry=True) -> object:
        from .telegram_topics import Address,THREAD_METHODS
        payload=dict(payload or {})
        address=payload.get('chat_id')
        if isinstance(address,Address):
            payload['chat_id']=int(address)
            if address.thread is not None and method in THREAD_METHODS and self.topics.enabled(address):
                payload['message_thread_id']=self.topics.address(address,address.thread).thread
        token = self.token()
        if not token:
            raise TelegramError("tg_token", "Нет токена бота Telegram", "Создай бота у @BotFather и вставь токен в «Связь».")
        base = str(self.settings().get("api_base") or "https://api.telegram.org").rstrip("/")
        try:
            options={'json':payload or {}}
            if files:
                from .media import multipart
                raw,ctype=multipart(payload or {},files)
                options={'body':raw,'headers':{'Content-Type':ctype}}
            resp = await self.app.http.post(f"{base}/bot{token}/{method}", **options, timeouts=Timeouts(connect=20, read=timeout, total=timeout + 10))
            async with resp:
                data = await resp.json()
        except NetError as e:
            raise TelegramError("tg_net", "Нет связи с Telegram", str(e)) from None
        except ValueError:
            raise TelegramError("tg_bad", "Telegram ответил непонятно", method) from None
        if not isinstance(data, dict) or not data.get("ok"):
            desc = str((data or {}).get("description") if isinstance(data, dict) else data)
            code = (data or {}).get("error_code") if isinstance(data, dict) else None
            if _topic_retry and code==400 and 'thread not found' in desc.lower() and isinstance(address,Address) and address.thread is not None and method in THREAD_METHODS:
                replacement=await self.topics.repair(address)
                return await self.api(method,{**payload,'chat_id':replacement},timeout=timeout,files=files,_topic_retry=False)
            if code == 401:
                raise TelegramError("tg_auth", "Telegram не принял токен бота", "Проверь токен у @BotFather (/token) и вставь заново.")
            raise TelegramError("tg_api", f"Telegram: {desc[:200]}", method, data={"code": code, "retry_after": ((data or {}).get("parameters") or {}).get("retry_after")})
        if method.startswith('send') and not method.endswith('Draft') and method!='sendChatAction':
            try:self._sent_at[(int(address),getattr(address,'thread',None))]=time.monotonic()
            except (TypeError,ValueError):pass
        return data.get("result")

    def capability(self, method):
        import hashlib
        key=hashlib.sha256((str(self.settings().get('api_base'))+'\0'+str(self.token())).encode()).hexdigest()
        return self._capabilities.get(key,{}).get(method)

    def set_capability(self, method, value):
        import hashlib
        key=hashlib.sha256((str(self.settings().get('api_base'))+'\0'+str(self.token())).encode()).hexdigest()
        cache=self._capabilities
        entry=cache.setdefault(key,{})
        if entry.get(method) is not value:
            entry[method]=value
            self._capabilities=dict(list(cache.items())[-4:])
            self.app.bus.publish({'type':'channel','channel':'telegram','state':self.state,'agent_id':'*'})
        if method=='sendMessageDraft':self._draft_supported=value

    def rich_enabled(self):
        return self.settings().get('rich',True) and self.capability('sendRichMessage') is True

    @staticmethod
    def unsupported(error):
        return error.data.get('code')==404 or any(s in error.title.lower() for s in ('method not found','unknown method','method is not available'))

    async def rich_request(self,payload):
        chat=payload['chat_id']
        for attempt in range(4):
            delay=self._retry_until.get(chat,0)-time.monotonic()
            if delay>0:await asyncio.sleep(delay)
            try:return await self.api('sendRichMessage',payload)
            except TelegramError as error:
                if error.data.get('code')!=429 or attempt==3:raise
                try:delay=max(1,min(86400,float(error.data.get('retry_after') or 1)))
                except (TypeError,ValueError):delay=1
                self.pause(chat,delay)

    async def send(self, chat_id: int, text: str, *, buttons=None, reply_to=None):
        from .telegram_events import capture
        capture(chat_id,text)
        from .telegram_rich import parts,RichFormatError
        if not self.settings().get('rich',True) or self.capability('sendRichMessage') is False:
            return await self.send_plain(chat_id,text,buttons=buttons,reply_to=reply_to)
        try:prepared=parts(text)
        except RichFormatError:
            return await self.send_plain(chat_id,text,buttons=buttons,reply_to=reply_to)
        last=None
        for n,(original,markdown) in enumerate(prepared):
            payload={'chat_id':chat_id,'rich_message':{'markdown':markdown,'skip_entity_detection':True}}
            if n==0 and reply_to:payload['reply_parameters']={'message_id':reply_to,'allow_sending_without_reply':True}
            if n==len(prepared)-1 and buttons:payload['reply_markup']={'inline_keyboard':buttons}
            try:
                result=await self.rich_request(payload)
                self.set_capability('sendRichMessage',True)
                last=(result or {}).get('message_id')
            except TelegramError as error:
                if error.data.get('code')==429:raise
                if self.unsupported(error):self.set_capability('sendRichMessage',False)
                elif error.data.get('code') not in (400,403):raise
                # Не повторяем уже доставленные части; исходный markdown остаётся для HTML-пути.
                rest=''.join(source for source,_ in prepared[n:])
                return await self.send_plain(chat_id,rest,buttons=buttons,reply_to=reply_to if n==0 else None)
        for live in self._live.values():
            if live.chat==chat_id and not live.closed:live.version+=1;live.wake.set()
        return last

    async def send_plain(self, chat_id: int, text: str, *, buttons: list[list[dict]] | None = None, reply_to: int | None = None) -> int | None:
        """Отправить (кусками, с разметкой; не разобралась разметка — простым текстом). Возвращает id последнего сообщения."""
        last = None
        chunks = split(text)
        for i, chunk in enumerate(chunks):
            payload: dict = {"chat_id": chat_id, "text": md_to_html(chunk), "parse_mode": "HTML", "disable_web_page_preview": True}
            if reply_to and i == 0:
                payload["reply_parameters"] = {"message_id": reply_to, "allow_sending_without_reply": True}
            if buttons and i == len(chunks) - 1:
                payload["reply_markup"] = {"inline_keyboard": buttons}
            for attempt in range(4):
                delay = self._retry_until.get(chat_id, 0) - time.monotonic()
                if delay > 0: await asyncio.sleep(delay)
                try:
                    res = await self.api("sendMessage", payload)
                    break
                except TelegramError as e:
                    if e.data.get('code') == 429 and attempt < 3:
                        try: delay = max(1, min(86400, float(e.data.get('retry_after') or 1)))
                        except (ValueError, TypeError): delay = 1
                        self.pause(chat_id, delay)
                        continue
                    if 'parse_mode' in payload and e.code == 'tg_api' and any(s in e.title.lower() for s in ('parse', 'entit')):
                        payload.pop('parse_mode')
                        payload['text'] = chunk
                        if attempt < 3: continue
                    raise
            last = (res or {}).get("message_id") if isinstance(res, dict) else None
            for live in self._live.values():
                if live.chat == chat_id and not live.closed:
                    live.version += 1; live.wake.set()
        return last

    def _task_done(self, task):
        self._bg.discard(task)
        if task.cancelled():return
        error=task.exception()
        if error:
            self.last_error=str(getattr(error,'title','') or error)[:300]
            if not getattr(self.app,'_closed',False):
                self.app.journal.write('channel','Telegram: фоновая отправка не удалась: '+self.last_error,actor='telegram')

    def pause(self, chat, seconds: float) -> None:
        """Telegram ответил 429: не писать в чат до срока. Долгую паузу видно в пульте и журнале."""
        until=time.monotonic()+seconds
        self._retry_until[chat]=max(until,self._retry_until.get(chat,0))
        if seconds<PAUSE_NOTICE or time.time()+seconds<=self.pause_until:return
        self.pause_until=time.time()+seconds
        self.app.journal.write('channel',f'Telegram просит паузу {int(seconds)} с: слишком частые сообщения в чат. Ответы уйдут после неё.',actor='telegram',data={'seconds':int(seconds)})
        self.app.bus.publish({'type':'channel','channel':'telegram','agent_id':'*','state':self.state})

    def _bg_task(self, coro) -> None:
        task=asyncio.ensure_future(coro)
        self._bg.add(task)
        task.add_done_callback(self._task_done)
        return task

    # жизнь

    def start(self) -> None:
        if getattr(self.app,'_closed',False):return
        if not self._listening:
            self.app.bus.listen(self._on_event)
            self.app.bus.listen(self._on_settings)
            self._listening = True
        if self.enabled() and not (self.task and not self.task.done()):
            self._stopping=False
            self.state = "connecting"
            self.task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stopping=True
        restart=self._restarting
        if restart and restart is not asyncio.current_task():
            restart.cancel()
            await asyncio.gather(restart,return_exceptions=True)
            if self._restarting is restart:self._restarting=None
        lives, self._live = list(self._live.values()), {}
        for live in lives:
            live.task.cancel()
        if lives: await asyncio.gather(*(live.task for live in lives), return_exceptions=True)
        self._albums.clear()
        self._lanes.clear()
        pending = list(self._bg)
        for t in pending: t.cancel()
        if pending: await asyncio.gather(*pending, return_exceptions=True)
        self._stopping=True
        task,self.task=self.task,None
        if task:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self.state = "off"

    async def restart(self) -> None:
        async with self._restart_lock:
            await self.stop()
            self.me = None
            self.start()

    def new_code(self) -> str:
        code = "BENDER-" + "".join(pysecrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(6))
        self.code = (code, time.time() + CODE_TTL)
        return code

    def unbind(self) -> None:
        self.app.store.meta_set(OWNER_META, None)
        self.app.journal.write("channel", "Telegram: хозяин отвязан", actor="owner")

    async def _restart_notice(self):
        notice=self.app.store.meta_get('lifecycle:restart_notice')
        if not isinstance(notice,dict):return
        owner=self.owner() or {}
        if notice.get('chat')!=(owner.get('chat_id') or owner.get('user_id')) or notice.get('owner')!=owner.get('user_id'):
            self.app.store.meta_set('lifecycle:restart_notice',None);return
        from .telegram_topics import Address
        try:seconds=max(0,int(time.time()-float(notice.get('at',time.time()))))
        except (ValueError,TypeError,OverflowError):seconds=0
        try:await self.send(Address(notice['chat'],notice.get('thread')),'Снова тут, не было '+str(seconds)+' с.')
        except TelegramError as error:
            if error.data.get('code') not in (400,403):raise
            self.app.journal.write('channel','Уведомление о перезапуске не доставлено; проверь чат/тему хозяина',data={'code':error.data.get('code')})
        self.app.store.meta_set('lifecycle:restart_notice',None)

    async def _run(self) -> None:
        delay = 5.0
        while not self._stopping:
            try:
                self.me = await self.api("getMe")
                self.state, self.last_error, delay = "ok", None, 5.0
                self.commands.start()
                self._bg_task(self._restart_notice())  # отправка может ждать паузу Telegram — опрос не ждёт
                offset = int(self.app.store.meta_get(OFFSET_META, 0) or 0)
                while not self._stopping:
                    updates = await self.api("getUpdates", {"offset": offset, "timeout": 50, "allowed_updates": ["message", "edited_message", "callback_query", "message_reaction", "my_chat_member", "stopped_message_generation"]}, timeout=65)
                    if self._stopping:return
                    previous, self.polled_at = self.polled_at, time.time()
                    try:
                        self._link_up()
                        self._lateness(updates, previous)
                    except Exception as e:  # noqa: BLE001 — диагностика никогда не мешает опросу
                        self.last_error = 'чёрный ящик Telegram: ' + str(e)[:200]
                    for u in updates or []:
                        offset = max(offset, int(u.get("update_id", 0)) + 1)
                        self._dispatch(u)
                    if updates:
                        self.app.store.meta_set(OFFSET_META, offset)  # пишем, только когда что-то пришло
            except asyncio.CancelledError:
                raise
            except TelegramError as e:
                if self._stopping:return
                self.state, self.last_error = "error", f"{e.title}. {e.hint}".strip()
                self._link_down(e)
                if e.code == "tg_auth":
                    delay = 300.0
                retry = (e.data or {}).get("retry_after") if isinstance(e.data, dict) else None
                await asyncio.sleep(float(retry or delay))
                # Раньше пауза росла до 5 минут: пара сбоев сети — и бот молчал, а потом отвечал на всё разом.
                delay = min(delay * 2, 300.0 if e.code == "tg_auth" else NET_RETRY_MAX)
            except Exception as e:  # noqa: BLE001
                if self._stopping:return
                self.state, self.last_error = "error", str(e)[:300]
                self._link_down(e)
                await asyncio.sleep(delay)
                delay = min(delay * 2, NET_RETRY_MAX)

    # чёрный ящик: почему сообщение пришло поздно

    def _link_down(self, error) -> None:
        if isinstance(error, TelegramError):
            reason = error.title + (': ' + str(error.hint)[:160] if error.hint and error.code == 'tg_net' else '')
            if (error.data or {}).get('code') == 409:
                reason += ' — этим ботом опрашивает кто-то ещё: второй Бендер или OpenClaw с тем же токеном'
        else:
            reason = (type(error).__name__ + ': ' + str(error))[:200]
        if self.outage is None:
            self.outage = {'since': time.time(), 'reason': reason, 'tries': 0}
        self.outage['tries'] += 1
        self.outage['reason'] = reason
        if self.outage['tries'] == 2:  # одиночный сбой — обычное дело; второй подряд — уже обрыв
            self.app.journal.write('channel', 'Telegram: нет связи — ' + reason + '. Пробую снова каждые до ' + str(int(NET_RETRY_MAX)) + ' с.', actor='telegram')

    def _link_up(self) -> None:
        if self.outage is None:
            return
        outage, self.outage = self.outage, None
        outage['until'] = time.time()
        self.last_outage = outage
        seconds = int(outage['until'] - outage['since'])
        if outage['tries'] >= 2:
            self.app.journal.write('channel', f"Telegram: связь вернулась, не было {seconds} с ({outage['tries']} попыток; {outage['reason']})", actor='telegram',
                                   data={'seconds': seconds, 'tries': outage['tries']})

    def _lateness(self, updates, previous) -> None:
        """Сообщение пришло намного позже, чем его отправили: пишем в журнал, где оно ждало."""
        dates = [body['date'] for u in updates or [] for body in [u.get('message') or u.get('edited_message') or {}] if isinstance(body.get('date'), (int, float))]
        if not dates:
            return
        now = self.polled_at or time.time()
        since = min(dates)
        late = now - since
        if late < LATE:
            return
        causes = []
        if previous is None:
            text = 'Бендер был выключен или только запустился'
        elif now - previous > POLL_GAP:
            outage = self.last_outage
            if outage and outage.get('until', now) >= since:
                causes.append(f"связи с Telegram не было {int(outage['until'] - outage['since'])} с — {outage['reason']}")
            watch = getattr(self.app.host, 'loop_watch', None)
            for stall in (watch.between(since, now) if watch else []):
                causes.append(f"Бендер стоял {stall['seconds']} с в {stall['where']} (стек в local/stalls.txt)")
            if not causes:
                causes.append(f'опрос Telegram не возвращался {int(now - previous)} с')
            text = 'Бендер не забирал сообщения: ' + '; '.join(causes)
        else:
            text = ('опрос шёл исправно, Telegram отдал сообщение только сейчас — задержка у Telegram или в сети до него. '
                    'Если так с каждым сообщением, проверь часы компьютера')
        self.app.journal.write('channel', f'Telegram: сообщение пришло через {int(late)} с после отправки — ' + text, actor='telegram',
                               data={'late': int(late), 'poll_gap': int(now - previous) if previous else None, 'count': len(dates)})

    async def _seen(self, chat_id, message_id) -> None:
        """👀 на сообщении, которое встало в идущий ход: хозяин видит, что бот его получил."""
        try:
            await self.api('setMessageReaction', {'chat_id': int(chat_id), 'message_id': message_id, 'reaction': [{'type': 'emoji', 'emoji': '👀'}]}, timeout=10)
        except TelegramError:
            pass

    @staticmethod
    def _busy(app, turn, sid) -> str:
        """Чем занят ход: модель, тул или карточка одобрения."""
        parts = [f'ход шёл уже {int(time.monotonic() - turn.started)} с']
        doing = turn.state.get('_doing') if turn else None
        if doing:
            parts.append(f"{doing['what']} {int(time.monotonic() - doing['since'])} с")
        if any((item.get('card') or {}).get('session') == sid for item in list(app.guard.pending.values())):
            parts.append('ждёт одобрения в карточке Предохранителя')
        return ', '.join(parts)

    # входящие

    @staticmethod
    def _lane(u: dict):
        """Очередь обновления. Сообщения одного чата обрабатываются по порядку; одобрения, ответы на вопросы,
        «Стоп» черновика и срочные команды — сразу, мимо очереди."""
        if 'stopped_message_generation' in u:return None
        if 'callback_query' in u:
            cq=u['callback_query'] or {};message=cq.get('message') or {}
            if str(cq.get('data') or '').startswith(('ap:','q:')):return None
            return ('button',(message.get('chat') or {}).get('id'))
        body=u.get('message') or u.get('edited_message') or u.get('message_reaction') or u.get('my_chat_member') or {}
        word=str(body.get('text') or '')[1:].split(maxsplit=1) if str(body.get('text') or '').startswith('/') else []
        if 'message' in u and word and word[0].split('@')[0].lower() in URGENT:return None
        return ((body.get('chat') or {}).get('id'),body.get('message_thread_id'))

    def _dispatch(self, u: dict) -> None:
        """Обработать обновление в фоне. Опрос никогда не ждёт обработку: долгая команда, бекап, расшифровка
        или пауза Telegram не должны отрезать бота от новых сообщений и кнопок одобрения."""
        lane=self._lane(u)
        previous=self._lanes.get(lane) if lane is not None else None
        task=self._bg_task(self._ordered(previous,u))
        if lane is not None:
            self._lanes[lane]=task
            task.add_done_callback(lambda t,k=lane:self._lanes.pop(k,None) if self._lanes.get(k) is t else None)

    async def _ordered(self, previous, u: dict) -> None:
        if previous is not None and not previous.done():
            await asyncio.wait({previous},timeout=ORDER_WAIT)
        try:
            await self._handle(u)
        except Exception as e:  # noqa: BLE001 — одно кривое сообщение не останавливает канал
            self.app.journal.write("channel", f"Telegram: не обработалось сообщение: {e}"[:300], actor="telegram")

    def _collect_album(self, msg):
        key=((msg.get('chat') or {}).get('id'),msg.get('message_thread_id'),msg['media_group_id'])
        old=self._albums.get(key)
        messages=old[0] if old else []
        if old:old[1].cancel()
        if len(messages)<10:messages.append(msg)
        async def flush():
            await asyncio.sleep(.8)  # Конец альбома API не отмечает; ждём тишины после события.
            self._albums.pop(key,None)
            combined={**messages[0],'_album_messages':list(messages)}
            combined.pop('media_group_id',None)
            await self._handle({'message':combined})
        task=asyncio.create_task(flush())
        self._albums[key]=(messages,task)
        self._bg.add(task);task.add_done_callback(self._task_done)

    async def _media_notice(self,ctx,chat,sid,text):
        from ..tools.registry import ToolContext
        owner=self.owner() or {};private=owner.get('chat_id') or owner.get('user_id')
        try:
            await ToolContext(ctx,actor='owner',session_id=sid).check('msg.send','telegram:owner' if chat==private else 'telegram:group:'+str(chat),
                'объяснить обработку присланного вложения',to_owner=chat==private)
            await self.send(chat,text)
        except HumanError as error:ctx.journal.write('channel','Telegram: уведомление о вложении не отправлено',data={'code':error.code})

    async def _download(self, ctx, sid, kind, info):
        """Скачать файл Telegram во входящие агента (до 20 МБ, как разрешает getFile)."""
        from pathlib import Path
        from urllib.parse import quote
        from ..core.pathutil import relative_path
        from . import media
        if int(info.get('file_size') or 0)>media.DOWNLOAD_LIMIT:
            raise HumanError('telegram_file_size','Telegram даёт скачать только до 20 МБ','Сожми файл, раздели его или прикрепи в пульте.')
        data=await self.api('getFile',{'file_id':info.get('file_id')})
        if int(data.get('file_size') or 0)>media.DOWNLOAD_LIMIT:
            raise HumanError('telegram_file_size','Файл больше 20 МБ','Сожми его или пришли иначе: getFile не отдаёт такой размер.')
        rel=str(relative_path(data.get('file_path','')))
        base=str(self.settings().get('api_base') or 'https://api.telegram.org').rstrip('/')
        response=await ctx.http.get(base+'/file/bot'+self.token()+'/'+quote(rel,safe='/'))
        async with response:
            raw=await response.read(limit=media.DOWNLOAD_LIMIT)
            if not response.ok:raise HumanError('telegram_download','Не удалось скачать файл из Telegram','Пришли файл ещё раз.')
        name=info.get('file_name') or kind+Path(rel).suffix
        return await media.save(ctx,name,raw,kind=kind,source='telegram',session=sid)

    async def receive_media(self, ctx, messages, sid):
        from . import media
        from .telegram_rich_in import media as rich_media
        from .video_in import KINDS as VISUAL
        files=[]
        async def download(kind,info):
            item=await self._download(ctx,sid,kind,info)
            if kind in VISUAL:  # для кадров/обложки/подписи стикера; убирается при разборе
                item['_tg']={key:info.get(key) for key in ('duration','emoji','set_name','is_animated','is_video','thumbnail') if info.get(key)}
            return item
        for msg in messages:
            for kind in ('photo','document','audio','video','video_note','voice','sticker','animation'):
                info=msg.get(kind)
                if not info:continue
                if kind=='document' and msg.get('animation'):continue  # Bot API дублирует гифку в document — качаем один раз
                media.require(ctx)
                if isinstance(info,list):info=info[-1]
                files.append(await download(kind,info))
            # Медиа Rich-поста — по возможности: текст поста важнее, поэтому лишнее и тяжёлое пропускаем с пометкой, а не отказываем.
            skipped=0;enabled=ctx.settings.get('attachments','enabled',True)
            for kind,info in rich_media(msg.get('rich_message')):
                size=int(info.get('file_size') or 0)
                if not enabled or len(files)>=RICH_MEDIA_MAX or sum(item['size'] for item in files)+size>media.DOWNLOAD_LIMIT:
                    skipped+=1;continue
                try:
                    item=await download(kind,info)
                    if sum(saved['size'] for saved in files)+item['size']>media.DOWNLOAD_LIMIT:skipped+=1  # размер не был известен заранее
                    else:files.append(item)
                except (HumanError,NetError) as error:
                    skipped+=1
                    self.app.journal.write('channel','Telegram: медиа Rich-поста не скачано: '+str(error),actor='telegram')
            if skipped:msg['_rich_skipped']=(skipped,enabled)
            for kind in ('location','contact'):
                if msg.get(kind):
                    import json
                    files.append(await media.save(ctx,kind+'.json',json.dumps(msg[kind],ensure_ascii=False).encode(),kind=kind,source='telegram',session=sid))
        if sum(item['size'] for item in files)>media.DOWNLOAD_LIMIT:
            raise HumanError('telegram_album_size','Альбом больше 20 МБ суммарно','Пришли файлы отдельными сообщениями.')
        return files

    def remember(self, ctx, chat, message, sid, text, direction,*,source_text=None,tainted=False,command=False,foreign=False):
        """foreign — сам текст не слова хозяина (пересланное); tainted — ход или сообщение несли чужое."""
        if not isinstance(message,int):return
        refs=ctx.store.meta_get('telegram:refs',{})
        refs[str(chat)+':'+str(message)]={'session':sid,'text':text[:4000],'direction':direction,'thread':getattr(chat,'thread',None),'tainted':bool(tainted),'foreign':bool(foreign)}
        if source_text is not None:
            from .telegram_events import digest
            refs[str(chat)+':'+str(message)].update(source_text=source_text[:4096],source_hash=digest(source_text))
        if command:refs[str(chat)+':'+str(message)]['command']=True
        ctx.store.meta_set('telegram:refs',dict(list(refs.items())[-2000:]))
        routes=self.app.store.meta_get('telegram:message_routes',{})
        routes[str(chat)+':'+str(message)]={'agent':ctx.agent_id,'session':sid}
        self.app.store.meta_set('telegram:message_routes',dict(list(routes.items())[-2000:]))

    def require_ref(self, ctx, chat, message, sid):
        ref=ctx.store.meta_get('telegram:refs',{}).get(str(chat)+':'+str(message))
        if not ref or ref['session']!=sid:
            raise HumanError('telegram_ref','Номер сообщения не найден в этом разговоре','Возьми [#номер] из его истории, не из чужого чата.')
        return ref

    async def _reaction(self, event):
        from .reactions import EMOJI
        owner=self.owner() or {}
        if not self.settings().get('reactions',True) or (event.get('user') or {}).get('id')!=owner.get('user_id'):return
        chat=(event.get('chat') or {}).get('id');mid=event.get('message_id')
        if chat!=(owner.get('chat_id') or owner.get('user_id')) and not self.groups.allowed(chat):return
        route=self.app.store.meta_get('telegram:message_routes',{}).get(str(chat)+':'+str(mid),{})
        try:ctx=self.app.agents.get(route.get('agent','main'))
        except HumanError:return
        ref=ctx.store.meta_get('telegram:refs',{}).get(str(chat)+':'+str(mid))
        if not ref:return
        old=[r.get('emoji') for r in event.get('old_reaction',[]) if r.get('type')=='emoji' and r.get('emoji') in EMOJI]
        new=[r.get('emoji') for r in event.get('new_reaction',[]) if r.get('type')=='emoji' and r.get('emoji') in EMOJI]
        if old==new:return
        key='telegram:reactions:'+ref['session']
        pending=ctx.store.meta_get(key,[])
        pending.append({'message':mid,'old':old,'new':new})
        ctx.store.meta_set(key,pending[-100:])
        ratings=ctx.store.meta_get('telegram:ratings',{})
        ratings[str(chat)+':'+str(mid)]=[x for x in new if x in ('👍','👎')]
        ctx.store.meta_set('telegram:ratings',dict(list(ratings.items())[-5000:]))
        ctx.bus.publish({'type':'reaction','session':ref['session'],'message_id':mid,'emoji':new})
        if self.settings().get('reaction_wake',False) and any(e in self.settings().get('wake_emoji',[]) for e in new if e not in old):
            chat=self.topics.for_session(ctx,ref['session']) or chat
            self._bg_task(self._owner_text(chat,None,'Хозяин отреагировал на сообщение.',reaction_session=ref['session'],reaction_wake=True,agent_ctx=ctx))

    async def _handle(self, u: dict) -> None:
        if 'edited_message' in u:
            from .telegram_events import edited
            await edited(self,u['edited_message'] or {})
            return
        if 'my_chat_member' in u:
            event=u['my_chat_member'] or {}
            await self.groups.membership(event)
            return
        if 'message_reaction' in u:
            await self._reaction(u['message_reaction'] or {})
            return
        if 'stopped_message_generation' in u:
            stopped = u['stopped_message_generation'] or {}
            owner = self.owner() or {}
            if (stopped.get('chat') or {}).get('id') == (owner.get('chat_id') or owner.get('user_id')):
                for live in self._live.values():
                    if str(stopped.get('draft_id')) == str(live.draft_id):
                        # Старый черновик ещё с кнопкой: прерываем только этот ход, стоп-кран не включаем.
                        from .telegram_events import record
                        record(live.app,live.sid,live.chat,'хозяин нажал кнопку Стоп черновика')
                        live.app.mind.stop(live.sid)
                        break
            return
        if "callback_query" in u:
            await self._callback(u["callback_query"])
            return
        msg = u.get("message") or {}
        frm = msg.get("from") or {}
        chat = msg.get("chat") or {}
        if not msg or frm.get('is_bot'):return
        group=None
        if chat.get('type')!='private':
            group=await self.groups.incoming(msg)
            if group is None:return
        from .telegram_rich_in import body
        text = body(msg).strip()
        owner = self.owner()
        if not owner:
            code = self.code[0] if self.code and self.code[1] > time.time() else None
            if code and code in text.upper():
                o = {"user_id": frm.get("id"), "chat_id": chat.get("id"), "name": " ".join(x for x in (frm.get("first_name"), frm.get("last_name")) if x),
                     "username": frm.get("username"), "bound_at": time.time()}
                self.app.store.meta_set(OWNER_META, o)
                self.code = None
                self.app.journal.write("channel", f"Telegram: хозяин привязан ({o['name'] or o['username']})", actor="owner", data={"user_id": o["user_id"]})
                self.app.bus.publish({"type": "channel", "channel": "telegram", "state": "bound"})
                await self.send(chat["id"], "Привязал. Теперь я твой и здесь. " + HELP)
                return
            await self._stranger(chat.get("id"), frm, "Я бот Бендера. Чтобы привязать — открой пульт → «Связь» → Telegram и пришли код оттуда.")
            return
        if frm.get("id") != owner.get("user_id"):
            await self._stranger(chat.get("id"), frm, "Я бот Бендера и отвечаю только своему хозяину.")
            return
        if chat.get('type')=='private':
            address=await self.topics.incoming(msg)
            if address is None:return
            chat={**chat,'id':address}
            msg={**msg,'chat':chat}
        if msg.get('media_group_id'):
            self._collect_album(msg)
            return
        try:ctx,sid=self.selected(chat['id'])
        except HumanError as error:
            await self.send(chat['id'],error.title+'. '+error.hint)
            return
        messages=msg.get('_album_messages') or [msg]
        if len(messages)>1:text='\n'.join(body(m) for m in messages).strip()
        try:attachments=await self.receive_media(ctx,messages,sid)
        except (HumanError,NetError,ValueError) as exc:
            await self.send(chat['id'],str(exc))
            return
        for item in messages:
            if item.get('_rich_skipped'):
                count,enabled=item['_rich_skipped']
                text+=('\n\n' if text else '')+f'[Медиа из поста не скачано: {count}. '+('Вложения выключены в настройках.]' if not enabled else f'Беру не больше {RICH_MEDIA_MAX} файлов и 20 МБ за сообщение.]')
        forwarded = any(msg.get(k) for k in ("forward_origin", "forward_from", "forward_from_chat", "forward_sender_name", "forward_date"))
        tainted = forwarded or bool(msg.get("external_reply"))
        if not tainted and text.split('@',1)[0].strip()=='/avatar' and attachments:
            from ..tools.registry import ToolContext
            try:
                if len(attachments)!=1 or attachments[0]['kind']!='photo':
                    raise HumanError('telegram_avatar','Пришли одно фото с подписью /avatar','Для профиля нужен JPG, не альбом.')
                await self.profile.apply(ToolContext(ctx,actor='owner',session_id=sid),{},attachments[0]['path'],reviewed=True)
                await self.send(chat['id'],'Аватар общего бота обновлён.')
            except HumanError as error:await self.send(chat['id'],error.title+'. '+error.hint)
            from .telegram_events import record
            record(ctx,sid,chat['id'],'хозяин вызвал /avatar')
            self.remember(ctx,chat['id'],msg.get('message_id'),sid,'/avatar','in',source_text='/avatar',command=True)
            return
        if any(item['kind']!='voice' for item in attachments):tainted=True
        if msg.get("voice"):
            try:
                info=msg["voice"]
                ctx.voice.require("stt")
                from ..tools.registry import ToolContext
                from ..core.pathutil import relative_path
                await ToolContext(ctx,actor="owner").check("audio.transcribe","Telegram","загрузить голосовое сообщение хозяина")
                from pathlib import Path
                audio=Path(next(i['path'] for i in attachments if i['kind']=='voice')).read_bytes()
                from ..voice.input import prepare,attachment,transcript_text
                result=await prepare(ctx,audio,ext='ogg',seconds=info.get('duration',0),session=sid,from_owner=not tainted)
                text=transcript_text(result,seconds=info.get('duration',0),from_owner=not tainted)
                if msg.get('caption'):text+='\n\nПодпись к голосовому: '+str(msg['caption'])
                if result['native']:
                    old=next(item for item in attachments if item['kind']=='voice')
                    native=await attachment(ctx,result,session=sid,existing=old,source='telegram')
                    attachments=[native if item is old else item for item in attachments]
                    tainted=True  # Сохранённый аудиофайл — внешние данные, в отличие от проверяемого текста хозяина.
                if result.get('notice'):await self.send(chat['id'],result['notice'])
            except (HumanError,NetError,ValueError) as exc:
                from .media import describe
                await self.send(chat["id"],str(exc)+'\n'+describe(attachments))
                return
        if attachments and not msg.get('voice'):
            from ..voice.files import format_of,prepare as audio_file
            from .video_in import KINDS as VISUAL,look
            processed=[]
            for item in attachments:
                if item['kind']=='audio' or item['kind']=='document' and format_of(item):
                    tainted=True
                    try:
                        result=await audio_file(ctx,item,session=sid);item=result['item'];text+=('\n\n' if text else '')+result['text']
                        if result['notice']:await self._media_notice(ctx,chat['id'],sid,result['notice'])
                    except (HumanError,NetError,ValueError) as error:
                        text+=('\n\n' if text else '')+'[Аудиофайл не расшифрован: '+str(error)+']\nФайл: '+item['path']
                        await self._media_notice(ctx,chat['id'],sid,str(error)+'\nФайл: '+item['path'])
                elif item['kind'] in VISUAL:
                    tainted=True
                    seen=await look(self,ctx,item,sid);item=seen['item'];text+=('\n\n' if text else '')+seen['text']
                    processed+=seen['extra']
                    if seen['notice']:await self._media_notice(ctx,chat['id'],sid,seen['notice'])
                processed.append(item)
            attachments=processed
        if attachments and not text:text='Прислано вложение.'
        if not text:
            await self.send(chat["id"],"Пришли текст или голосовое сообщение; голос включается в пульте → Настройки → Голос.")
            return
        source_text=body(msg)
        if forwarded:
            from ..tools.web import untrusted
            text=untrusted('пересланное сообщение Telegram',text)  # чужие слова целиком, вместе с расшифровкой голоса
        cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else ""
        if tainted or any(a['kind']!='voice' for a in attachments):
            cmd = ""  # пересланная речь/текст не является командой хозяина
        if cmd:
            from ..tools.registry import ToolContext
            await self.authorize_group(ToolContext(ctx,actor='owner',session_id=sid),chat['id'],'ответить на явную команду хозяина в группе')
            from .telegram_events import dispatch
            if await dispatch(self.commands,ctx,chat['id'],sid,cmd[1:],text.partition(' ')[2].strip()):
                self.remember(ctx,chat['id'],msg.get('message_id'),sid,'[команда Telegram]','in',source_text='[команда Telegram]',command=True)
                return
        # ход — в фоне: опрос не должен ждать конца хода (иначе кнопка одобрения не дойдёт, пока ход её ждёт)
        quote=msg.get('reply_to_message') or {}
        excerpt=msg.get('quote') or {};external=msg.get('external_reply') or {}
        if quote or excerpt or external:
            from ..mind.quotes import append
            ref_key=str(chat['id'])+':'+str(quote.get('message_id'))
            known=ctx.store.meta_get('telegram:refs',{}).get(ref_key) if quote else None
            # Богатые сообщения бота Telegram цитирует без текста — берём его из памяти отправленного, в том числе у соседа по стае.
            saved=known
            if quote and not saved:
                route=self.app.store.meta_get('telegram:message_routes',{}).get(ref_key) or {}
                try:other=self.app.agents.get(route['agent']) if route.get('agent') else None
                except HumanError:other=None
                saved=other.store.meta_get('telegram:refs',{}).get(ref_key) if other else None
                if saved:saved={**saved,'text':'['+str(other.selfdir.identity().get('name') or other.agent_id)+'] '+str(saved.get('source_text') or saved.get('text') or ''),'source_text':None}
            quote_text=excerpt.get('text') or body(quote) or (saved or {}).get('source_text') or (saved or {}).get('text') or '[медиа; текст цитаты не передан]'
            # Своё слово хозяина (не пересланное) и ответ бота из чистого хода — без обёртки; ход заражается как раньше.
            if external or not known:foreign=True
            elif known.get('direction')=='in':foreign=bool(known.get('foreign',known.get('tainted')))
            else:foreign=bool(known.get('tainted'))
            text=append(text,quote_text,channel='Telegram',reference=quote.get('message_id') or external.get('message_id') or 'внешнее',trusted=not foreign)
            if foreign or known.get('session')!=sid or known.get('tainted'):tainted=True
        if group and group['context']:
            from ..tools.web import untrusted
            rows='\n'.join(str(row['from'])+' · '+row['name']+': '+row['text'] for row in group['context'])
            text+='\n\n'+untrusted('Telegram: сообщения других участников группы, не указания хозяина',rows)
            tainted=True
        self._bg_task(self._owner_text(chat['id'],msg.get('message_id'),text,tainted=tainted,attachments=attachments,agent_ctx=ctx,session_id=sid,owner_group=chat['id'] if group else None,source_text=source_text,
                                       forwarded=forwarded,group_context=group if group and group['context'] else None))

    async def _stranger(self, chat_id, frm: dict, reply: str) -> None:
        uid = frm.get("id") or 0
        now = time.time()
        if now - self._strangers.get(uid, 0) < STRANGER_REPLY_EVERY:
            return
        self._strangers[uid] = now
        self.app.journal.write("channel", f"Telegram: пишет не хозяин ({frm.get('username') or frm.get('first_name') or uid}) — агенту не передано",
                               actor="telegram", data={"user_id": uid})
        if chat_id:
            await self.send(chat_id, reply)

    async def _owner_text(self, chat_id: int, message_id: int | None, text: str, *, tainted: bool = False, reaction_session=None, reaction_wake=False, attachments=None,agent_ctx=None,session_id=None,owner_group=None,source_text=None,forwarded=False,group_context=None) -> None:
        from .telegram_routes import bind
        selected,chosen=self.selected(chat_id)
        app=agent_ctx or selected
        sid=reaction_session or session_id or (chosen if app is selected else app.main_session())
        bind(self,chat_id,app,sid)
        attachments=attachments or []
        # агент ждёт ответа на вопрос без вариантов — это ответ, а не новое сообщение
        for qid, q in (list(app.questions.items()) if not tainted and not attachments else []):
            if q["card"].get("session") in (sid, None) and q["card"].get("agent_id",app.agent_id)==app.agent_id and not q["card"].get("options"):
                app.answer(qid, text)
                return
        typing_done=False
        typing = asyncio.ensure_future(self._typing(chat_id,alive=lambda:not typing_done)) if self.settings().get("live", "full") == "off" or owner_group is not None else None
        self._reply_to[self.route_key(app,sid)] = message_id
        try:
            if message_id:self.remember(app,chat_id,message_id,sid,text,'in',source_text=source_text,tainted=tainted,foreign=forwarded)
            key='telegram:reactions:'+sid
            pending=app.store.meta_get(key,[])
            if pending:
                text+='\n\nРеакции хозяина с прошлого хода:\n'+'\n'.join('Хозяин поставил '+(' '.join(r['new']) or 'ничего (снял реакцию)')+' на #'+str(r['message']) for r in pending)
                app.store.meta_set(key,[])
            from . import media
            parts=media.content(text,attachments,vision=app.settings.get('attachments','vision',True),audio=app.settings.get('voice','enabled',False) and app.settings.get('voice','stt_mode') in ('native','both')) if attachments else None
            started = time.monotonic()
            # Реплики участников уходят в историю вместе с этим сообщением: из буфера — только отданные, и только если ход их принял.
            taken=self.groups.take(group_context) if group_context else None
            try:
                res = await app.mind.say(sid, text, content=parts, channel="telegram", actor="reaction" if reaction_wake else "owner",tainted=tainted or reaction_wake or any(a['kind']!='voice' for a in attachments),
                                         unattended=reaction_wake,message_meta={'telegram_message_id':message_id,'telegram_chat_id':int(chat_id),'attachments':attachments,'telegram_route':{'chat':int(chat_id),'thread':getattr(chat_id,'thread',None)},'telegram_owner_requested':owner_group,**({'forwarded':True} if forwarded else {})})
            except HumanError:
                if taken:self.groups.give_back(group_context,taken)
                raise
            if res is None:  # встало в идущий ход — ждём его конца
                t = app.mind.turns.get(sid)
                busy = 'ход уже заканчивался'
                if t and t.task and message_id and not t.task.done():
                    self._bg_task(self._seen(chat_id, message_id))
                    busy = self._busy(app, t, sid)
                res = await t.task if t and t.task else None
                waited = time.monotonic() - started
                if t and waited >= LATE and message_id:
                    app.journal.write('channel', f'Telegram: сообщение хозяина ждало конца идущего хода {int(waited)} с; когда пришло — {busy}', actor='telegram',
                                      data={'waited': int(waited)})
        finally:
            typing_done=True
            if typing:
                typing.cancel()
                await asyncio.gather(typing,return_exceptions=True)
        # Обычные и вставленные в ход ответы доставляются одним событием answer.
        # Команды навыков возвращаются сразу, не начиная ход.
        if res and not res.get("steps") and text.startswith(("/научись", "/убрать-навык")):
            await self._answer(chat_id, sid, res, reply_to=message_id,agent_ctx=app)

    async def _answer(self, chat_id, sid, res, *, label="", reply_to=None,agent_ctx=None):
        app = agent_ctx or self.app
        res = res or {}
        from ..core.dates import strip_labels
        reply = strip_labels((res.get("text") or "").strip())
        err = res.get("error")
        if not err and (res.get('sent_all') or reply and reply in res.get('channel_sent',[])):return
        reply_to=None
        explicit=re.match(r'^↩\s*#(\d+)\s*',reply)
        if explicit:
            number=int(explicit[1]);reply=reply[explicit.end():]
            try:self.require_ref(app,chat_id,number,sid);reply_to=number
            except HumanError:app.journal.write('channel','Telegram: неизвестный номер ответа, отправлен обычным сообщением')
        if err and err.get("code") != "stopped":
            reply = (reply + "\n\n" if reply else "") + f"⚠ {err.get('title')}" + (f"\n{err.get('hint')}" if err.get("hint") else "")
        if reply:
            owner=self.owner() or {};private=owner.get('chat_id') or owner.get('user_id')
            if chat_id!=private:
                from ..guard.policy import Action
                action=Action('msg.send','telegram:group:'+str(chat_id),actor='system',tool='telegram_reply',tainted=bool(res.get('tainted')),summary='Ответ в группу: '+reply[:1600],detail={'telegram_owner_requested':res.get('telegram_owner_requested'),'preview':reply})
                decision=await app.guard.check(action,session_id=sid)
                if not decision.allowed:return
            if label:
                reply = label + "\n\n" + reply
            if app.settings.get("voice","enabled",False) and app.settings.get("voice","telegram_reply",False):
                try:
                    result=await app.voice.speak(reply,session_id=sid,format="opus")
                    mid=await self.send_voice(chat_id,result["audio"])
                    self.remember(app,chat_id,mid,sid,reply,'out',tainted=bool(res.get('tainted')))
                    return
                except (HumanError,TelegramError,NetError) as exc:
                    app.journal.write("voice","Telegram: голос недоступен, отправлен текст",data={"reason":str(exc)})
                    reply += "\n\nГолос недоступен, отвечаю текстом: " + str(exc)
            reply=self.signed(app,reply)
            mid=await self.send(chat_id, reply, reply_to=reply_to)
            self.remember(app,chat_id,mid,sid,reply,'out',tainted=bool(res.get('tainted')))
            row=app.store.one("SELECT id FROM messages WHERE session_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",(sid,))
            if row and mid:
                app.store.x("UPDATE messages SET meta=json_set(meta,'$.telegram_message_id',?,'$.telegram_chat_id',?) WHERE id=?",(mid,chat_id,row['id']))

    async def send_voice(self, chat_id, audio):
        result=await self.api('sendVoice',{'chat_id':chat_id,'voice':'attach://voice'},files={'voice':{'name':'voice.ogg','mime':'audio/ogg','bytes':audio}})
        return (result or {}).get('message_id')

    async def _typing(self, chat_id: int, *, alive=None) -> None:
        try:
            while not self._stopping and (alive is None or alive()):
                try:
                    await self.api("sendChatAction", {"chat_id": chat_id, "action": "typing"}, timeout=10)
                except TelegramError:
                    pass
                if self._stopping or alive is not None and not alive():return
                await asyncio.sleep(4.5)
        except asyncio.CancelledError:
            pass

    async def _callback(self,cq:dict)->None:
        message=cq.get('message') or {};chat=message.get('chat') or {}
        if chat.get('id') is not None:
            cq={**cq,'message':{**message,'chat':{**chat,'id':self.topics.address(chat['id'],message.get('message_thread_id'))}}}
        from .telegram_events import button,record
        try:notice=button(self,cq)
        except HumanError:notice=None
        before=None
        if notice:
            try:
                selected,selected_sid=self.selected(notice[2]);before=(selected.agent_id,selected_sid)
            except HumanError:pass
            ctx,sid,address,label=notice
            record(ctx,sid,address,label)
        try:await self._callback_action(cq)
        finally:
            if notice:
                ctx,sid,address,label=notice
                try:selected,new_sid=self.selected(address)
                except HumanError:selected=None
                if selected and before is not None and (selected.agent_id,new_sid)!=before and (selected.agent_id,new_sid)!=(ctx.agent_id,sid):
                    record(selected,new_sid,address,'хозяин выбрал этот разговор кнопкой')

    async def _callback_action(self, cq: dict) -> None:
        owner = self.owner()
        frm = cq.get("from") or {}
        data = str(cq.get("data") or "")
        ok_text = ""
        if not owner or frm.get("id") != owner.get("user_id"):
            ok_text = "Это кнопки хозяина."
        elif await self.commands.callback(cq):
            return
        elif data.startswith("sk:"):
            from ..skills.notices import archive
            from ..tools.registry import ToolContext
            notice=self.app.store.meta_get("skill.notice."+data[3:])
            if not notice:
                ok_text="Уведомление уже закрыто"
            else:
                try:
                    notice_ctx=self.app.agents.get(notice.get("agent_id","main"))
                    ok_text=await archive(notice_ctx,notice["name"],ToolContext(notice_ctx,actor="owner"),notice.get("hash"))
                    self.app.store.meta_set("skill.notice."+data[3:],None)
                except HumanError as exc:
                    ok_text=exc.title+". "+exc.hint
        elif data.startswith("ap:"):
            _, pid, choice = data.split(":", 2)
            done = self.app.guard.respond(pid, choice, by="owner")
            ok_text = {"once": "Разрешено", "task": "Разрешено в этом разговоре", "always": "Разрешено всегда", "deny": "Отказано"}.get(choice, "") if done else "Уже решено"
        elif data.startswith("q:"):
            _, qid, idx = data.split(":", 2)
            q = self.app.questions.get(qid)
            opts = (q or {}).get("card", {}).get("options") or []
            if q and idx.isdigit() and int(idx) < len(opts):
                self.app.answer(qid, opts[int(idx)])
                ok_text = f"Ответ: {opts[int(idx)]}"
            else:
                ok_text = "Вопрос уже закрыт"
        try:
            await self.api("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": ok_text[:190]})
            m = cq.get("message") or {}
            if m and ok_text and owner and frm.get("id") == owner.get("user_id"):
                await self.api("editMessageReplyMarkup", {"chat_id": (m.get("chat") or {}).get("id"), "message_id": m.get("message_id"), "reply_markup": {"inline_keyboard": []}})
                await self.send((m.get("chat") or {}).get("id"), f"→ {ok_text}")
        except TelegramError:
            pass

    def _on_settings(self, ev: dict) -> None:
        if ev.get('type')=='settings' and ev.get('group')=='telegram' and set(ev.get('keys',[])) & {'commands','command_languages'}:
            if not self._stopping:self.commands.start()
        if ev.get('type')=='settings' and ev.get('group')=='telegram' and ('keys' not in ev or set(ev['keys']) & {'groups','groups_enabled'}):
            self.groups.drop_stale()
        if ev.get("type") == "settings" and ev.get("group") == "telegram" and ("keys" not in ev or set(ev["keys"]) & {"enabled", "api_base"}):
            # включили, выключили, сменили адрес — переподключиться (отдельной задачей: stop() её не отменяет)
            if getattr(self.app,'_closed',False):return
            if self._restarting and not self._restarting.done():return  # Один перезапуск читает последние настройки.
            self._restarting = asyncio.ensure_future(self.restart())
            self._restarting.add_done_callback(self._task_done)

    def _stream(self, ev, chat,agent_ctx=None):
        from ..mind.loop import home_channel
        ctx=agent_ctx or self.app;sid=ev.get('session')
        owner=self.owner() or {}
        if chat!=(owner.get('chat_id') or owner.get('user_id')):return  # Мысли в группах не публикуются.
        row=ctx.store.session(sid) if sid else None
        if not row or row['kind'] not in ('main','chat','imported'):return
        key=self.route_key(ctx,sid)
        if home_channel(ctx,sid)!='telegram':
            live=self._live.pop(key,None)
            if live:self._bg_task(self._close_live(live))
            return
        live=self._live.get(key)
        settings=ctx.settings.group('telegram')
        if ev.get('type')!='answer' and not live and settings.get('live','full')!='off' and ctx.mind.running(sid):
            turn=ctx.mind.turns[sid]
            if turn.channel=='telegram' or settings.get('notify',True):
                from .telegram_live import Live
                draft_id=turn.state.setdefault('telegram_draft',pysecrets.randbelow(2**52-1)+1)
                live=self._live[key]=Live(self,chat,sid,draft_id,agent_ctx=ctx)
        if live:live.feed(ev)

    async def _finish_answer(self, live, chat, ev, reply_to, deliver,agent_ctx=None):
        if live:await live.finish()
        try:
            if not deliver or self.state=='off':return
            for delay in (*ANSWER_RETRY,None):
                try:
                    await self._answer(chat,ev['session'],ev.get('result'),label=ev.get('label',''),reply_to=reply_to,agent_ctx=agent_ctx)
                    return
                except TelegramError as error:
                    # Сеть моргнула — ответ не выбрасываем, а пробуем ещё.
                    if error.code!='tg_net' or delay is None or self._stopping:raise
                    await asyncio.sleep(delay)
        finally:
            if live:await self._end_draft(live)

    async def _close_live(self, live):
        await live.finish()
        await self._end_draft(live)

    async def _end_draft(self, live):
        """Черновик Telegram живёт ~30 с и держит у хозяина «обработку» вместо кнопки отправки. API не умеет
        удалить черновик — его закрывает только обычное сообщение бота. Если после последнего черновика
        сообщения не было (агент всё отправил тулом, ответ пустой или ушёл в другой канал), шлём тихое
        «✓» и сразу удаляем: поле ввода освобождается, в чате ничего не остаётся."""
        if not live.drafted_at or self.state=='off' or self._stopping:return
        try:key=(int(live.chat),getattr(live.chat,'thread',None))
        except (TypeError,ValueError):return
        if self._sent_at.get(key,0)>=live.drafted_at:return
        try:
            result=await self.api('sendMessage',{'chat_id':live.chat,'text':'✓','disable_notification':True},timeout=8)
            mid=(result or {}).get('message_id') if isinstance(result,dict) else None
            if mid:await self.api('deleteMessage',{'chat_id':live.chat,'message_id':mid},timeout=8)
        except TelegramError as error:
            live.app.journal.write('channel','Telegram: не удалось закрыть черновик хода',data={'error':error.title[:200]})

    # исходящие по событиям ядра

    def _on_event(self, ev: dict) -> None:
        aid=ev.get('agent_id','main')
        if ev.get('type') in ('approval','question','notify') and self._userbot_serves(ev):return
        try:ctx=self.app if aid=='*' else self.app.agents.get(aid)
        except HumanError:
            if ev.get('type') not in ('approval_done','question_done'):return
            ctx=self.app
        owner = self.owner()
        # Не «ok» бывает и на секунды сетевого сбоя опроса: ответ, карточка одобрения или напоминание
        # всё равно пробуют уйти, а не пропадают молча.
        if not owner or self.state == "off":
            return
        private=owner.get('chat_id') or owner.get('user_id')
        route=ev.get('telegram_route') or (ev.get('result') or {}).get('telegram_route') or {}
        chat=self.topics.address(route['chat'],route.get('thread')) if route.get('chat') else self.destination(ctx,ev.get('session'))
        if chat!=private and (ev.get('type')!='answer' or (ev.get('result') or {}).get('telegram_owner_requested')!=chat):chat=private
        s=ctx.settings.group('telegram')
        t=ev.get('type');key=self.route_key(ctx,ev.get('session'))
        # Групповой ход не показывает черновики даже в личке хозяина.
        if not route.get('chat') or route.get('chat')==private:self._stream(ev,chat,ctx)
        reply_to=self._reply_to.pop(key,None) if t=='answer' else None
        if t == "answer":
            live = self._live.pop(key, None)
            deliver = ev.get('channel') == 'telegram' and (ev.get('source') == 'telegram' or s.get('notify', True))
            if live or deliver: self._bg_task(self._finish_answer(live, chat, ev, reply_to, deliver,ctx))
        elif t == "message" and s.get("notify",True) and ev.get("message",{}).get("kind")=="skill":
            self._bg_task(self._skill_notice(chat,ev["message"],ctx))
        elif t == "notify" and s.get("notify", True) and ev.get("channel", "telegram") == "telegram":
            icon = {"remind": "⏰", "alarm": "🚨", "job": "📋", "reflex": "🔔", "pulse": "💓", "mail": "📧", "hook": "🪝", "update": "🔄"}.get(ev.get("kind"), "•")
            text = str(ev.get("text") or "")
            if not text.startswith(icon):
                text = f"{icon} {text}"
            self._bg_task(self._notice(chat, text, ctx, ev.get('session'), agent=aid!='*'))
        elif t == "approval" and s.get("approvals", True):
            c = ev.get("card") or {}
            row = [{"text": "Да, один раз", "callback_data": f"ap:{c['id']}:once"}] + ([{"text": "В этом разговоре", "callback_data": f"ap:{c['id']}:task"}] if c.get("can_session", True) else [])
            row2 = ([{"text": ("Всегда: " + str(c.get("remember") or "такое"))[:60], "callback_data": f"ap:{c['id']}:always"}] if c.get("can_remember") else []) + [{"text": "Нет", "callback_data": f"ap:{c['id']}:deny"}]
            text = f"⚠ Предохранитель спрашивает: **{c.get('summary') or c.get('what')}**\n{c.get('reason') or ''}" + ("\nВ этом ходе агент читал недоверенное." if c.get("tainted") else "")
            self._bg_task(self._card(c["id"], chat, self._card_signed(c, text), [row, row2]))
        elif t == "question" and s.get("approvals", True):
            c = ev.get("card") or {}
            opts = c.get("options") or []
            buttons = [[{"text": o[:60], "callback_data": f"q:{c['id']}:{i}"}] for i, o in enumerate(opts[:8])]
            self._bg_task(self._card(c["id"], chat, self._card_signed(c, f"❓ {c.get('question')}" + ("" if opts else "\n(ответь сообщением)")), buttons or None))
        elif t in ("approval_done", "question_done"):
            ref = self._cards.pop(str(ev.get("id")), None)
            if ref:
                self._bg_task(self.api("editMessageReplyMarkup", {"chat_id": ref[0], "message_id": ref[1], "reply_markup": {"inline_keyboard": []}}))

    def _userbot_serves(self, ev):
        """Хозяин направил одобрения, вопросы и уведомления этого агента в чат его аккаунта Telegram — общий бот их не дублирует."""
        aid=(ev.get('card') or {}).get('agent_id') or ev.get('agent_id','main')
        ctx=self.app if aid=='main' else self.app.agents.loaded.get(aid)
        bot=getattr(ctx,'_userbot',None)
        return bool(bot and bot.serving())

    def badge(self, ctx):
        """«🦊: » — начало сообщения агента (эмодзи у каждого агента стаи свой); '' — подпись выключена или агента нет (общее сообщение стаи)."""
        if ctx is None or not ctx.settings.get('telegram','agent_label',True):return ''
        from ..agents.badge import emoji
        try:return emoji(ctx)+': '
        except Exception:return ''  # noqa: BLE001 — знак не должен мешать доставке

    def signed(self, ctx, text):
        head=self.badge(ctx);text=str(text)
        return text if not head or text.startswith(head) else head+text

    async def _notice(self, chat, text, ctx, sid=None, *, agent=True):
        """Уведомление запоминается как сказанное агентом: ответ на него придёт с текстом, а не «медиа»."""
        if agent:text=self.signed(ctx,text)
        mid=await self.send(chat,text)
        # Уведомления несут тему письма, тело вебхука, вывод рефлекса: при цитате это чужой текст.
        self.remember(ctx,chat,mid,sid or ctx.main_session(),text,'out',tainted=True)

    async def _skill_notice(self,chat,message,agent_ctx=None):
        data=message.get("data") or {}
        if not data.get("name"):return
        from ..core.ids import new_id
        token=new_id()[-12:].lower()
        self.app.store.meta_set("skill.notice."+token,{"name":data["name"],"hash":data.get("hash"),"agent_id":(agent_ctx or self.app).agent_id})
        text=self.signed(agent_ctx or self.app,str(message.get("content") or "💾 Навык: "+data["name"]))
        mid=await self.send(chat,text,buttons=[[{"text":"Убрать навык","callback_data":"sk:"+token}]])
        ctx=agent_ctx or self.app;self.remember(ctx,chat,mid,ctx.main_session(),text,'out')

    def _card_signed(self, card, text):
        try:ctx=self.app.agents.get(card['agent_id'],archived=True) if card.get('agent_id') else None
        except HumanError:ctx=None
        return self.signed(ctx,text)

    async def _card(self, cid: str, chat: int, text: str, buttons) -> None:
        mid = await self.send(chat, text, buttons=buttons)
        if mid:
            self._cards[cid] = (chat, mid)
