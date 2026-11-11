"""Черновик только активного хода; никаких таймеров или записей в простое."""
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
        self.message_id = None
        self.drafted_at = 0.0  # когда черновик последний раз ушёл в Telegram (закрыть его в конце хода)
        self.head = telegram.badge(agent_ctx)  # чей это черновик — видно сразу
        self.wake = asyncio.Event()
        self.task = asyncio.ensure_future(self.run())

    def feed(self, ev):
        if self.closed or self.quiet: return
        kind = ev.get('type')
        if kind == 'delta' and ev.get('kind') in ('text', 'reasoning'):
            if self.next_block:
                self.intermediate = self.text[-800:]
                self.text, self.reasoning, self.next_block = '', '', False
            if ev['kind'] == 'text': self.text = (self.text + str(ev.get('text') or ''))[-12000:]
            else: self.reasoning = (self.reasoning + str(ev.get('text') or ''))[-8000:]
            self.status = '💭 Думаю…' if ev['kind'] == 'reasoning' else '💬 Отвечаю…'
        elif kind == 'tool_start': self.status = STATUS.get(ev.get('name'), '🔧 Тул: ' + str(ev.get('name') or ''))
        elif kind == 'tool_end': self.status = '💭 Думаю…\n' + ('✓ ' if ev.get('ok') else '⚠ ') + str(ev.get('summary') or ev.get('name') or '')[:180]
        elif kind == 'message' and isinstance(ev.get('message'), dict) and ev['message'].get('role') == 'assistant':
            message = ev['message']
            if isinstance(message.get('content'), str): self.text = message['content'][-12000:]
            self.next_block = bool(message.get('tool_calls'))
        else: return  # пользовательские реплики, содержимое тулов, письма/страницы не копируются
        self.version += 1
        self.wake.set()

    def render(self):
        mode = self.app.settings.group('telegram').get('live', 'full')
        def safe(value, units): return clip(clean(self.app, value, stream=True), units)
        text = safe(self.text, 2200)
        if mode == 'text': return text if self.plain else md_to_html(text)
        status = safe(self.head + self.status, 260)
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
        return draft(safe(self.head+self.status,260),safe('\n'.join(self.reasoning.strip().splitlines()[-4:]),650),
                     safe(self.intermediate,500),safe('\n'.join(self.text.splitlines()[-100:]),12000),
                     text_only=self.app.settings.group('telegram').get('live','full')=='text')

    async def run(self):
        typing = None
        try:
            decision = await self.app.guard.check(Action('msg.send', 'owner', actor='system', to_owner=True, summary='Черновик активного хода Telegram'))
            if not decision.allowed: return
            typing = asyncio.ensure_future(self.telegram._typing(self.chat,alive=lambda:not self.closed))
            while not self.closed:
                if self.app.settings.group('telegram').get('live', 'full') == 'off': break
                if self.quiet:
                    await self.wait(); continue
                now = time.monotonic()
                due = self.telegram._live_next.get(self.chat, 0)
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
                self.telegram._live_next[self.chat] = now + (FALLBACK_INTERVAL if self.fallback else DRAFT_INTERVAL)
                try:
                    result = await self.telegram.api(method, payload, timeout=8)
                    if not self.fallback:self.telegram.set_capability(method,True)
                    if self.fallback and self.message_id is None:
                        self.message_id = result.get('message_id') if isinstance(result, dict) else None
                        if self.message_id is None: self.quiet = True
                    self.sent, self.keepalive = version, time.monotonic()+KEEPALIVE
                    if not self.fallback: self.drafted_at = time.monotonic()
                except TelegramError as exc:
                    code = exc.data.get('code')
                    if code == 429:
                        try: delay = max(1.0, min(86400.0, float(exc.data.get('retry_after') or 1)))
                        except (ValueError, TypeError): delay = 1.0
                        self.telegram._live_next[self.chat] = time.monotonic()+delay
                        self.telegram.pause(self.chat, delay)
                        if delay >= PAUSE_NOTICE: self.quiet = True  # долгая пауза: черновик молчит до конца хода, итог придёт после неё
                    elif 'not modified' in exc.title.lower(): self.sent = version
                    elif self.rich and (self.telegram.unsupported(exc) or code==400):
                        if self.telegram.unsupported(exc):self.telegram.set_capability('sendRichMessageDraft',False)
                        self.rich=False
                        self.fallback=self.telegram.capability('sendMessageDraft') is False
                        self.telegram._live_next[self.chat]=time.monotonic()
                    elif not self.plain and any(s in exc.title.lower() for s in ('parse', 'entit')): self.plain = True
                    elif not self.fallback:
                        self.fallback = True
                        if self.telegram.unsupported(exc): self.telegram.set_capability('sendMessageDraft',False)
                    else: self.quiet = True  # остаётся «печатает…», без повторных сообщений
        finally:
            self.closed = True
            if typing:
                typing.cancel()
                await asyncio.gather(typing, return_exceptions=True)
            if self.message_id is not None:
                try: await self.telegram.api('deleteMessage', {'chat_id':self.chat, 'message_id':self.message_id}, timeout=8)
                except TelegramError: pass

    async def finish(self):
        self.closed = True
        self.wake.set()
        await asyncio.gather(self.task, return_exceptions=True)  # сначала завершить запрос черновика, затем итог
