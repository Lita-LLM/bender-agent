"""Старый отдельный HTTP+SSE: один входящий поток и POST на проверенный endpoint."""
import asyncio
import json
from urllib.parse import urljoin, urlsplit

from ..core.errors import HumanError
from ..net.http_client import Timeouts
from ..net.sse import parse_sse
from .client import LIMIT, _message, input_reply


def failed(hint):return HumanError('mcp_sse','Не удалось продолжить MCP по HTTP+SSE',hint)


def endpoint_url(base,value):
    """Не даёт чужому endpoint унести заголовок авторизации на другой origin."""
    if not isinstance(value,str) or not value or len(value)>8192 or any(ord(c)<33 for c in value) or '\\' in value:raise failed('Сервер прислал неверный endpoint.')
    url=urljoin(base,value);original=urlsplit(base);target=urlsplit(url)
    def origin(parts):return parts.scheme.lower(),parts.hostname,(parts.port or (443 if parts.scheme=='https' else 80))
    if target.username or target.password or target.fragment or origin(target)!=origin(original):raise failed('Endpoint другого origin запрещён; проверь адрес сервера.')
    return url


class LegacySse:
    def __init__(self,client,url,headers,timeout):
        self.client,self.url,self.headers,self.timeout=client,url,headers,timeout
        self.endpoint=None;self.reader=None;self.response=None;self.ready=asyncio.Event()
        self.queue=asyncio.Queue(maxsize=32);self.queued_bytes=0;self.closed=False;self.error=None

    async def read(self):
        try:
            headers={key:value for key,value in self.headers.items() if key.lower()!='accept' and not key.lower().startswith('mcp-')}
            response=await self.client.get(self.url,headers={**headers,'Accept':'text/event-stream'},timeouts=Timeouts(connect=min(15,self.timeout),read=max(300,self.timeout)),retry_stale=False)
            self.response=response
            async with response:
                if not response.ok or (response.headers.get('content-type') or '').split(';')[0].strip().lower()!='text/event-stream':raise failed('Нужен успешный SSE-ответ без редиректов; получен HTTP '+str(response.status)+'.')
                async def lines():
                    buffer=b'';frame=0
                    async for chunk in response.iter_bytes():
                        buffer+=chunk
                        if len(buffer)>LIMIT:raise failed('Строка SSE больше 2 МБ.')
                        while b'\n' in buffer:
                            line,buffer=buffer.split(b'\n',1);frame+=len(line)+1
                            if frame>LIMIT:raise failed('Событие SSE больше 2 МБ.')
                            if not line.rstrip(b'\r'):frame=0
                            yield line.rstrip(b'\r').decode('utf-8')
                    if buffer:yield buffer.decode('utf-8')
                async for event in parse_sse(lines()):
                    if event.event=='endpoint':
                        value=endpoint_url(self.url,event.data)
                        if self.endpoint is not None:raise failed('Сервер повторно объявил endpoint; переподключи его.')
                        self.endpoint=value;self.ready.set()
                    elif event.event=='message' and event.data:
                        if self.endpoint is None:raise failed('Сообщение пришло до endpoint.')
                        # Нельзя незаметно накопить очередь/много мегабайт в бездействии.
                        size=len(event.data.encode('utf-8'))
                        if self.queued_bytes+size>LIMIT:raise failed('Очередь SSE больше 2 МБ.')
                        self.queue.put_nowait((_message(json.loads(event.data)),size));self.queued_bytes+=size
            raise failed('Входящий поток закрыт. Действие могло выполниться; проверь результат до повтора.')
        except asyncio.CancelledError:raise
        except Exception as error:
            self.error=error if isinstance(error,HumanError) else failed('Сломанный/переполненный SSE-поток; переподключи сервер.')
        finally:
            self.closed=True;self.ready.set()

    async def post(self,msg):
        headers={key:value for key,value in self.headers.items() if not key.lower().startswith('mcp-')}
        response=await self.client.post(self.endpoint,headers=headers,json=msg,timeouts=Timeouts(connect=min(15,self.timeout),read=self.timeout,total=self.timeout),retry_stale=False)
        async with response:
            if not response.ok:raise failed('POST отклонён: HTTP '+str(response.status)+'. Редиректы и повторы не выполняются.')
            await response.read(limit=LIMIT)

    async def exchange(self,msg,on_progress=None,*,input_handler=None):
        if self.reader is None:self.reader=asyncio.create_task(self.read(),name='mcp-legacy-sse')
        await self.ready.wait()
        if self.closed:raise self.error or failed('Соединение закрыто; подключи сервер заново.')
        await self.post(msg)
        if 'id' not in msg:return None
        total=0
        for _ in range(256):
            waiting=asyncio.create_task(self.queue.get())
            try:
                done,_=await asyncio.wait((waiting,self.reader),return_when=asyncio.FIRST_COMPLETED)
                if waiting not in done:raise self.error or failed('Поток оборвался; действие не повторяется.')
                received,size=waiting.result();self.queued_bytes-=size
            finally:
                if not waiting.done():waiting.cancel()
                await asyncio.gather(waiting,return_exceptions=True)
            total+=len(json.dumps(received,ensure_ascii=False).encode('utf-8'))
            if total>LIMIT:raise failed('Ответ со служебными событиями больше 2 МБ.')
            if received.get('id')==msg['id'] and 'method' not in received:return received
            if 'id' in received and 'method' in received:
                await self.post(await input_reply(received,input_handler))
            elif received.get('method')=='notifications/progress' and on_progress:on_progress(str((received.get('params') or {}).get('message') or 'MCP-сервер работает')[:500])
        raise failed('Слишком много посторонних сообщений; проверь сервер.')

    async def close(self):
        self.closed=True
        if self.reader and not self.reader.done():self.reader.cancel()
        if self.reader:await asyncio.gather(self.reader,return_exceptions=True)
        if self.response:await self.response.aclose()
        self.endpoint=None
        while not self.queue.empty():self.queue.get_nowait()
        self.queued_bytes=0
