"""MCP без сторонних библиотек: JSON-RPC по stdio и Streamable HTTP.

Нет автоматического повтора tools/call после обрыва. Встречные права выключены
по умолчанию; разрешённый MRTR продолжается только после проверенного ввода.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
from pathlib import Path

from ..core.errors import HumanError
from .. import __version__
from ..net.http_client import NetError, Timeouts
from .headers import annotations, header_value, parameter_headers

NEW = "2026-07-28"
VERSIONS = (NEW, "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
LIMIT = 2 * 1024 * 1024


class RpcError(HumanError):
    def __init__(self, error):
        self.rpc_code = error.get("code")
        super().__init__("mcp_rpc", "MCP-сервер отклонил запрос", "Проверь его настройки и выбранный тул.",
                         detail=str(error.get("message", ""))[:2000])


def _message(raw):
    if not isinstance(raw, dict) or raw.get("jsonrpc") != "2.0":
        raise HumanError("mcp_protocol", "Ответ сервера не похож на MCP", "Нужен JSON-RPC 2.0.")
    return raw


async def input_reply(request,handler):
    """Ответ серверу без раскрытия внутренних ошибок/секретов и без прав по умолчанию."""
    reply={'jsonrpc':'2.0','id':request['id']}
    if handler is None:return {**reply,'error':{'code':-32601,'message':'Возможности клиента выключены'}}
    try:return {**reply,'result':await handler(request)}
    except asyncio.CancelledError:raise
    except Exception as error:
        if hasattr(handler,'rejected'):handler.rejected(request.get('method'),error)
        return {**reply,'error':{'code':-32000,'message':'Клиент отклонил запрос; подробности доступны только хозяину'}}


class Stdio:
    def __init__(self, argv: list[str], *, cwd: Path, env: dict[str, str], cleanup=None,resource=None,limit=LIMIT,log=None):
        self.argv, self.cwd, self.env = argv, cwd, env
        self.limit=limit
        self.cleanup = cleanup
        self.resource=resource
        self.process = None
        self.log = log  # deque строк stderr: журнал сервера для хозяина и mcp_diagnose
        self.closed = False
        self.returncode = None
        self._reader = None

    async def start(self):
        try:
            self.process = await asyncio.create_subprocess_exec(
                *self.argv, cwd=str(self.cwd), env=self.env, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE if self.log is not None else asyncio.subprocess.DEVNULL,
                limit=self.limit + 1, start_new_session=os.name != "nt")
            if self.resource and hasattr(self.resource,'bind'):self.resource.bind(self.process)
        except OSError as exc:
            raise HumanError("mcp_start", "Не удалось запустить MCP-сервер", "Проверь программу, аргументы и рабочую папку.",
                             detail=str(exc)) from None
        if self.log is not None:
            self._reader = asyncio.get_running_loop().create_task(self._drain(self.process.stderr))

    async def _drain(self, stream):
        # stderr читаем всегда: иначе переполненный канал остановит сервер
        while True:
            try:
                line = await stream.readline()
            except ValueError:  # строка длиннее лимита — пропускаем кусок
                line = await stream.read(65536)
            except Exception:
                return
            if not line:
                return
            self.log.append(line.decode("utf-8", "replace").rstrip()[:2000])

    def alive(self):
        return not self.closed and self.process is not None and self.process.returncode is None

    async def send(self, msg):
        if self.closed:
            raise HumanError("mcp_closed", "Подключение к MCP-серверу закрыто", "Повтори действие: Бендер подключится заново.")
        if self.process is None:
            await self.start()
        if self.process.returncode is not None:
            self.returncode = self.process.returncode
            raise HumanError("mcp_closed", "MCP-сервер завершился", f"Код выхода {self.process.returncode}. Журнал сервера — тул mcp_diagnose или раздел «MCP».")
        self.process.stdin.write(json.dumps(msg, ensure_ascii=False).encode() + b"\n")
        await self.process.stdin.drain()

    async def exchange(self, msg, on_progress=None, *, input_handler=None):
        await self.send(msg)
        if "id" not in msg:
            return None
        for _ in range(256):
            line = await self.process.stdout.readline()
            if not line:
                raise HumanError("mcp_closed", "MCP-сервер закрыл ответ", "Действие могло выполниться; проверь результат до повторения.")
            if len(line) > self.limit:
                raise HumanError("mcp_size", "Слишком большой ответ MCP", "Сократи результат на стороне сервера.")
            received = _message(json.loads(line))
            if received.get("id") == msg["id"] and "method" not in received:
                return received
            if "method" in received and "id" in received:
                await self.send(await input_reply(received,input_handler))
            elif received.get("method") == "notifications/progress" and on_progress:
                on_progress(str((received.get("params") or {}).get("message") or "MCP-сервер работает")[:500])
        raise HumanError("mcp_protocol", "Сервер прислал слишком много посторонних сообщений", "Проверь его журнал.")

    async def close(self):
        self.closed = True
        job=getattr(self.process,'_bender_job',None)
        if job is not None:job.close()
        proc, self.process = self.process, None
        if proc is None:
            if self.cleanup:
                self.cleanup()
            return
        if proc.returncode is None:
            try:
                if os.name != "nt":
                    os.killpg(proc.pid, signal.SIGTERM)
                else:
                    proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 2)
            except asyncio.TimeoutError:
                try:
                    if os.name != "nt":
                        os.killpg(proc.pid, signal.SIGKILL)
                    else:
                        proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
        self.returncode = proc.returncode
        if self._reader is not None:
            try:
                await asyncio.wait_for(self._reader, 1)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            self._reader = None
        # wait() уже завершившегося процесса не дожидается закрытия каналов.
        # Явно закрываем транспорт asyncio, включая stdout и буфер stdin.
        proc._transport.close()
        if proc.stdin:
            try:
                await proc.stdin.wait_closed()
            except (BrokenPipeError, ConnectionResetError):
                pass
        if self.cleanup:
            self.cleanup()


class Http:
    def __init__(self, client, url: str, headers: dict, timeout: float):
        self.client, self.url, self.headers, self.timeout = client, url, headers, timeout
        self.session = None

    async def exchange(self, msg, on_progress=None, *, schema=None, input_handler=None):
        headers = {**{key:value for key,value in self.headers.items() if not key.lower().startswith('mcp-') and key.lower()!='accept'}, "Accept": "application/json, text/event-stream"}
        params = msg.get("params") or {}
        version = (params.get("_meta") or {}).get("io.modelcontextprotocol/protocolVersion")
        if version:
            headers["MCP-Protocol-Version"] = version
        headers["Mcp-Method"] = msg["method"]
        if msg["method"] in ('tools/call','prompts/get','resources/read'):
            headers["Mcp-Name"] = header_value(params['uri'] if msg['method']=='resources/read' else params['name'])
        if msg['method'] in ('tasks/get','tasks/update','tasks/cancel'):headers['Mcp-Name']=header_value(params['taskId'])
        if version==NEW and msg['method']=='tools/call' and schema is not None:headers.update(parameter_headers(schema,params.get('arguments',{})))
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        response = await self.client.post(self.url, headers=headers, json=msg,
                                          timeouts=Timeouts(connect=min(15, self.timeout), read=self.timeout, total=input_handler.timeout if input_handler else self.timeout), retry_stale=False)
        async with response:
            if not response.ok:
                if msg["method"] == "server/discover" and response.status in (400, 404, 405):
                    # Старый Streamable HTTP может требовать initialize ещё до разбора метода.
                    # 401/403, ошибки прокси и таймауты сюда не попадают.
                    raw = await response.read(limit=LIMIT)
                    try:
                        error = json.loads(raw).get("error") or {}
                    except (ValueError, AttributeError):
                        error = {}
                    if error.get("code") not in (None, -32600, -32601, -32000, -32022):
                        raise RpcError(error)
                    raise HumanError("mcp_legacy", "Сервер не поддерживает обнаружение без сессии", "Пробую initialize.")
                raise HumanError("mcp_http", f"MCP-сервер ответил HTTP {response.status}",
                                 "Проверь адрес, доступ и секреты; редиректы автоматически не выполняются.")
            if msg["method"] == "initialize":
                self.session = response.headers.get("mcp-session-id")
            if "id" not in msg:
                await response.read(limit=LIMIT)
                return None
            ctype = (response.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
            if ctype == "application/json":
                return _message(json.loads(await response.read(limit=LIMIT)))
            if ctype != "text/event-stream":
                raise HumanError("mcp_protocol", "MCP-сервер не прислал JSON или SSE", "Проверь адрес MCP-эндпоинта.")
            # Ограничиваем весь поток, в том числе комментарии и незавершённые строки.
            async def lines():
                total, buf = 0, b""
                async for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > LIMIT:
                        raise HumanError("mcp_size", "Слишком большой поток MCP", "Сократи результат на стороне сервера.")
                    buf += chunk
                    while b"\n" in buf:
                        line, buf = buf.split(b"\n", 1)
                        yield line.rstrip(b"\r").decode("utf-8")
                if buf:
                    yield buf.decode("utf-8")
            from ..net.sse import parse_sse

            async for event in parse_sse(lines()):
                if not event.data:
                    continue
                received = _message(json.loads(event.data))
                if received.get("id") == msg["id"] and "method" not in received:
                    return received
                if "method" in received and "id" in received:
                    if input_handler is None:raise HumanError('mcp_capability','Сервер запросил выключенную возможность клиента','Разрешения — в подключении MCP.')
                    reply=await input_reply(received,input_handler)
                    response_headers={key:value for key,value in headers.items() if key.lower() not in ('mcp-method','mcp-name') and not key.lower().startswith('mcp-param-')}
                    sent=await self.client.post(self.url,headers=response_headers,json=reply,timeouts=Timeouts(read=self.timeout,total=self.timeout),retry_stale=False)
                    async with sent:
                        if not sent.ok:raise HumanError('mcp_http','Сервер не принял ответ клиента','Действие не повторяется.')
                        await sent.read(limit=LIMIT)
                if received.get("method") == "notifications/progress" and on_progress:
                    on_progress(str((received.get("params") or {}).get("message") or "MCP-сервер работает")[:500])
            raise HumanError("mcp_closed", "Поток MCP оборвался без результата", "Действие могло выполниться; не повторяй его вслепую.")

    async def close(self):
        self.session = None


class Client:
    def __init__(self, transport, *, timeout: float = 30):
        self.transport, self.timeout = transport, timeout
        self.version = None
        self.info = {}
        self.capabilities = {}
        self.rejected_tools=[]
        self._next = 0
        self._lock = asyncio.Lock()

    async def request(self, method: str, params=None, *, notify=False, progress=None, schema=None, input_handler=None, task_support=False):
        async with self._lock:
            current=dict(params or {})
            for round_number in range(4):
                result=await self._once(method,current,notify=notify,progress=progress,schema=schema,input_handler=input_handler,task_support=task_support)
                if result.get('resultType','complete')=='complete':return result
                if result.get('resultType')=='task' and task_support and self.version==NEW and method=='tools/call':return result
                if self.version!=NEW or result.get('resultType')!='input_required' or input_handler is None:raise HumanError('mcp_capability','Серверу нужны дополнительные возможности клиента','Включи нужную возможность в MCP; действие не повторяется автоматически.')
                if round_number==3:raise HumanError('mcp_input','Сервер исчерпал четыре раунда MCP','Новый ввод не запрашивается, действие не продолжается.')
                requests=result.get('inputRequests');state=result.get('requestState')
                if not isinstance(requests,dict) or not requests or len(requests)>8 or any(not isinstance(key,str) or len(key)>128 or not isinstance(value,dict) for key,value in requests.items()):raise HumanError('mcp_input','Неверный/пустой запрос ввода MRTR','Без запросов ввода продолжение не отправляется.')
                if state is not None and (not isinstance(state,str) or len(state)>65536):raise HumanError('mcp_input','Неверное состояние MRTR','Проверь MCP-сервер.')
                responses={}
                for key,request in requests.items():
                    try:responses[key]=await asyncio.wait_for(input_handler(request),input_handler.timeout)
                    except asyncio.TimeoutError:raise HumanError('mcp_input','Время ввода MCP истекло','Запрос не продолжен; проверь результат на сервере.') from None
                input_handler.validate()
                current={**dict(params or {}),'inputResponses':responses}
                if state is not None:current['requestState']=state
            raise HumanError('mcp_input','Сервер исчерпал четыре раунда MCP','Действие не продолжается автоматически.')

    async def _once(self, method, params=None, *, notify=False, progress=None, schema=None, input_handler=None, task_support=False):
        # Сериализацией всего обмена, включая MRTR, управляет request.
        self._next += 1
        mid = f"bender-{self._next}"
        params = dict(params or {})
        version=self.version or (NEW if method=='server/discover' else None)
        if version:
            params["_meta"] = {**params.get("_meta", {}), "io.modelcontextprotocol/protocolVersion": version,
                               'io.modelcontextprotocol/clientInfo':{'name':'bender','version':__version__},
                               "io.modelcontextprotocol/clientCapabilities": input_handler.capabilities() if input_handler else {}}
        if task_support and version==NEW:
            caps=params['_meta']['io.modelcontextprotocol/clientCapabilities']
            caps['extensions']={**caps.get('extensions',{}),'io.modelcontextprotocol/tasks':{}}
        msg = {"jsonrpc": "2.0", "method": method, "params": params}
        if not notify:
            msg["id"] = mid
        try:
            kwargs={'schema':schema} if isinstance(self.transport,Http) else {}
            if input_handler is not None:kwargs['input_handler']=input_handler
            exchange=self.transport.exchange(msg,progress,**kwargs)
            response = await asyncio.wait_for(exchange, input_handler.timeout if input_handler else self.timeout)
        except asyncio.CancelledError:
            await self.transport.close()
            self.version = None
            raise
        except (asyncio.TimeoutError, OSError, ValueError, NetError) as exc:
            await self.transport.close()
            self.version = None
            raise HumanError("mcp_transport", "Не удалось получить ответ MCP-сервера",
                             "Проверь его запуск и доступность. Действие могло выполниться; перед повтором проверь результат.",
                             detail=str(exc)[:2000]) from None
        if notify:
            return {}
        if not isinstance(response, dict) or response.get("id") != mid:
            raise HumanError("mcp_protocol", "Ответ MCP относится к другому запросу", "Переподключи сервер.")
        if "error" in response:
            if not isinstance(response["error"], dict):
                raise HumanError("mcp_protocol", "Некорректная ошибка MCP", "Проверь сервер.")
            raise RpcError(response["error"])
        result = response.get("result")
        if not isinstance(result, dict):
            raise HumanError("mcp_protocol", "В ответе MCP нет результата", "Проверь сервер.")
        return result

    async def connect(self,*,input_handler=None):
        try:
            discovered = await self.request("server/discover",input_handler=input_handler)
        except HumanError as exc:
            if exc.code != "mcp_legacy" and not (isinstance(exc, RpcError) and exc.rpc_code in (-32601, -32022)):
                raise
        else:
            if NEW in discovered.get("supportedVersions", []):
                self.version, self.info = NEW, discovered.get("serverInfo") or {}
                self.capabilities=discovered.get('capabilities') or {}
                return
        initialized = await self.request("initialize", {"protocolVersion": "2025-11-25", "capabilities": input_handler.capabilities() if input_handler else {},
                                                        "clientInfo": {"name": "bender", "version": __version__}},input_handler=input_handler)
        version = initialized.get("protocolVersion")
        if version not in VERSIONS or version == NEW:
            raise HumanError("mcp_version", "Версия MCP-сервера не поддерживается", str(version))
        self.version, self.info = version, initialized.get("serverInfo") or {}
        self.capabilities=initialized.get('capabilities') or {}
        await self.request("notifications/initialized", notify=True)

    async def tools(self):
        tools=await self.pages('tools/list','tools');self.rejected_tools=[];valid=[]
        for tool in tools:
            try:
                if isinstance(self.transport,Http) and self.version==NEW:annotations(tool.get('inputSchema'))
            except HumanError as error:self.rejected_tools.append({'name':str(tool.get('name','')),'reason':error.hint})
            else:valid.append(tool)
        return valid

    async def pages(self,method,key):
        """Ограниченная пагинация всех стандартных каталогов; курсор сервера не исполняется."""
        out, cursor, seen, size = [], None, set(), 0
        for _ in range(20):
            result = await self.request(method, {"cursor": cursor} if cursor else {})
            page = result.get(key)
            if not isinstance(page, list) or any(not isinstance(t, dict) for t in page):
                raise HumanError("mcp_tools", "Некорректный каталог MCP", "Проверь сервер: "+method+'.')
            out.extend(page)
            try:size+=len(json.dumps(page,ensure_ascii=False,allow_nan=False).encode('utf-8'))
            except (ValueError,UnicodeError):raise HumanError('mcp_tools','Каталог MCP содержит неверный JSON/UTF-8','Проверь сервер.') from None
            if size>LIMIT:raise HumanError('mcp_size','Каталог MCP больше 2 МБ','Сократи определения на сервере.')
            if len(out) > 200:
                break
            cursor = result.get("nextCursor")
            if not cursor:
                return out
            if not isinstance(cursor, str) or len(cursor)>8192 or cursor in seen:
                break
            seen.add(cursor)
        raise HumanError("mcp_tools", "Слишком большой или зацикленный каталог MCP", "Не более 200 записей и 20 страниц: "+method+'.')

    async def close(self):
        await self.transport.close()
        self.version = None
        self.capabilities = {}
