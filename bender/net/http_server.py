"""Маленький HTTP/1.1-сервер на asyncio — без сторонних библиотек.

Умеет ровно то, что нужно пульту, фальшивой модели, вебхукам и MCP по HTTP:
- разбор запроса с ограничениями размеров (заголовки, тело) и таймаутами (от медленных клиентов);
- keep-alive: несколько запросов в одном соединении;
- ответы целиком (JSON, текст, байты), файлы кусками, потоковые ответы (chunked) — для SSE;
- маршруты с параметрами в пути: `/api/chat/{id}`.

Безопасность (проверка Host, Origin, токен) — забота приложения, а не сервера: сервер
только честно отдаёт заголовки и адрес клиента.
"""

from __future__ import annotations

import asyncio
import json
import mimetypes
import re
from dataclasses import dataclass, field
from email.utils import formatdate
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable
from urllib.parse import parse_qs, unquote, urlsplit

MAX_LINE = 16 * 1024
MAX_HEADERS = 100
MAX_BODY = 64 * 1024 * 1024
HEADER_TIMEOUT = 30.0
BODY_TIMEOUT = 120.0
KEEPALIVE_TIMEOUT = 30.0

REASONS = {
    200: "OK", 201: "Created", 202: "Accepted", 204: "No Content", 206: "Partial Content",
    301: "Moved Permanently", 302: "Found", 304: "Not Modified",
    400: "Bad Request", 401: "Unauthorized", 402: "Payment Required", 403: "Forbidden", 404: "Not Found",
    405: "Method Not Allowed", 408: "Request Timeout", 409: "Conflict", 413: "Payload Too Large",
    415: "Unsupported Media Type", 421: "Misdirected Request", 422: "Unprocessable Entity", 423: "Locked",
    428: "Precondition Required", 429: "Too Many Requests", 431: "Request Header Fields Too Large",
    500: "Internal Server Error", 501: "Not Implemented", 502: "Bad Gateway", 503: "Service Unavailable",
    504: "Gateway Timeout", 529: "Overloaded",
}


class Headers:
    """Заголовки без учёта регистра имён, с сохранением порядка и повторов."""

    def __init__(self, items: list[tuple[str, str]] | None = None):
        self.items: list[tuple[str, str]] = list(items or [])

    def get(self, name: str, default: str | None = None) -> str | None:
        name = name.lower()
        for k, v in self.items:
            if k.lower() == name:
                return v
        return default

    def get_all(self, name: str) -> list[str]:
        name = name.lower()
        return [v for k, v in self.items if k.lower() == name]

    def __contains__(self, name: str) -> bool:
        return self.get(name) is not None

    def set(self, name: str, value: str) -> None:
        low = name.lower()
        self.items = [(k, v) for k, v in self.items if k.lower() != low]
        self.items.append((name, value))

    def add(self, name: str, value: str) -> None:
        self.items.append((name, value))

    def to_dict(self) -> dict[str, str]:
        return {k.lower(): v for k, v in self.items}


@dataclass
class Request:
    method: str
    target: str
    path: str
    query: dict[str, list[str]]
    version: str
    headers: Headers
    body: bytes
    client: tuple[str, int] | None
    params: dict[str, str] = field(default_factory=dict)
    state: dict = field(default_factory=dict)

    def q(self, name: str, default: str | None = None) -> str | None:
        vals = self.query.get(name)
        return vals[0] if vals else default

    def json(self):
        if not self.body:
            return None
        return json.loads(self.body.decode("utf-8"))

    @property
    def host(self) -> str:
        return (self.headers.get("host") or "").strip()


class Response:
    def __init__(self, body: bytes | str = b"", status: int = 200, headers: dict | list | None = None, content_type: str | None = None):
        self.status = status
        self.headers = Headers(list(headers.items()) if isinstance(headers, dict) else list(headers or []))
        self.body = body.encode("utf-8") if isinstance(body, str) else body
        if content_type and "content-type" not in self.headers:
            self.headers.set("Content-Type", content_type)

    async def send(self, writer: asyncio.StreamWriter, head_only: bool = False) -> bool:
        """Отправить ответ. Возвращает, можно ли продолжать соединение."""
        self.headers.set("Content-Length", str(len(self.body)))
        writer.write(_head(self.status, self.headers))
        if not head_only and self.body:
            writer.write(self.body)
        await writer.drain()
        return True


