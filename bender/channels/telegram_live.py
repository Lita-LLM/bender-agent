"""Черновик только активного хода; никаких таймеров или записей в простое.

Несколько агентов стаи в одном месте чата (чат, тема): у Telegram одно место черновика, поэтому черновик ведёт один
ход, остальные — статус-сообщением (не мигает «🤖/🦊» попеременно). Частота правок — общая на место; «печатает…» —
одно на место. Статус-сообщение запоминает маршрут: ответ на него цитатой (и /stop цитатой) — этому агенту."""
from __future__ import annotations

import asyncio
import html
import time

from ..guard.policy import Action
from ..mind.passport import clean
from .telegram import PAUSE_NOTICE, TelegramError, md_to_html

DRAFT_INTERVAL = .9
FALLBACK_INTERVAL = 2.5
KEEPALIVE = 20.0

STATUS = {'read_file':'🔧 Читаю файл', 'look':'👁 Смотрю картинку', 'list_dir':'🔧 Смотрю папку', 'find_files':'🔎 Ищу файлы', 'grep':'🔎 Ищу в файлах',
          'web_search':'🔎 Ищу в сети', 'web_fetch':'🔎 Читаю страницу', 'spawn_task':'👥 Зову помощника', 'wait_tasks':'👥 Жду команду',
          'run':'🔧 Выполняю команду', 'plan':'🔧 Веду план', 'write_file':'🔧 Записываю файл', 'edit_file':'🔧 Правлю файл'}


def clip(text, units):
    """Лимит Telegram — символы UTF-16, в том числе двойные emoji."""
    return str(text).encode('utf-16-le', 'surrogatepass')[:units*2].decode('utf-16-le', 'ignore')


