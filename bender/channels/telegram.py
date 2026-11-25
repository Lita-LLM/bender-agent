"""Telegram: Бендер в кармане. Bot API, долгий опрос своим HTTP-клиентом — без библиотек и без вебхука
(не нужен белый адрес). Один запрос висит до 50 секунд: в простое — ни ЦП, ни записи на диск.

- Привязка хозяина одноразовым кодом из пульта: `/start КОД`. Чужие сообщения агенту не идут — бот
  вежливо отвечает раз в час и пишет в бортжурнал.
- Сообщение хозяина → главная сессия (один разговор на все каналы), ответ — сюда же. Пока агент думает —
  «печатает…». Пришло во время чужого хода — встаёт в него, ответ придёт, когда ход закончится.
- Одобрения Предохранителя и вопросы агента — кнопками. Напоминания, тревоги и итоги задач сердца —
  сообщениями (выключается).
- Команды: /stop — прервать работу агента, /estop — стоп-кран (блокирует всё до /resume), /resume — снять стоп, /status — самочувствие, /new — новая страница, /help.
- Фото/файлы и голос ограничены настройками; команды и кнопки — служебные факты, правки — было → стало.
- Пересланное — с источником и исходной датой, пачкой (telegram_forward.py); всегда недоверенное и не команда.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
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
SLOW_ANSWER = 8.0     # дольше до первого слова модели — строка о медленном ответе в журнал
ORDER_WAIT = 30.0     # сколько сообщение чата ждёт обработки предыдущего, прежде чем пойти само
ANSWER_RETRY = (5.0, 10.0, 20.0)  # повторы ответа после сетевого сбоя
PAUSE_NOTICE = 30.0   # с какой паузы Telegram за частые сообщения показывать её в пульте и журнале
LATE = 60.0           # опоздание сообщения, о котором чёрный ящик пишет в журнал
NET_RETRY_MAX = 30.0  # самая долгая пауза между попытками связаться после сбоя сети (кроме неверного токена)
POLL_GAP = 70.0       # опрос держится до 50 с; дольше без ответа — значит, сообщения не забирались
FORWARD_BATCH_MAX = 50  # пачка пересланного: Telegram пересылает до 100 разом; больше — следующей пачкой
COMMENT_WINDOW = 1.5   # комментарий хозяина пришёл не раньше стольких секунд до пересланного — это подпись к нему
URGENT = {'stop', 'стоп', 'estop', 'resume', 'продолжить', 'status', 'help', 'start', 'whoami', 'delivery', 'mode', 'режим'}  # вне очереди чата

HELP = ("Я — Бендер. Пиши как обычно — отвечу.\n"
        "/stop — прервать работу агента\n/delivery — сообщение во время работы: вставить в ход, прервать или после хода\n/estop — стоп-кран: остановить всё и не давать действовать до /resume\n/resume — снять стоп\n/status — как я себя чувствую\n/new — новая страница разговора (память остаётся)\n/убрать-навык имя — убрать навык из активных\n/help — это сообщение")


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
        s = re.sub(r"\[([^\]\n]+)\]\((https?://[^)\s]+)\)", lambda m: f'<a href="{m.group(2).replace(chr(34), "&quot;")}">{m.group(1)}</a>', s)  # текст уже экранирован: & не удваивается
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


class SendLock:
    """Замок одного места чата (чат, тема): чья задача сейчас отправляет логическое сообщение."""
    __slots__ = ('lock', 'owner')

    def __init__(self):
        self.lock = asyncio.Lock()
        self.owner = None


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
        self._live = {}
        self._live_next = {}         # (чат, тема) → когда можно следующий черновик или правку статуса
        self._draft_holder = {}      # (чат, тема) → Live, чей черновик сейчас открыт: у Telegram одно место черновика
        self._send_locks = {}        # (чат, тема) → SendLock: куски одного ответа идут подряд, без кусков соседа
        self._status_messages = {}   # (чат, сообщение) → (агент, разговор): статус хода, маршрут без текста (в памяти)
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
        self._recent = {}            # (чат, тема) → последнее простое сообщение хозяина: комментарий к пересланному следом
        self._arrived = {}           # (чат, тема) → когда пришло последнее слово хозяина (отклик по этапам, Live)
        self.timing = collections.deque(maxlen=20)  # отклик последних ответов: где ждали
        self._flushing = {}          # (чат, тема) → пачки, которые уже разбираются после тишины
        self._commands = None
        self._profile = None
        self._groups = None
        self._topics = None
        from .telegram_refs import migrate_host
        migrate_host(app.store)  # таблицы учёта сообщений — на старте, вместе с прочими миграциями

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

    @staticmethod
    def slot(chat):
        """(чат, тема) — одно место черновика и одна очередь отправки. Адрес темы — int с .thread."""
        try:return (int(chat),getattr(chat,'thread',None))
        except (TypeError,ValueError):return None

    @contextlib.asynccontextmanager
    async def sending(self, chat):
        """Одна логическая отправка (все куски длинного ответа, файл и карточка, статус хода) идёт подряд: куски
        соседа по стае не вклиниваются. Та же задача входит повторно без ожидания. Модель под замком не зовётся;
        долгую паузу Telegram (429) ждут без замка — соседи всё равно ждут ту же паузу чата."""
        slot=self.slot(chat)
        if slot is None:
            yield
            return
        entry=self._send_locks.get(slot)
        if entry is None:entry=self._send_locks[slot]=SendLock()
        task=asyncio.current_task()
        if entry.owner is task:
            yield
            return
        await entry.lock.acquire()
        entry.owner=task
        try:
            yield
        finally:
            if entry.owner is task:
                entry.owner=None
                entry.lock.release()

    async def _pause_wait(self, chat, delay):
        if delay<=0:return
        entry=self._send_locks.get(self.slot(chat));task=asyncio.current_task()
        if entry is None or entry.owner is not task or delay<PAUSE_NOTICE:
            await asyncio.sleep(delay)
            return
        entry.owner=None;entry.lock.release()
        try:await asyncio.sleep(delay)
        finally:
            await entry.lock.acquire();entry.owner=task

    @staticmethod
    def _reply(reply_to, quote=None):
        params={'message_id':reply_to,'allow_sending_without_reply':True}
        if quote:params['quote']=quote
        return params

    def _quote_refused(self, error, payload):
        """Telegram не нашёл фрагмент цитаты в сообщении: повторить без фрагмента (ответ всё равно цитатой на сообщение)."""
        params=payload.get('reply_parameters')
        if not isinstance(params,dict) or not params.get('quote') or error.data.get('code')!=400 or 'quote' not in error.title.lower():return False
        params.pop('quote',None)
        self.app.journal.write('channel','Telegram не принял фрагмент цитаты — ответ отправлен без фрагмента',actor='telegram',data={'error':error.title[:200]})
        return True

    def _wake_drafts(self, chat, skip=None):
        """Обычное сообщение бота закрывает черновик в этом месте чата: открытый черновик уйдёт снова. Статус-сообщения
        не трогаем — их правка тем же текстом Telegram всё равно отклонит."""
        slot=self.slot(chat)
        for live in list(self._live.values()):
            if live is not skip and not live.closed and not live.fallback and self.slot(live.chat)==slot:
                live.version+=1;live.wake.set()

    def mark(self, ctx):
        """«🦊» — знак агента без двоеточия: подпись голосового и продолжения длинного ответа; '' — подпись выключена."""
        head=self.badge(ctx)
        return head[:-2] if head.endswith(': ') else head.strip()

    async def rich_request(self,payload):
        chat=payload['chat_id'];attempt=0
        while True:
            delay=self._retry_until.get(chat,0)-time.monotonic()
            if delay>0:await self._pause_wait(chat,delay)
            try:return await self.api('sendRichMessage',payload)
            except TelegramError as error:
                if self._quote_refused(error,payload):continue
                if error.data.get('code')!=429 or attempt>=3:raise
                attempt+=1
                try:delay=max(1,min(86400,float(error.data.get('retry_after') or 1)))
                except (TypeError,ValueError):delay=1
                self.pause(chat,delay)

    async def send(self, chat_id: int, text: str, *, buttons=None, reply_to=None, quote=None, ids=None, agent=None):
        """Отправить (Rich, иначе HTML кусками). Возвращает номер последнего сообщения; ids — список, в который
        дописываются номера всех кусков (маршрут у каждого). agent — чьё сообщение: при подписи агентов знак стоит
        и на продолжениях («🦊 (2/3)»). quote — точный фрагмент сообщения reply_to; не принят — ответ без фрагмента."""
        from .telegram_events import capture
        capture(chat_id,text)
        async with self.sending(chat_id):
            return await self._send(chat_id,text,buttons=buttons,reply_to=reply_to,quote=quote,ids=ids,agent=agent)

    async def _send(self, chat_id, text, *, buttons=None, reply_to=None, quote=None, ids=None, agent=None):
        from .telegram_rich import parts,RichFormatError
        if not self.settings().get('rich',True) or self.capability('sendRichMessage') is False:
            return await self._send_plain(chat_id,text,buttons=buttons,reply_to=reply_to,quote=quote,ids=ids,agent=agent)
        try:prepared=parts(text)
        except RichFormatError:
            return await self._send_plain(chat_id,text,buttons=buttons,reply_to=reply_to,quote=quote,ids=ids,agent=agent)
        last=None;mark=self.mark(agent) if agent is not None else ''
        for n,(original,markdown) in enumerate(prepared):
            if n and mark:markdown=mark+' ('+str(n+1)+'/'+str(len(prepared))+')\n\n'+markdown
            payload={'chat_id':chat_id,'rich_message':{'markdown':markdown,'skip_entity_detection':True}}
            if n==0 and reply_to:payload['reply_parameters']=self._reply(reply_to,quote)
            if n==len(prepared)-1 and buttons:payload['reply_markup']={'inline_keyboard':buttons}
            try:
                result=await self.rich_request(payload)
                self.set_capability('sendRichMessage',True)
                last=(result or {}).get('message_id')
                if ids is not None and isinstance(last,int) and not isinstance(last,bool):ids.append(last)
            except TelegramError as error:
                if error.data.get('code')==429:raise
                if self.unsupported(error):self.set_capability('sendRichMessage',False)
                elif error.data.get('code') not in (400,403):raise
                # Не повторяем уже доставленные части; исходный markdown остаётся для HTML-пути.
                rest=''.join(source for source,_ in prepared[n:])
                return await self._send_plain(chat_id,rest,buttons=buttons,reply_to=reply_to if n==0 else None,quote=quote if n==0 else None,ids=ids,agent=agent,offset=n)
        self._wake_drafts(chat_id)
        return last

    async def send_plain(self, chat_id: int, text: str, *, buttons: list[list[dict]] | None = None, reply_to: int | None = None, quote=None, ids=None, agent=None) -> int | None:
        """Отправить (кусками, с разметкой; не разобралась разметка — простым текстом). Возвращает id последнего сообщения."""
        async with self.sending(chat_id):
            return await self._send_plain(chat_id,text,buttons=buttons,reply_to=reply_to,quote=quote,ids=ids,agent=agent)

    async def _send_plain(self, chat_id, text, *, buttons=None, reply_to=None, quote=None, ids=None, agent=None, offset=0):
        last = None
        chunks = split(text)
        mark = self.mark(agent) if agent is not None else ''
        for i, chunk in enumerate(chunks):
            n = offset + i
            if n and mark: chunk = mark + ' (' + str(n + 1) + '/' + str(offset + len(chunks)) + ')\n' + chunk
            payload: dict = {"chat_id": chat_id, "text": md_to_html(chunk), "parse_mode": "HTML", "disable_web_page_preview": True}
            if reply_to and i == 0:
                payload["reply_parameters"] = self._reply(reply_to, quote)
            if buttons and i == len(chunks) - 1:
                payload["reply_markup"] = {"inline_keyboard": buttons}
            attempt = 0
            while True:
                delay = self._retry_until.get(chat_id, 0) - time.monotonic()
                if delay > 0: await self._pause_wait(chat_id, delay)
                try:
                    res = await self.api("sendMessage", payload)
                    break
                except TelegramError as e:
                    if self._quote_refused(e, payload): continue
                    if e.data.get('code') == 429 and attempt < 3:
                        attempt += 1
                        try: delay = max(1, min(86400, float(e.data.get('retry_after') or 1)))
                        except (ValueError, TypeError): delay = 1
                        self.pause(chat_id, delay)
                        continue
                    if 'parse_mode' in payload and e.code == 'tg_api' and any(s in e.title.lower() for s in ('parse', 'entit')):
                        payload.pop('parse_mode')
                        payload['text'] = chunk
                        if attempt < 3:
                            attempt += 1
                            continue
                    raise
            last = (res or {}).get("message_id") if isinstance(res, dict) else None
            if ids is not None and isinstance(last, int) and not isinstance(last, bool): ids.append(last)
        self._wake_drafts(chat_id)
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
        self._draft_holder.clear()
        self._status_messages.clear()
        self._albums.clear()
        self._recent.clear()
        self._flushing.clear()
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
        # У правки дата отправки — давняя; опоздание считается от времени правки (edit_date).
        dates = [stamp for u in updates or [] for stamp in [(u.get('message') or {}).get('date') if u.get('message') else
                 (u.get('edited_message') or {}).get('edit_date') or (u.get('edited_message') or {}).get('date')]
                 if isinstance(stamp, (int, float)) and not isinstance(stamp, bool)]
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

    SEEN = {'insert': '👀', 'queue': '✍', 'interrupt': '⚡'}  # вставлю в ход · после хода · прервал ход ради этого

    async def _seen(self, chat_id, message_id, how='insert') -> None:
        """Реакция на сообщении, пришедшем во время хода: хозяин видит, что бот его получил и как доставит."""
        try:
            await self.api('setMessageReaction', {'chat_id': int(chat_id), 'message_id': message_id, 'reaction': [{'type': 'emoji', 'emoji': self.SEEN.get(how, '👀')}]}, timeout=10)
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
            if str(cq.get('data') or '').startswith(('ap:','q:','nm:')):return None
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

    def _batch_wait(self, msg):
        """Собирать ли сообщение в пачку: (вид, сколько ждать тишины) или None. Альбом — всегда 0,8 с;
        пересланное — настройкой forward_batch_ms (пересланный альбом входит в ту же пачку)."""
        from .telegram_forward import forwarded
        if forwarded(msg):
            try:wait=int(self.settings().get('forward_batch_ms',800) or 0)
            except (TypeError,ValueError):wait=800
            if wait>0:return ('fwd',),min(wait,5000)/1000
        if msg.get('media_group_id'):return ('album',msg['media_group_id']),.8
        return None

    def _collect(self, msg, kind, wait):
        """Конец альбома и пачки пересланного API не отмечает: ждём тишины после последнего сообщения."""
        key=(int((msg.get('chat') or {}).get('id')),msg.get('message_thread_id'))+kind
        limit=FORWARD_BATCH_MAX if kind[0]=='fwd' else 10
        old=self._albums.get(key)
        if old and len(old[0])>=limit:old=None  # полная пачка уже уходит; следующее сообщение начинает новую
        messages=old[0] if old else []
        if old:old[1].cancel()
        if len(messages)<limit:messages.append({**msg,'_arrived':time.monotonic()})
        delay=0 if len(messages)>=limit else wait
        async def flush():
            await asyncio.sleep(delay)
            if (self._albums.get(key) or (None,))[0] is messages:self._albums.pop(key,None)
            # Тишина дождались, пачка разбирается (скачивание, расшифровка): следующее сообщение чата её подождёт.
            running=self._flushing.setdefault(key[:2],set());running.add(task)
            try:await self._handle({'message':self._combined(messages)})
            finally:
                running.discard(task)
                if not running and self._flushing.get(key[:2]) is running:self._flushing.pop(key[:2],None)
        task=asyncio.create_task(flush())
        self._albums[key]=(messages,task)
        self._bg.add(task);task.add_done_callback(self._task_done)

    @staticmethod
    def _combined(messages):
        combined={**messages[0],'_album_messages':list(messages)}
        combined.pop('media_group_id',None)
        return combined

    async def _flush_pending(self, lane):
        """Следующее сообщение того же чата не обгоняет собираемую пачку: она уходит сразу, без ожидания тишины,
        а уже разбираемую (тишина дождалась, идёт скачивание) оно ждёт, как обычное сообщение ждёт предыдущее."""
        running=[task for task in self._flushing.get(lane,()) if not task.done()]
        if running:await asyncio.wait(running,timeout=ORDER_WAIT)
        for key,(messages,task) in list(self._albums.items()):
            if key[:2]!=lane or (self._albums.get(key) or (None,))[0] is not messages:continue
            task.cancel();self._albums.pop(key,None)
            await self._handle({'message':self._combined(messages)})

    def _still_working(self, comment):
        """Комментарий ещё в работе — ждёт в очереди хода или его ход идёт; на уже отвеченный пересланное не ссылается."""
        try:ctx=self.app.agents.get(comment['agent'])
        except HumanError:return False
        return ctx.mind.running(comment['sid'])

    def _join_comment(self, comment, chat, text, attachments, forward, ids, items):
        """Комментарий хозяина (Telegram присылает его раньше пересланного) ещё ждёт в очереди идущего хода —
        модель его не видела: пачка дописывается в ту же запись. Иначе — False, пачка идёт обычным сообщением."""
        try:ctx=self.app.agents.get(comment['agent'])
        except HumanError:return False
        from . import media
        def match(msg,meta):return meta.get('telegram_message_id')==comment['mid'] and meta.get('telegram_chat_id')==int(chat)
        def change(msg,meta):
            parts=media.content(text,attachments,vision=ctx.settings.get('attachments','vision',True),audio=False) if attachments else [{'type':'text','text':text}]
            value=msg.get('content')
            if isinstance(value,list):
                first=next((n for n,p in enumerate(value) if isinstance(p,dict) and p.get('type')=='text'),None)
                if first is None:return None
                value=list(value);value[first]={**value[first],'text':str(value[first].get('text') or '')+'\n\n'+parts[0]['text']}
                value+=parts[1:]
            elif len(parts)>1:value=[{'type':'text','text':str(value or '')+'\n\n'+parts[0]['text']},*parts[1:]]
            else:value=str(value or '')+'\n\n'+parts[0]['text']
            meta={**meta,'untrusted':True,'forwarded':True,'forward':forward,'attachments':list(meta.get('attachments') or [])+list(attachments),
                  'telegram_message_ids':[comment['mid'],*ids]}
            return {**msg,'content':value},meta
        if not ctx.mind.amend_queued(comment['sid'],match,change):return False
        self.remember(ctx,chat,ids,comment['sid'],text,'in',tainted=True,foreign=True,items=items)
        return True

    async def _transcribe(self, ctx, chat, sid, m, attachments, *, tainted, wrap=True):
        """Голосовое m → (текст, вложения, нативный ли звук). Пересланное (wrap=False) уже внутри рамки пересланного."""
        info=m["voice"]
        ctx.voice.require("stt")
        from ..tools.registry import ToolContext
        await ToolContext(ctx,actor="owner").check("audio.transcribe","Telegram","загрузить голосовое сообщение хозяина")
        from pathlib import Path
        old=next((i for i in attachments if i['kind']=='voice' and i.get('_message',m.get('message_id'))==m.get('message_id')),None)
        if old is None:raise HumanError('telegram_voice','Голосовое не скачалось','Пришли его ещё раз.')
        audio=Path(old['path']).read_bytes()
        from ..voice.input import prepare,attachment,transcript_text
        result=await prepare(ctx,audio,ext='ogg',seconds=info.get('duration',0),session=sid,from_owner=not tainted)
        text=transcript_text(result,seconds=info.get('duration',0),from_owner=not tainted,wrap=wrap)
        if m.get('caption'):text+='\n\nПодпись к голосовому: '+str(m['caption'])
        native=bool(result['native'])
        if native:
            item=await attachment(ctx,result,session=sid,existing=old,source='telegram')
            attachments=[item if a is old else a for a in attachments]
        if result.get('notice'):
            notice=self.signed(ctx,result['notice']);ids=[]
            await self.send(chat,notice,ids=ids,agent=ctx)
            self.remember(ctx,chat,ids,sid,notice,'out')
        return text,attachments,native

    async def _media_notice(self,ctx,chat,sid,text):
        from ..tools.registry import ToolContext
        owner=self.owner() or {};private=owner.get('chat_id') or owner.get('user_id')
        try:
            await ToolContext(ctx,actor='owner',session_id=sid).check('msg.send','telegram:owner' if chat==private else 'telegram:group:'+str(chat),
                'объяснить обработку присланного вложения',to_owner=chat==private)
            text=self.signed(ctx,text);ids=[]
            mid=await self.send(chat,text,ids=ids,agent=ctx)
            self.remember(ctx,chat,ids or mid,sid,text,'out')  # ответ цитатой на уведомление — тому агенту, чьё вложение
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
        async def download(kind,info,message=None):
            item=await self._download(ctx,sid,kind,info)
            item['_message']=message  # чьё вложение — для пачки с несколькими голосовыми; убирается при разборе
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
                files.append(await download(kind,info,msg.get('message_id')))
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

    def remember(self, ctx, chat, message, sid, text, direction,*,source_text=None,tainted=False,command=False,foreign=False,items=None,answer=False):
        """foreign — сам текст не слова хозяина (пересланное); tainted — ход или сообщение несли чужое.
        message — номер или список номеров (куски одного ответа, альбом, пачка): маршрут один, одна транзакция.
        items — {номер: {'source_text', 'media'}}: у сообщения альбома/пачки своя подпись и отпечаток вложения (для правок).
        answer — ответ на вопрос агента: запоминается, чтобы правку ответа было видно.
        Строка на сообщение (telegram_refs.py), без переписи общего JSON."""
        from . import telegram_refs
        from .telegram_events import digest
        if not isinstance(message,(int,list,tuple)):return
        data={'session':sid,'text':text[:4000],'direction':direction,'thread':getattr(chat,'thread',None),'tainted':bool(tainted),'foreign':bool(foreign)}
        if source_text is not None:
            data.update(source_text=source_text[:4096],source_hash=digest(source_text))
        if command:data['command']=True
        if answer:data['answer']=True
        per={}
        for mid,item in (items or {}).items():
            own={'media':item['media']} if isinstance(item.get('media'),list) else {}
            if isinstance(item.get('source_text'),str):own.update(source_text=item['source_text'][:4096],source_hash=digest(item['source_text']))
            per[mid]=own
        telegram_refs.remember(self.app,ctx,chat,message,data,per)

    def require_ref(self, ctx, chat, message, sid):
        from .telegram_refs import ref as known
        ref=known(ctx,chat,message)
        if not ref or ref['session']!=sid:
            raise HumanError('telegram_ref','Номер сообщения не найден в этом разговоре','Возьми [#номер] из его истории, не из чужого чата.')
        return ref

    async def _reaction(self, event):
        from .reactions import EMOJI
        owner=self.owner() or {}
        if not self.settings().get('reactions',True) or (event.get('user') or {}).get('id')!=owner.get('user_id'):return
        chat=(event.get('chat') or {}).get('id');mid=event.get('message_id')
        if chat!=(owner.get('chat_id') or owner.get('user_id')) and not self.groups.allowed(chat):return
        from . import telegram_refs
        route=telegram_refs.route(self.app,chat,mid) or {}
        try:ctx=self.app.agents.get(route.get('agent','main'))
        except HumanError:return
        ref=telegram_refs.ref(ctx,chat,mid)
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
        from . import telegram_forward
        lane=(int(chat['id']),msg.get('message_thread_id'))
        arrived=msg.get('_arrived') or time.monotonic()
        self._arrived[lane]=arrived
        if len(self._arrived)>200:self._arrived.pop(next(iter(self._arrived)))
        if not msg.get('_album_messages'):
            batch=self._batch_wait(msg)
            if batch:
                self._collect(msg,*batch)
                return
            await self._flush_pending(lane)  # собранная пачка этого чата уходит раньше следующего сообщения
            self._recent.pop(lane,None)  # комментарием к пересланному станет только простой текст прямо перед ним, не команда
        from .telegram_routes import fallback_notice,route_reply
        try:ctx,sid,refused=*self.selected(chat['id']),None
        except HumanError as error:ctx,sid,refused=None,None,error  # закрытая тема: действующий адресат цитаты всё равно получит ответ
        try:
            # ответ цитатой на сообщение соседа по стае — ему, а не выбранному агенту; выбор не меняется
            ctx,sid,routing=route_reply(self,chat['id'],msg,ctx,sid)
            if ctx is None:raise refused
        except HumanError as error:
            await self.send(chat['id'],error.title+'. '+error.hint)
            return
        notice=fallback_notice(self,routing)
        if notice:await self.send(chat['id'],notice)
        messages=msg.get('_album_messages') or [msg]
        origins=[telegram_forward.origin(m,owner.get('user_id'),(self.me or {}).get('id')) for m in messages]
        forwarded=any(origins)
        texts=[body(m) for m in messages]
        if len(messages)>1:text='\n'.join(texts).strip()
        notes=[]  # дописанное к тексту при разборе вложений: у пересланного — внутри той же рамки
        def note(piece):
            nonlocal text
            text+=('\n\n' if text else '')+piece;notes.append(piece)
        try:attachments=await self.receive_media(ctx,messages,sid)
        except (HumanError,NetError,ValueError) as exc:
            await self.send(chat['id'],str(exc))
            return
        for item in messages:
            if item.get('_rich_skipped'):
                count,enabled=item['_rich_skipped']
                note(f'[Медиа из поста не скачано: {count}. '+('Вложения выключены в настройках.]' if not enabled else f'Беру не больше {RICH_MEDIA_MAX} файлов и 20 МБ за сообщение.]'))
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
        for n,item in enumerate(messages):
            if not item.get('voice'):continue
            try:said,attachments,native=await self._transcribe(ctx,chat['id'],sid,item,attachments,tainted=tainted,wrap=not forwarded)
            except (HumanError,NetError,ValueError) as exc:
                if forwarded and len(messages)>1:
                    texts[n]='[голосовое не расшифровано: '+str(exc)[:200]+']';continue  # остальная пачка доходит
                from .media import describe
                await self.send(chat["id"],str(exc)+'\n'+describe(attachments))
                return
            texts[n]=said
            if len(messages)==1:text=said
            if native:tainted=True  # Сохранённый аудиофайл — внешние данные, в отличие от проверяемого текста хозяина.
        if attachments:
            from ..voice.files import format_of,prepare as audio_file
            from .video_in import KINDS as VISUAL,look
            processed=[]
            for item in attachments:
                if item['kind']=='audio' or item['kind']=='document' and format_of(item):
                    tainted=True
                    try:
                        result=await audio_file(ctx,item,session=sid);item=result['item'];note(result['text'])
                        if result['notice']:await self._media_notice(ctx,chat['id'],sid,result['notice'])
                    except (HumanError,NetError,ValueError) as error:
                        note('[Аудиофайл не расшифрован: '+str(error)+']\nФайл: '+item['path'])
                        await self._media_notice(ctx,chat['id'],sid,str(error)+'\nФайл: '+item['path'])
                elif item['kind'] in VISUAL:
                    tainted=True
                    seen=await look(self,ctx,item,sid);item=seen['item'];note(seen['text'])
                    processed+=seen['extra']
                    if seen['notice']:await self._media_notice(ctx,chat['id'],sid,seen['notice'])
                processed.append(item)
            attachments=processed
        for item in attachments:item.pop('_message',None)
        if attachments and not text:text='Прислано вложение.'
        if not text and not forwarded:
            await self.send(chat["id"],"Пришли текст или голосовое сообщение; голос включается в пульте → Настройки → Голос.")
            return
        source_text=body(msg)
        comment=None
        if forwarded:
            # Одна рамка на пачку: автор и исходная дата — в строке источника, у пачки ещё и в каждой строке.
            items=[{'origin':o,'text':t if m.get('voice') else telegram_forward.item_text(m,t),'id':m.get('message_id'),'date':m.get('date')}
                   for m,o,t in zip(messages,origins,texts)]
            recent=self._recent.get(lane)
            if recent and 0<=arrived-recent['at']<=COMMENT_WINDOW and self._still_working(recent):comment=recent
            text=telegram_forward.frame(items,extra='\n\n'.join(notes),comment=comment['mid'] if comment else None)
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
        # ответ цитатой на карточку вопроса — ответ этому вопросу, чей бы он ни был
        qid=self._question_for(chat['id'],(msg.get('reply_to_message') or {}).get('message_id')) if not tainted and all(a['kind']=='voice' for a in attachments) else None
        if qid:
            card=self.app.questions[qid]['card']
            try:asker=self.app.agents.get(card.get('agent_id','main'))
            except HumanError:asker=ctx
            if self.app.answer(qid,text):
                # Ответ тоже запоминается: его правку агент увидит.
                self.remember(asker,chat['id'],msg.get('message_id'),card.get('session') or asker.main_session(),text,'in',source_text=source_text,answer=True)
                asker.journal.write('channel','Telegram: хозяин ответил на вопрос цитатой',actor='owner',data={'question':qid})
                sent=[];await self.send(chat['id'],'→ Ответ передан'+('' if asker is ctx and asker.agent_id=='main' else ' «'+str(asker.selfdir.identity().get('name') or asker.agent_id)+'»')+'.',ids=sent)
                self._route_only(asker,chat['id'],sent,card.get('session'))
                return
        # ход — в фоне: опрос не должен ждать конца хода (иначе кнопка одобрения не дойдёт, пока ход её ждёт)
        quote=msg.get('reply_to_message') or {}
        excerpt=msg.get('quote') or {};external=msg.get('external_reply') or {}
        if quote or excerpt or external:
            from ..mind.quotes import append
            from . import telegram_refs
            if external:
                # Сообщение другого чата: его номер не из этого разговора, автор и чат — только в строке источника рамки.
                where,word=telegram_forward.describe_external(external,owner_id=owner.get('user_id'),bot_id=(self.me or {}).get('id'))
                quote_text=excerpt.get('text') or '['+(word+'; ' if word else '')+'текст сообщения из другого чата не передан]'
                text=append(text,quote_text,channel='Telegram',reference='внешнее',label='из другого чата',source=where,trusted=False)
                tainted=True
            else:
                mid=quote.get('message_id') if quote else None
                known=telegram_refs.ref(ctx,chat['id'],mid) if quote else None
                # Богатые сообщения бота Telegram цитирует без текста — берём его из памяти отправленного, в том числе у соседа по стае.
                saved=known;neighbour=None
                status=self._status_messages.get((int(chat['id']),mid)) if quote else None
                if quote and not saved:
                    route=telegram_refs.route(self.app,chat['id'],mid) or {}
                    # Сосед в архиве или стая выключена — его сообщение всё равно известно: архив не делает его слова чужими.
                    try:other=self.app.agents.get(route['agent'],archived=True) if route.get('agent') else None
                    except HumanError:other=None
                    neighbour=telegram_refs.ref(other,chat['id'],mid) if other else None
                    if neighbour:saved={**neighbour,'text':'['+str(other.selfdir.identity().get('name') or other.agent_id)+'] '+str(neighbour.get('source_text') or neighbour.get('text') or ''),'source_text':None}
                word=telegram_forward.media_word(quote)
                if status and not saved:
                    # Статус хода маршрут помнит, а текст нет: он и так из идущего хода; передаём только, чей это статус.
                    try:whose=self.app.agents.get(status[0],archived=True)
                    except HumanError:whose=None
                    from .telegram_routes import agent_name
                    quote_text='[статус хода'+(' «'+str(agent_name(whose))+'»' if whose is not None else '')+'; текст статуса не передаётся]'
                else:
                    quote_text=excerpt.get('text') or body(quote) or (saved or {}).get('source_text') or (saved or {}).get('text') or '['+(word+'; ' if word else 'медиа; ')+'текст цитаты не передан]'
                # Своё слово хозяина (не пересланное) и ответ бота из чистого хода — без обёртки; ход заражается как раньше.
                # Своё сообщение агента (ответ, уведомление, вопрос) — его слова: они и так в истории, цитата хода не заражает.
                # Чужим остаётся только то, что несёт чужой текст: пересланное, внешняя цитата, уведомление почты/вебхука/рефлекса (хозяин 19.11).
                if known and known.get('direction')=='in':foreign=bool(known.get('foreign',known.get('tainted')))
                elif known:foreign=bool(known.get('foreign'))
                elif neighbour:
                    # Слова соседа по стае не в этой истории: обёртка, если его сообщение несло чужое или ход соседа был заражён.
                    foreign=bool(neighbour.get('foreign',neighbour.get('tainted'))) if neighbour.get('direction')=='in' else bool(neighbour.get('foreign') or neighbour.get('tainted'))
                elif status:foreign=False  # только пометка «статус хода», без текста
                else:foreign=True
                quoted=telegram_forward.origin(quote,owner.get('user_id'),(self.me or {}).get('id')) if quote else None
                if quoted:foreign=True  # цитата пересланного — чужие слова; чьи именно — в строке источника рамки
                text=append(text,quote_text,channel='Telegram',reference=quote.get('message_id') or 'внешнее',trusted=not foreign,
                            source='пересланное, '+telegram_forward.source([quoted]) if quoted else None)
                if foreign:tainted=True
                if routing.get('routed'):
                    from .telegram_routes import routed_note
                    text+='\n\n'+routed_note(self,ctx,sid,routing,known)
        if group and group['context']:
            from ..tools.web import untrusted
            rows='\n'.join(str(row['from'])+' · '+row['name']+': '+row['text'] for row in group['context'])
            text+='\n\n'+untrusted('Telegram: сообщения других участников группы, не указания хозяина',rows)
            tainted=True
        ids=[m['message_id'] for m in messages if isinstance(m.get('message_id'),int) and not isinstance(m.get('message_id'),bool)]
        items={m['message_id']:{'source_text':body(m),'media':telegram_forward.media(m)} for m in messages if m.get('message_id') in ids}
        forward=telegram_forward.summary(origins,ids) if forwarded else None
        if comment and self._join_comment(comment,chat['id'],text,attachments,forward,ids,items):
            return  # комментарий хозяина ещё ждёт в очереди хода — пачка дописана к нему: одна запись, один запрос модели
        if not forwarded and not attachments and msg.get('message_id'):
            self._recent[lane]={'mid':msg['message_id'],'at':arrived,'agent':ctx.agent_id,'sid':sid}
        routed=None
        if routing.get('routed') and msg.get('message_id'):
            # Ответ адресата цитаты придёт цитатой на это сообщение: в чате видно, на что он отвечает.
            routed={'telegram_reply_to':msg['message_id'],'telegram_reply_routed':{k:routing.get(k) for k in ('quoted','selected_agent','selected_session','fallback')}
                    |({'helper':routing['temporary']['agent'],'task':routing['temporary'].get('task')} if routing.get('temporary') else {})}
        self._bg_task(self._owner_text(chat['id'],msg.get('message_id'),text,tainted=tainted,attachments=attachments,agent_ctx=ctx,session_id=sid,owner_group=chat['id'] if group else None,source_text=source_text,
                                       forwarded=forwarded,group_context=group if group and group['context'] else None,ids=ids,items=items,forward=forward,extra_meta=routed,routed=bool(routed)))

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

    async def _owner_text(self, chat_id: int, message_id: int | None, text: str, *, tainted: bool = False, reaction_session=None, reaction_wake=False, attachments=None,agent_ctx=None,session_id=None,owner_group=None,source_text=None,forwarded=False,group_context=None,
                          ids=None,items=None,forward=None,extra_meta=None,edit_wake=False,routed=False) -> None:
        """routed — ответ цитатой ушёл не выбранному агенту: 👀 сразу (хозяин видит, что принято), ответ — цитатой на сообщение."""
        from .telegram_routes import bind
        if agent_ctx is not None and (reaction_session or session_id):
            app,sid=agent_ctx,reaction_session or session_id  # адрес уже выбран (в том числе цитатой при закрытой теме)
        else:
            selected,chosen=self.selected(chat_id)
            app=agent_ctx or selected
            sid=reaction_session or session_id or (chosen if app is selected else app.main_session())
        bind(self,chat_id,app,sid)
        attachments=attachments or []
        # агент ждёт ответа на вопрос без вариантов — это ответ, а не новое сообщение
        for qid, q in (list(app.questions.items()) if not tainted and not attachments and not reaction_wake and not edit_wake else []):
            if q["card"].get("session") in (sid, None) and q["card"].get("agent_id",app.agent_id)==app.agent_id and not q["card"].get("options"):
                if message_id:self.remember(app,chat_id,message_id,sid,text,'in',source_text=source_text,answer=True)  # правку ответа агент увидит
                app.answer(qid, text)
                return
        typing_done=False
        typing = asyncio.ensure_future(self._typing(chat_id,alive=lambda:not typing_done)) if self.settings().get("live", "full") == "off" or owner_group is not None else None
        if routed and message_id:self._bg_task(self._seen(chat_id,message_id))
        try:
            if message_id:self.remember(app,chat_id,ids or message_id,sid,text,'in',source_text=source_text,tainted=tainted,foreign=forwarded,items=items)
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
            if message_id and not reaction_wake and app.mind.running(sid) and app.mind.delivery(sid,'telegram','owner')=='interrupt':
                self._bg_task(self._seen(chat_id,message_id,'interrupt'))  # ⚡ — ради этого сообщения ход прерван
            try:
                res = await app.mind.say(sid, text, content=parts, channel="telegram", actor="reaction" if reaction_wake else "owner",tainted=tainted or reaction_wake or any(a['kind']!='voice' for a in attachments),
                                         unattended=reaction_wake,message_meta={'telegram_message_id':message_id,'telegram_chat_id':int(chat_id),'attachments':attachments,'telegram_route':{'chat':int(chat_id),'thread':getattr(chat_id,'thread',None)},'telegram_owner_requested':owner_group,
                                                       **({'forwarded':True,'forward':forward} if forwarded else {}),**({'telegram_message_ids':list(ids)} if ids and len(ids)>1 else {}),**(extra_meta or {})})
            except HumanError:
                if taken:self.groups.give_back(group_context,taken)
                raise
            if res is None:  # встало в идущий ход — ждём его конца
                t = app.mind.turns.get(sid)
                busy = 'ход уже заканчивался'
                if t and t.task and message_id and not t.task.done():
                    if not routed:self._bg_task(self._seen(chat_id, message_id, app.mind.pending_delivery(sid, message_id) or 'insert'))
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
            await self._answer(chat_id, sid, res, reply_to=message_id if routed else None,agent_ctx=app)

    async def _answer(self, chat_id, sid, res, *, label="", reply_to=None,agent_ctx=None):
        """reply_to — ответ на сообщение хозяина, пришедшее цитатой не выбранному агенту (иначе ответ обычный);
        «↩#N» в начале ответа — цитатой на #N, «↩#N «фрагмент»» — ещё и с выделенным куском сообщения."""
        app = agent_ctx or self.app
        res = res or {}
        from ..core.dates import strip_labels
        reply = strip_labels((res.get("text") or "").strip())
        err = res.get("error")
        if not err and (res.get('sent_all') or reply and reply in res.get('channel_sent',[])):return
        quote=None
        explicit=re.match(r'^↩\s*#(\d+)(?:[ \t]*«([^»]{1,1024})»)?\s*',reply)
        if explicit:
            number=int(explicit[1]);whole=reply[re.match(r'^↩\s*#\d+\s*',reply).end():];reply=reply[explicit.end():]
            try:
                ref=self.require_ref(app,chat_id,number,sid);reply_to=number
                quote=self.quote_fragment(ref,explicit[2]) if explicit[2] else None
                if explicit[2] and not quote:
                    reply=whole  # «…» не кусок сообщения (например, название) — остаётся в тексте ответа
                    app.journal.write('channel','Telegram: фрагмент цитаты не найден в сообщении дословно — ответ без фрагмента',data={'message':number})
            except HumanError:
                if explicit[2]:reply=whole
                app.journal.write('channel','Telegram: неизвестный номер ответа, отправлен обычным сообщением')
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
                    # Знак агента — подписью голосового: в общем чате стаи видно, чей это голос.
                    mid=await self.send_voice(chat_id,result["audio"],caption=self.mark(app),reply_to=reply_to)
                    self.remember(app,chat_id,mid,sid,reply,'out',tainted=bool(res.get('tainted')))
                    return
                except (HumanError,TelegramError,NetError) as exc:
                    app.journal.write("voice","Telegram: голос недоступен, отправлен текст",data={"reason":str(exc)})
                    reply += "\n\nГолос недоступен, отвечаю текстом: " + str(exc)
            reply=self.signed(app,reply)
            ids=[]
            mid=await self.send(chat_id, reply, reply_to=reply_to, quote=quote, ids=ids, agent=app)
            self.remember(app,chat_id,ids or mid,sid,reply,'out',tainted=bool(res.get('tainted')))  # каждый кусок длинного ответа — его
            row=app.store.one("SELECT id FROM messages WHERE session_id=? AND role='assistant' ORDER BY id DESC LIMIT 1",(sid,))
            if row and mid:
                app.store.x("UPDATE messages SET meta=json_set(meta,'$.telegram_message_id',?,'$.telegram_chat_id',?) WHERE id=?",(mid,chat_id,row['id']))

    async def send_voice(self, chat_id, audio, *, caption='', reply_to=None):
        payload={'chat_id':chat_id,'voice':'attach://voice'}
        if caption:payload['caption']=caption[:1024]
        if reply_to:payload['reply_parameters']=self._reply(reply_to)  # multipart сам переводит составное поле в JSON
        async with self.sending(chat_id):
            result=await self.api('sendVoice',payload,files={'voice':{'name':'voice.ogg','mime':'audio/ogg','bytes':audio}})
        self._wake_drafts(chat_id)
        return (result or {}).get('message_id')

    @staticmethod
    def quote_fragment(ref, fragment):
        """Фрагмент для reply_parameters.quote: дословный кусок известного сообщения до 1024 знаков, иначе None."""
        if not isinstance(fragment,str) or not isinstance(ref,dict):return None
        fragment=fragment.strip()
        source=str(ref.get('source_text') or ref.get('text') or '')
        if not fragment or len(fragment)>1024 or fragment not in source:return None
        return fragment

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
        card = {}  # чья карточка: подтверждение «→ …» под ней — тоже сообщение этого агента
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
        elif data.startswith("nm:"):
            # Кнопки под статусом хода: как доставить ждущие слова хозяина. Без лишнего сообщения — статус обновится сам.
            m = cq.get("message") or {}
            target = self._status_messages.get((int((m.get("chat") or {}).get("id") or 0), m.get("message_id")))
            mode = data[3:]
            try:
                moved = await self.app.agents.get(target[0]).mind.redeliver(target[1], mode) if target else 0
                ok_text = ({"interrupt": "⚡ Прерываю ход и отвечаю", "queue": "⏳ Отвечу после хода", "insert": "↪ Вставлю в ход"}.get(mode, "Готово")
                           if moved else "Ждущих сообщений уже нет")
            except HumanError as exc:
                ok_text = exc.title
            try:await self.api("answerCallbackQuery", {"callback_query_id": cq.get("id"), "text": ok_text[:190]})
            except TelegramError:pass
            return
        elif data.startswith("ap:"):
            _, pid, choice = data.split(":", 2)
            card = (self.app.guard.pending.get(pid) or {}).get("card") or {}
            done = self.app.guard.respond(pid, choice, by="owner")
            ok_text = {"once": "Разрешено", "task": "Разрешено в этом разговоре", "always": "Разрешено всегда", "deny": "Отказано"}.get(choice, "") if done else "Уже решено"
        elif data.startswith("q:"):
            _, qid, idx = data.split(":", 2)
            q = self.app.questions.get(qid)
            card = (q or {}).get("card") or {}
            opts = card.get("options") or []
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
                sent = []
                await self.send((m.get("chat") or {}).get("id"), f"→ {ok_text}", ids=sent)
                if card.get("agent_id"):
                    try:self._route_only(self.app.agents.get(card["agent_id"], archived=True), (m.get("chat") or {}).get("id"), sent, card.get("session"))
                    except HumanError:pass
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
        if live:
            live.keep_status=bool(deliver)  # статус-сообщение удаляется после ответа, а не перед ним: ответ виден раньше
            await live.finish()
        try:
            if not deliver or self.state=='off':return
            for delay in (*ANSWER_RETRY,None):
                try:
                    await self._answer(chat,ev['session'],ev.get('result'),label=ev.get('label',''),reply_to=reply_to,agent_ctx=agent_ctx)
                    if live:self._timed(live,agent_ctx)
                    return
                except TelegramError as error:
                    # Сеть моргнула — ответ не выбрасываем, а пробуем ещё.
                    if error.code!='tg_net' or delay is None or self._stopping:raise
                    await asyncio.sleep(delay)
        finally:
            if live:
                await live.drop_status()
                await self._end_draft(live)

    def _timed(self, live, ctx=None):
        """Где ждал ответ: до хода, молчание модели до первого слова, до текста у хозяина, весь ответ.
        Медленный (дольше SLOW_ANSWER до первого слова или текста) — строка в журнал с самым долгим этапом."""
        now=time.monotonic();start=live.t_got or live.t_live
        stages={'queue':live.t_live-start,'model':(live.t_model or now)-live.t_live,
                'shown':((live.t_text or now)-(live.t_model or now)) if live.t_model else 0.0,'total':now-start}
        item={k:round(max(0.0,v),2) for k,v in stages.items()}
        item.update(at=time.time(),agent=getattr(ctx or self.app,'agent_id','main'),measured=bool(live.t_got))
        self.timing.append(item)
        first=item['queue']+item['model']
        if first<SLOW_ANSWER and item['total']<SLOW_ANSWER*4:return
        names={'queue':'ждало до начала хода','model':'модель молчала до первого слова','shown':'текст шёл в Telegram'}
        worst=max(names,key=lambda k:item[k])
        def sec(v):return f'{v:.1f}'.replace('.',',')+' с'
        self.app.journal.write('channel',f'Telegram: ответ через {sec(item["total"])} — дольше всего {names[worst]} ({sec(item[worst])}); '
                               f'до хода {sec(item["queue"])}, модель {sec(item["model"])}, до текста {sec(item["shown"])}',actor='telegram',data=item)

    async def _close_live(self, live):
        await live.finish()
        await self._end_draft(live)

    async def _end_draft(self, live):
        """Черновик Telegram живёт ~30 с и держит у хозяина «обработку» вместо кнопки отправки. API не умеет
        удалить черновик — его закрывает только обычное сообщение бота. Если после последнего черновика
        сообщения не было (агент всё отправил тулом, ответ пустой или ушёл в другой канал), шлём тихое
        «✓» и сразу удаляем: поле ввода освобождается, в чате ничего не остаётся."""
        if not live.drafted_at or self.state=='off' or self._stopping:return
        key=self.slot(live.chat)
        if key is None:return
        if self._sent_at.get(key,0)>=live.drafted_at:return
        # Соседу по стае место черновика досталось позже: наш черновик уже заменён, «✓» закрыл бы его черновик.
        if any(other is not live and not other.closed and self.slot(other.chat)==key and other.drafted_at>live.drafted_at for other in list(self._live.values())):return
        try:
            async with self.sending(live.chat):
                result=await self.api('sendMessage',{'chat_id':live.chat,'text':'✓','disable_notification':True},timeout=8)
            mid=(result or {}).get('message_id') if isinstance(result,dict) else None
            if mid:await self.api('deleteMessage',{'chat_id':live.chat,'message_id':mid},timeout=8)
        except TelegramError as error:
            live.app.journal.write('channel','Telegram: не удалось закрыть черновик хода',data={'error':error.title[:200]})
        self._wake_drafts(live.chat,skip=live)  # «✓» закрывает любой черновик этого места — открытый соседский уйдёт снова

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
        if ev.get('type') in ('approval','question') and not route:
            chat=self._card_destination(ev.get('card') or {})  # карточка — в тему спросившего агента, иначе в личку
        else:
            chat=self.topics.address(route['chat'],route.get('thread')) if route.get('chat') else self.destination(ctx,ev.get('session'))
        if chat!=private and (ev.get('type')!='answer' or (ev.get('result') or {}).get('telegram_owner_requested')!=chat):chat=private
        s=ctx.settings.group('telegram')
        t=ev.get('type');key=self.route_key(ctx,ev.get('session'))
        # Групповой ход не показывает черновики даже в личке хозяина.
        if not route.get('chat') or route.get('chat')==private:self._stream(ev,chat,ctx)
        # Ответ на сообщение, пришедшее цитатой не выбранному агенту, — цитатой на него (номер несёт сам ход).
        reply_to=(ev.get('result') or {}).get('telegram_reply_to') if t=='answer' else None
        if t == "answer":
            live = self._live.pop(key, None)
            deliver = ev.get('channel') == 'telegram' and (ev.get('source') == 'telegram' or s.get('notify', True))
            if live or deliver: self._bg_task(self._finish_answer(live, chat, ev, reply_to, deliver,ctx))
        elif t == "message" and s.get("notify",True) and ev.get("message",{}).get("kind")=="skill":
            self._bg_task(self._skill_notice(chat,ev["message"],ctx))
        elif t == "notify" and s.get("notify", True) and ev.get("channel", "telegram") == "telegram":
            icon = {"remind": "⏰", "alarm": "🚨", "job": "📋", "reflex": "🔔", "pulse": "💓", "mail": "📧", "hook": "🪝", "update": "🔄", "load": "📈"}.get(ev.get("kind"), "•")
            text = str(ev.get("text") or "")
            if not text.startswith(icon):
                text = f"{icon} {text}"
            self._bg_task(self._notice(chat, text, ctx, ev.get('session'), agent=aid!='*', foreign=ev.get('kind') in ('mail','hook','reflex') or bool(ev.get('untrusted'))))
        elif t == "approval" and s.get("approvals", True):
            c = ev.get("card") or {}
            from ..guard.cardtext import action_text
            what, cut = action_text(c)
            row = [{"text": "Да, один раз", "callback_data": f"ap:{c['id']}:once"}] + ([{"text": "В этом разговоре", "callback_data": f"ap:{c['id']}:task"}] if c.get("can_session", True) else [])
            row2 = ([{"text": ("Всегда: " + str(c.get("remember") or "такое"))[:60], "callback_data": f"ap:{c['id']}:always"}] if c.get("can_remember") else []) + [{"text": "Нет", "callback_data": f"ap:{c['id']}:deny"}]
            full = str(c.get("full") or "")
            if cut and not full:row, row2 = [], [{"text": "Нет", "callback_data": f"ap:{c['id']}:deny"}]  # полного текста нет — вслепую не одобряется
            # Что именно одобряется — блоком кода: разметка из команды ([docs](…), **…**) не прячет её часть (охота 16.11).
            what = what.replace("```", "ʼʼʼ")
            text = f"⚠ Предохранитель спрашивает:\n```\n{what}\n```\n{c.get('reason') or ''}" + ("\nВ этом ходе агент читал недоверенное." if c.get("tainted") else "") \
                + (("\nДействие длинное — целиком в файле выше." if full else "\nДействие показано не целиком — одобрить можно только в пульте.") if cut else "")
            # Карточка запоминается за спросившим агентом: ответ на неё цитатой — обычное слово этому агенту, не одобрение.
            self._bg_task(self._card(c["id"], chat, self._card_signed(c, text), [r for r in (row, row2) if r], route=(c.get("agent_id","main"), c.get("session")),
                                     full=full if cut else "", tainted=bool(c.get("tainted"))))
        elif t == "question" and s.get("approvals", True):
            c = ev.get("card") or {}
            opts = c.get("options") or []
            buttons = [[{"text": o[:60], "callback_data": f"q:{c['id']}:{i}"}] for i, o in enumerate(opts[:8])]
            self._bg_task(self._card(c["id"], chat, self._card_signed(c, f"❓ {c.get('question')}" + ("" if opts else "\n(ответь сообщением или цитатой)")), buttons or None, route=(c.get("agent_id","main"), c.get("session"))))
        elif t in ("approval_done", "question_done"):
            ref = self._cards.pop(str(ev.get("id")), None)
            if ref:
                closed = {"timeout": "⌛ Одобрение истекло — не выполнялось", "cancelled": "Ход остановлен — не выполнялось",
                          "steer": "✉ Ты написал сообщение — не выполнялось"}.get(str(ev.get("choice") or ""))
                keyboard = [[{"text": closed, "callback_data": "x"}]] if closed else []  # видно, что это не отказ хозяина (охота 20.11)
                self._bg_task(self.api("editMessageReplyMarkup", {"chat_id": ref[0], "message_id": ref[1], "reply_markup": {"inline_keyboard": keyboard}}))

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

    async def _notice(self, chat, text, ctx, sid=None, *, agent=True, foreign=False):
        """Уведомление запоминается как сказанное агентом: ответ на него придёт с текстом, а не «медиа»."""
        if agent:text=self.signed(ctx,text)
        ids=[]
        mid=await self.send(chat,text,ids=ids,agent=ctx if agent else None)
        # Тема письма, тело вебхука, вывод рефлекса — чужой текст; пульс, напоминание, задача — слова самого агента.
        self.remember(ctx,chat,ids or mid,sid or ctx.main_session(),text,'out',tainted=foreign,foreign=foreign)

    async def _skill_notice(self,chat,message,agent_ctx=None):
        data=message.get("data") or {}
        if not data.get("name"):return
        from ..core.ids import new_id
        token=new_id()[-12:].lower()
        self.app.store.meta_set("skill.notice."+token,{"name":data["name"],"hash":data.get("hash"),"agent_id":(agent_ctx or self.app).agent_id})
        ctx=agent_ctx or self.app
        text=self.signed(ctx,str(message.get("content") or "💾 Навык: "+data["name"]))
        ids=[]
        mid=await self.send(chat,text,buttons=[[{"text":"Убрать навык","callback_data":"sk:"+token}]],ids=ids,agent=ctx)
        self.remember(ctx,chat,ids or mid,ctx.main_session(),text,'out')

    def _card_signed(self, card, text):
        try:ctx=self.app.agents.get(card['agent_id'],archived=True) if card.get('agent_id') else None
        except HumanError:ctx=None
        return self.signed(ctx,text)

    def _card_destination(self, card):
        """Куда карточке одобрения/вопроса: где хозяин говорит с этим ходом (тема или личка), затем тема разговора;
        без адреса — личка. Пропавшая тема чинится при отправке (api → topics.repair)."""
        owner=self.owner() or {};private=owner.get('chat_id') or owner.get('user_id')
        try:asker=self.app.agents.get(card['agent_id'],archived=True) if card.get('agent_id') else self.app
        except HumanError:return private
        sid=card.get('session')
        if not isinstance(sid,str):return private
        turn=asker.mind.turns.get(sid)
        route=(turn.state.get('_telegram_route') if turn else None) or {}
        try:
            chat=self.topics.address(route['chat'],route.get('thread')) if route.get('chat') else self.destination(asker,sid)
        except HumanError:return private
        return chat if chat==private else private

    def _route_only(self, ctx, chat, ids, sid=None):
        """Маршрут без текста служебного сообщения агента (статус, «→ Ответ передан»): ответ цитатой на него — этому агенту."""
        if ctx is None or not ids:return
        from . import telegram_refs
        try:telegram_refs.put_routes(self.app,chat,list(ids),ctx.agent_id,sid if isinstance(sid,str) else None)
        except Exception as error:  # noqa: BLE001 — учёт маршрута не мешает доставке
            self.app.journal.write('channel','Telegram: маршрут служебного сообщения не записан: '+str(error)[:200],actor='telegram')

    def _question_for(self, chat, message):
        """Открытый вопрос, карточку которого хозяин процитировал."""
        if not message:return None
        return next((cid for cid,(c,m) in list(self._cards.items()) if m==message and int(c)==int(chat) and cid in self.app.questions),None)

    async def _card(self, cid: str, chat: int, text: str, buttons, route=None, full: str = "", tainted=False) -> None:
        """tainted — карточка хода, читавшего чужое: в ней может быть чужая команда, цитата карточки остаётся в обёртке."""
        ids=[]
        async with self.sending(chat):  # файл и карточка — подряд, без сообщений соседа между ними
            if full:
                # Длинное действие целиком — файлом перед карточкой: хозяин одобряет с телефона то, что прочитал (охота 20.11).
                try:
                    result=await self.api("sendDocument", {"chat_id": chat, "document": "attach://action", "caption": "Действие целиком"},
                                          files={"action": {"name": "action.txt", "mime": "text/plain", "bytes": full.encode("utf-8")}})
                    if isinstance((result or {}).get('message_id'),int):ids.append(result['message_id'])
                except TelegramError:
                    buttons = [[{"text": "Нет", "callback_data": f"ap:{cid}:deny"}]]
                    text = text.replace("целиком в файле выше", "файл не отправился, одобрить можно только в пульте")
            mid = await self.send(chat, text, buttons=buttons, ids=ids)
        if mid:
            self._cards[cid] = (chat, mid)
            if route:
                # карточка запоминается за спросившим агентом: ответ на неё цитатой (и после закрытия вопроса) идёт ему
                try:
                    asker=self.app.agents.get(route[0] or 'main',archived=True)
                    sid=route[1] if isinstance(route[1],str) else (None if self.app.agents.record(asker.agent_id)['temporary'] else asker.main_session())
                    self.remember(asker,chat,ids or mid,sid,text,'out',tainted=tainted,foreign=tainted)
                except HumanError:pass