class JSONResponse(Response):
    def __init__(self, data, status: int = 200, headers: dict | None = None):
        body = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        super().__init__(body, status, headers, "application/json; charset=utf-8")


class TextResponse(Response):
    def __init__(self, text: str, status: int = 200, headers: dict | None = None, content_type: str = "text/plain; charset=utf-8"):
        super().__init__(text, status, headers, content_type)


class StreamResponse(Response):
    """Потоковый ответ: тело приходит из асинхронного генератора кусками (chunked). Для SSE и долгих потоков."""

    def __init__(self, chunks: AsyncIterator[bytes], status: int = 200, headers: dict | None = None, content_type: str = "text/event-stream; charset=utf-8"):
        super().__init__(b"", status, headers, content_type)
        self.chunks = chunks

    async def send(self, writer: asyncio.StreamWriter, head_only: bool = False) -> bool:
        self.headers.set("Transfer-Encoding", "chunked")
        if "cache-control" not in self.headers:
            self.headers.set("Cache-Control", "no-cache")
        writer.write(_head(self.status, self.headers))
        await writer.drain()
        if head_only:
            return True
        try:
            async for chunk in self.chunks:
                if not chunk:
                    continue
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")
                writer.write(b"%x\r\n%s\r\n" % (len(chunk), chunk))
                await writer.drain()
            writer.write(b"0\r\n\r\n")
            await writer.drain()
        finally:
            closer = getattr(self.chunks, "aclose", None)
            if closer:
                try:
                    await closer()
                except Exception:  # noqa: BLE001
                    pass
        return True