class Live:
    def __init__(self, telegram, chat, sid, draft_id,agent_ctx=None):
        self.telegram, self.app, self.chat, self.sid, self.draft_id = telegram, agent_ctx or telegram.app, chat, sid, draft_id
        self.status = '💭 Думаю…'
        self.text, self.reasoning, self.intermediate = '', '', ''
        self.next_block = False
        self.version, self.sent = 1, 0
        self.keepalive = 0.0
        self.rich=self.app.settings.get('telegram','rich',True) and telegram.capability('sendRichMessageDraft') is not False
        self.fallback = not self.rich and telegram.capability('sendMessageDraft') is False
        if self.app.settings.group('telegram').get('live_via','message')!='draft':
            # Черновик Telegram закрывает хозяину отправку до своего конца; статус-сообщение — нет.
            self.rich,self.fallback=False,True
        self.quiet, self.closed, self.plain = False, False, False
        self.pending, self.inserted = {}, 0  # слова хозяина, пришедшие во время хода: ждут вставки/конца, уже вставлены
        self.message_id = None
        self.keep_status = False  # ответ уйдёт первым, статус удалится после него (на одну задержку Telegram быстрее)
        self.drafted_at = 0.0  # когда черновик последний раз ушёл в Telegram (закрыть его в конце хода)
        self.head = telegram.badge(agent_ctx)  # чей это черновик — видно сразу
        self.slot = telegram.slot(chat)
        self.typing = None
        # Отклик по этапам (telegram.timing): пришло → ход → первое слово модели → хозяин увидел текст.
        self.t_got = telegram._arrived.pop(self.slot, None) if isinstance(self.slot, tuple) else None
        self.t_live, self.t_model, self.t_text = time.monotonic(), None, None
        self.wake = asyncio.Event()
        self.task = asyncio.ensure_future(self.run())

    def _neighbours(self):
        return [live for live in list(self.telegram._live.values()) if live is not self and not live.closed and live.slot == self.slot]

    def _draft_slot(self):
        """Место черновика свободно или наше — берём; занято открытым черновиком соседа — False."""
        holders = self.telegram._draft_holder
        holder = holders.get(self.slot)
        if holder is None or holder is self or holder.closed:
            holders[self.slot] = self
            return True
        return False

    def _start_typing(self):
        """«печатает…» — одно на место чата: у соседа уже идёт — не дублируем (при его конце передаст нам)."""
        if any(live.typing is not None and not live.typing.done() for live in self._neighbours()): return
        self.typing = asyncio.ensure_future(self.telegram._typing(self.chat, alive=lambda: not self.closed))

    def feed(self, ev):
        if self.closed or self.quiet: return
        kind = ev.get('type')
        if self.t_model is None and (kind in ('delta', 'tool_start') or (kind == 'message' and (ev.get('message') or {}).get('role') == 'assistant')):
            self.t_model = time.monotonic()
        if kind == 'delta' and ev.get('kind') in ('text', 'reasoning'):
            if self.next_block:
                self.intermediate = self.text[-800:]
                self.text, self.reasoning, self.next_block = '', '', False
            if ev['kind'] == 'text': self.text = (self.text + str(ev.get('text') or ''))[-12000:]
            else: self.reasoning = (self.reasoning + str(ev.get('text') or ''))[-8000:]
            self.status = '💭 Думаю…' if ev['kind'] == 'reasoning' else '💬 Отвечаю…'
        elif kind == 'tool_start': self.status = STATUS.get(ev.get('name'), '🔧 Тул: ' + str(ev.get('name') or ''))
        elif kind == 'tool_end': self.status = '💭 Думаю…\n' + ('✓ ' if ev.get('ok') else '⚠ ') + str(ev.get('summary') or ev.get('name') or '')[:180]
        elif kind == 'pending': self.pending = {'insert': int(ev.get('insert') or 0), 'queue': int(ev.get('queue') or 0)}
        elif kind == 'message_delivered':
            if not ev.get('mid_turn'): return
            self.inserted += 1
        elif kind == 'message' and isinstance(ev.get('message'), dict) and ev['message'].get('role') == 'assistant':
            message = ev['message']
            if isinstance(message.get('content'), str): self.text = message['content'][-12000:]
            self.next_block = bool(message.get('tool_calls'))
        else: return  # пользовательские реплики, содержимое тулов, письма/страницы не копируются
        self.version += 1
        self.wake.set()

    def delivery(self):
        """Строка о словах хозяина, пришедших во время хода: как они дойдут до агента (agent.new_message, /delivery)."""
        if self.pending.get('insert'): return '↪ Твоё сообщение — в ход на следующем шаге'
        if self.pending.get('queue'): return f"⏳ После хода: сообщений {self.pending['queue']}"
        if self.inserted: return '↪ Твоё сообщение вставлено — агент его видит'
        return ''

    def buttons(self):
        """Разовый выбор для ждущих слов — кнопками статус-сообщения (у черновика кнопок нет: /delivery)."""
        if not (self.pending.get('insert') or self.pending.get('queue')): return []
        other = {'text': '⏳ После хода', 'callback_data': 'nm:queue'} if self.pending.get('insert') else {'text': '↪ Вставить', 'callback_data': 'nm:insert'}
        return [[{'text': '⚡ Прервать и ответить', 'callback_data': 'nm:interrupt'}, other]]

    def render(self):
        mode = self.app.settings.group('telegram').get('live', 'full')
        def safe(value, units): return clip(clean(self.app, value, stream=True), units)
        text = safe(self.text, 2200)
        line = self.delivery()
        if mode == 'text':
            # Только текст — но знак агента остаётся: в общем чате стаи видно, чей это ход. Строка о слове хозяина — тоже.
            if not text and not line: return text
            parts = []
            if line: parts.append(self.head + line if self.plain else html.escape(self.head + line))
            if text: parts.append((self.head if not line else '') + text if self.plain else (html.escape(self.head) if not line else '') + md_to_html(text))
            return '\n\n'.join(parts)
        status = safe(self.head + self.status + ('\n' + line if line else ''), 320)
        thought = safe('\n'.join(self.reasoning.strip().splitlines()[-4:]), 650)
        previous = safe(self.intermediate, 500)
        if self.plain: return '\n\n'.join(x for x in (status, thought, previous, text) if x)
        return '\n\n'.join(x for x in (html.escape(status), '<i>'+html.escape(thought)+'</i>' if thought else '', md_to_html(previous), md_to_html(text)) if x)

    async def wait(self, seconds=None):
        self.wake.clear()
        try:
            if seconds is None: await self.wake.wait()
            elif seconds > 0: await asyncio.wait_for(self.wake.wait(), seconds)
        except asyncio.TimeoutError: pass

    def render_rich(self):
        from .telegram_rich import draft
        def safe(value,units):return clip(clean(self.app,value,stream=True),units)
        text_only=self.app.settings.group('telegram').get('live','full')=='text'
        text=safe('\n'.join(self.text.splitlines()[-100:]),12000)
        if text_only and text and self.head:text=self.head+text
        line=self.delivery()
        return draft(safe(self.head+self.status+('\n'+line if line else ''),320),safe('\n'.join(self.reasoning.strip().splitlines()[-4:]),650),
                     safe(self.intermediate,500),text,text_only=text_only)

    async def run(self):
        try:
            decision = await self.app.guard.check(Action('msg.send', 'owner', actor='system', to_owner=True, summary='Черновик активного хода Telegram'))
            if not decision.allowed: return
            self._start_typing()
            while not self.closed:
                if self.app.settings.group('telegram').get('live', 'full') == 'off': break
                if self.quiet:
                    await self.wait(); continue
                if not self.fallback and not self._draft_slot():
                    # Черновик этого места чата ведёт сосед по стае: наш ход — статус-сообщением, без смены черновиков.
                    self.rich, self.fallback = False, True
                    continue
                if self.fallback and self.telegram._draft_holder.get(self.slot) is self:
                    self.telegram._draft_holder.pop(self.slot, None)  # перешли на статус (старый Bot API) — место черновика свободно
                now = time.monotonic()
                due = self.telegram._live_next.get(self.slot, 0)
                if now < due:
                    await self.wait(due-now); continue
                if self.version == self.sent and (self.fallback or now < self.keepalive):
                    await self.wait(None if self.fallback else self.keepalive-now); continue
                version = self.version
                payload = {'chat_id':self.chat, 'text':self.render(), 'disable_web_page_preview':True}
                if self.fallback and not payload['text']:
                    self.sent = version
                    await self.wait(); continue
                if not self.plain: payload['parse_mode'] = 'HTML'
                if self.fallback:
                    method = 'sendMessage' if self.message_id is None else 'editMessageText'
                    if self.message_id is None: payload['disable_notification'] = True
                    else: payload['message_id'] = self.message_id
                    keyboard = self.buttons()
                    if keyboard or self.message_id is not None: payload['reply_markup'] = {'inline_keyboard': keyboard}
                else:
                    method = 'sendRichMessageDraft' if self.rich else 'sendMessageDraft'
                    payload['draft_id'] = self.draft_id
                    payload.pop('disable_web_page_preview', None)
                    if self.rich:
                        from .telegram_rich import RichFormatError
                        try:rendered=self.render_rich()
                        except RichFormatError:
                            self.rich=False
                            self.fallback=self.telegram.capability('sendMessageDraft') is False
                            continue
                        payload.pop('text',None);payload.pop('parse_mode',None)
                        payload['rich_message']=rendered
                    # Кнопку «Стоп» черновика не просим: она перекрывала ввод и мешала писать агенту. Стоп — командой /stop.
                self.telegram._live_next[self.slot] = now + (FALLBACK_INTERVAL if self.fallback else DRAFT_INTERVAL)
                try:
                    if self.fallback and self.message_id is None:
                        # Новое статус-сообщение не встаёт между кусками чужого длинного ответа.
                        async with self.telegram.sending(self.chat):
                            if self.closed: break
                            result = await self.telegram.api(method, payload, timeout=8)
                    else:
                        result = await self.telegram.api(method, payload, timeout=8)
                    if not self.fallback:self.telegram.set_capability(method,True)
                    if self.fallback and self.message_id is None:
                        self.message_id = result.get('message_id') if isinstance(result, dict) else None
                        if self.message_id is None: self.quiet = True
                        else: self._remember_status()
                    self.sent, self.keepalive = version, time.monotonic()+KEEPALIVE
                    if self.t_text is None and self.text: self.t_text = time.monotonic()
                    if not self.fallback: self.drafted_at = time.monotonic()
                except TelegramError as exc:
                    code = exc.data.get('code')
                    if code == 429:
                        try: delay = max(1.0, min(86400.0, float(exc.data.get('retry_after') or 1)))
                        except (ValueError, TypeError): delay = 1.0
                        self.telegram._live_next[self.slot] = time.monotonic()+delay
                        self.telegram.pause(self.chat, delay)
                        if delay >= PAUSE_NOTICE: self.quiet = True  # долгая пауза: черновик молчит до конца хода, итог придёт после неё
                    elif 'not modified' in exc.title.lower(): self.sent = version
                    elif self.rich and (self.telegram.unsupported(exc) or code==400):
                        if self.telegram.unsupported(exc):self.telegram.set_capability('sendRichMessageDraft',False)
                        self.rich=False
                        self.fallback=self.telegram.capability('sendMessageDraft') is False
                        self.telegram._live_next[self.slot]=time.monotonic()
                    elif not self.plain and any(s in exc.title.lower() for s in ('parse', 'entit')): self.plain = True
                    elif not self.fallback:
                        self.fallback = True
                        if self.telegram.unsupported(exc): self.telegram.set_capability('sendMessageDraft',False)
                    else: self.quiet = True  # остаётся «печатает…», без повторных сообщений
        finally:
            self.closed = True
            if self.telegram._draft_holder.get(self.slot) is self: self.telegram._draft_holder.pop(self.slot, None)
            typing, self.typing = self.typing, None
            if typing:
                typing.cancel()
                await asyncio.gather(typing, return_exceptions=True)
                for live in self._neighbours():  # «печатает…» переходит к соседу, который ещё работает
                    if live.typing is None or live.typing.done(): live._start_typing(); break
            if not self.keep_status: await self.drop_status()

    async def drop_status(self):
        message_id, self.message_id = self.message_id, None
        if message_id is not None:
            try: await self.telegram.api('deleteMessage', {'chat_id':self.chat, 'message_id':message_id}, timeout=8)
            except TelegramError: pass

    def _remember_status(self):
        """Маршрут статус-сообщения (без текста): ответ на него цитатой и /stop цитатой — этому агенту."""
        statuses = self.telegram._status_messages
        statuses[(int(self.chat), self.message_id)] = (self.app.agent_id, self.sid)
        while len(statuses) > 500: statuses.pop(next(iter(statuses)))
        self.telegram._route_only(self.app, self.chat, [self.message_id], self.sid)

    async def finish(self):
        self.closed = True
        self.wake.set()
        await asyncio.gather(self.task, return_exceptions=True)  # сначала завершить запрос черновика, затем итог