class FileResponse(Response):
    def __init__(self, path: Path | str, status: int = 200, headers: dict | None = None, content_type: str | None = None):
        self.path = Path(path)
        ctype = content_type or mimetypes.guess_type(self.path.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json", "image/svg+xml"):
            ctype += "; charset=utf-8"
        super().__init__(b"", status, headers, ctype)

    async def send(self, writer: asyncio.StreamWriter, head_only: bool = False) -> bool:
        size = self.path.stat().st_size
        self.headers.set("Content-Length", str(size))
        writer.write(_head(self.status, self.headers))
        await writer.drain()
        if head_only:
            return True
        with self.path.open("rb") as fh:
            while True:
                chunk = fh.read(64 * 1024)
                if not chunk:
                    break
                writer.write(chunk)
                await writer.drain()
        return True


def sse(data, event: str | None = None, id: str | None = None) -> bytes:
    """Одно событие Server-Sent Events. data — строка или что-то, что станет JSON."""
    if not isinstance(data, str):
        data = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    out = []
    if id is not None:
        out.append(f"id: {id}")
    if event:
        out.append(f"event: {event}")
    for ln in data.split("\n"):
        out.append(f"data: {ln}")
    return ("\n".join(out) + "\n\n").encode("utf-8")


def _head(status: int, headers: Headers) -> bytes:
    lines = [f"HTTP/1.1 {status} {REASONS.get(status, 'Status')}"]
    if "date" not in headers:
        headers.set("Date", formatdate(usegmt=True))
    if "server" not in headers:
        headers.set("Server", "bender")
    for k, v in headers.items:
        if "\r" in k or "\n" in k or "\r" in v or "\n" in v:
            raise ValueError("перевод строки в заголовке ответа")
        lines.append(f"{k}: {v}")
    return ("\r\n".join(lines) + "\r\n\r\n").encode("latin-1")


Handler = Callable[[Request], Awaitable[Response]]


class Router:
    """Маршруты вида `/api/chat/{id}` и `/static/{path:rest}` (rest — остаток пути со слэшами)."""

    def __init__(self):
        self.routes: list[tuple[str, re.Pattern, Handler]] = []

    def add(self, method: str, pattern: str, handler: Handler) -> None:
        regex = "^"
        for part in re.split(r"(\{[^}]+\})", pattern):
            if part.startswith("{") and part.endswith("}"):
                name, _, kind = part[1:-1].partition(":")
                regex += f"(?P<{name}>.+)" if kind == "rest" else f"(?P<{name}>[^/]+)"
            else:
                regex += re.escape(part)
        regex += "$"
        self.routes.append((method.upper(), re.compile(regex), handler))

    def route(self, method: str, pattern: str):
        def deco(fn: Handler) -> Handler:
            self.add(method, pattern, fn)
            return fn

        return deco

    def match(self, method: str, path: str) -> tuple[Handler | None, dict[str, str], bool]:
        """(обработчик, параметры, путь вообще существует)."""
        path_known = False
        for m, rx, handler in self.routes:
            mo = rx.match(path)
            if not mo:
                continue
            path_known = True
            if m == method or (method == "HEAD" and m == "GET"):
                return handler, {k: unquote(v) for k, v in mo.groupdict().items()}, True
        return None, {}, path_known

    async def __call__(self, request: Request) -> Response:
        handler, params, known = self.match(request.method, request.path)
        if handler is None:
            if known:
                return JSONResponse({"error": {"code": "method", "title": "Метод не поддерживается"}}, 405)
            return JSONResponse({"error": {"code": "not_found", "title": "Нет такого адреса"}}, 404)
        request.params = params
        return await handler(request)


class BadRequest(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


async def _read_line(reader: asyncio.StreamReader) -> bytes:
    try:
        line = await reader.readuntil(b"\n")
    except asyncio.LimitOverrunError:
        raise BadRequest(431, "слишком длинная строка") from None
    except asyncio.IncompleteReadError as e:
        if not e.partial:
            raise ConnectionResetError("клиент закрыл соединение") from None
        raise BadRequest(400, "оборванный запрос") from None
    if len(line) > MAX_LINE:
        raise BadRequest(431, "слишком длинная строка")
    return line


async def read_request(reader: asyncio.StreamReader, client, max_body: int = MAX_BODY) -> Request:
    try:return await _read_request(reader,client,max_body)
    except (asyncio.IncompleteReadError,ValueError,asyncio.LimitOverrunError):
        raise BadRequest(400,'Оборванный или некорректный HTTP-запрос') from None


async def _read_request(reader: asyncio.StreamReader, client, max_body: int = MAX_BODY) -> Request:
    line = await _read_line(reader)
    while line in (b"\r\n", b"\n"):  # пустые строки между запросами допустимы
        line = await _read_line(reader)
    try:
        method, target, version = line.decode("latin-1").strip().split(" ", 2)
    except ValueError:
        raise BadRequest(400, "непонятная строка запроса") from None
    if not version.startswith("HTTP/1."):
        raise BadRequest(505 if version.startswith("HTTP/") else 400, "нужен HTTP/1.x")
    headers = Headers()
    while True:
        hline = await _read_line(reader)
        if hline in (b"\r\n", b"\n"):
            break
        if len(headers.items) >= MAX_HEADERS:
            raise BadRequest(431, "слишком много заголовков")
        name, sep, value = hline.decode("latin-1").partition(":")
        if not sep or not name or name != name.strip():
            raise BadRequest(400, "непонятный заголовок")
        headers.add(name, value.strip())
    lengths=headers.get_all('content-length')
    if len(lengths)>1 and len(set(lengths))>1:
        raise BadRequest(400,'Противоречивые длины тела')
    if lengths and headers.get('transfer-encoding'):
        raise BadRequest(400,'Нельзя одновременно задавать Content-Length и Transfer-Encoding')
    body = b""
    te = (headers.get("transfer-encoding") or "").lower()
    if te:
        if te != "chunked":
            raise BadRequest(501, "неизвестное кодирование тела")
        parts = []
        total = 0
        while True:
            size_line = await _read_line(reader)
            try:
                size = int(size_line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                raise BadRequest(400, "сломанный кусок тела") from None
            if size<0:raise BadRequest(400,'Отрицательный размер куска')
            if size == 0:
                trailers=0
                while (await _read_line(reader)) not in (b"\r\n", b"\n"):
                    trailers+=1
                    if trailers>MAX_HEADERS:raise BadRequest(431,'Слишком много завершающих заголовков')
                break
            total += size
            if total > max_body:
                raise BadRequest(413, "слишком большое тело")
            parts.append(await reader.readexactly(size))
            if await reader.readexactly(2)!=b"\r\n":raise BadRequest(400,"Нет конца строки после куска")
        body = b"".join(parts)
    elif headers.get("content-length"):
        try:
            length = int(headers.get("content-length") or "0")
        except ValueError:
            raise BadRequest(400, "непонятная длина тела") from None
        if length < 0:
            raise BadRequest(400, "непонятная длина тела")
        if length > max_body:
            raise BadRequest(413, "слишком большое тело")
        body = await reader.readexactly(length) if length else b""
    # сырые не-ASCII байты в адресе (не по стандарту, но бывает) — как UTF-8
    target = target.encode("latin-1").decode("utf-8", "replace")
    split = urlsplit(target)
    return Request(
        method=method.upper(),
        target=target,
        path=unquote(split.path or "/"),
        query=parse_qs(split.query, keep_blank_values=True),
        version=version,
        headers=headers,
        body=body,
        client=client,
    )


class HttpServer:
    def __init__(self, handler: Handler, *, max_body: int = MAX_BODY, on_error: Callable[[BaseException, Request | None], Response] | None = None):
        self.handler = handler
        self.max_body = max_body
        self.on_error = on_error
        self._server: asyncio.base_events.Server | None = None
        self._conns: set[asyncio.Task] = set()
        self._writers = set()
        self._stopping = False
        self.port: int | None = None
        self.host: str | None = None

    async def start(self, host: str, port: int) -> None:
        self._stopping=False
        self._server = await asyncio.start_server(self._accept, host, port, limit=MAX_LINE * 4, reuse_address=True)
        sock = self._server.sockets[0]
        self.host, self.port = sock.getsockname()[:2]

    async def stop(self) -> None:
        self._stopping=True
        if self._server:
            # Дать уже принятому TCP создать transport до закрытия asyncio.Server.
            # Иначе поздний _attach/_detach попадает в уже закрытый Server Python 3.13.
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            self._server.close()
            # Уже принятый TCP может ещё ждать connection_made; _accept закроет его при остановке.
            await asyncio.sleep(0)
            tasks=[task for task in self._conns if task is not asyncio.current_task()]
            writers=list(self._writers)
            for writer in writers:writer.close()
            for task in tasks:task.cancel()
            try:
                if tasks:await asyncio.wait_for(asyncio.gather(*tasks,return_exceptions=True),3)
                if writers:await asyncio.wait_for(asyncio.gather(*(writer.wait_closed() for writer in writers),return_exceptions=True),3)
                await asyncio.wait_for(self._server.wait_closed(), 3)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
            self._server = None
            await asyncio.sleep(0)

    def _accept(self, reader, writer):
        # Регистрируем до первого await: отмена ещё не начавшейся _handle иначе теряла сокет.
        if self._stopping:
            writer.close()
            return
        self._writers.add(writer)
        task=asyncio.create_task(self._handle(reader,writer))
        self._conns.add(task)
        def finished(done):
            writer.close()
            self._writers.discard(writer)
            self._conns.discard(done)
            if not done.cancelled():done.exception()
        task.add_done_callback(finished)

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        if task:
            self._conns.add(task)
        client = writer.get_extra_info("peername")
        try:
            first = True
            while True:
                try:
                    req = await asyncio.wait_for(read_request(reader, client, self.max_body), HEADER_TIMEOUT if first else KEEPALIVE_TIMEOUT)
                except (asyncio.TimeoutError, ConnectionError, asyncio.IncompleteReadError):
                    return
                except BadRequest as e:
                    resp = JSONResponse({"error": {"code": "bad_request", "title": "Запрос не понят", "hint": str(e)}}, e.status)
                    resp.headers.set("Connection", "close")
                    await resp.send(writer)
                    return
                first = False
                keep = (req.headers.get("connection") or "").lower() != "close" and req.version == "HTTP/1.1"
                try:
                    resp = await self.handler(req)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    resp = self.on_error(exc, req) if self.on_error else JSONResponse({"error": {"code": "internal", "title": "Внутренняя ошибка"}}, 500)
                if not keep:
                    resp.headers.set("Connection", "close")
                try:
                    await resp.send(writer, head_only=req.method == "HEAD")
                except (ConnectionError, asyncio.CancelledError):
                    return
                if not keep:
                    return
        finally:
            if task:
                self._conns.discard(task)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
