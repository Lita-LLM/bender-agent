"""HTTP/1.1-клиент на asyncio — без сторонних библиотек.

Умеет то, что нужно для работы с моделями, вебом, Telegram и MCP:
- HTTP и HTTPS (системные сертификаты; SSL_CERT_FILE уважается);
- прокси из окружения (HTTPS_PROXY / HTTP_PROXY / NO_PROXY): HTTPS — туннелем CONNECT;
- тело ответа целиком или потоком (chunked, Content-Length, до закрытия), gzip/deflate;
- переиспользование соединений (keep-alive) — меньше рукопожатий TLS, меньше ЦП;
- таймауты: на соединение и на тишину между кусками ответа (модели думают долго, но не молчат вечно).

Ошибки сети — NetError с видом (connect, dns, tls, timeout, proxy, protocol, closed) и
понятным текстом. Что с ними делать (повторить, переключиться, сказать человеку), решает
вызывающий — например, Коммутатор.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import json as jsonlib
import os
import socket
import ssl
import time
import zlib
from dataclasses import dataclass
from typing import AsyncIterator
from urllib.parse import quote, unquote, urlsplit

from .http_server import Headers

from .. import PROJECT_URL

USER_AGENT = "Bender/0.1" + (f" (+{PROJECT_URL})" if PROJECT_URL else "")
MAX_HEADER_LINE = 64 * 1024
POOL_IDLE_SECONDS = 50.0
POOL_PER_HOST = 6
POOL_TOTAL = 32


CONNECT_RETRIES = (0.5, 1.5)  # паузы перед повторными попытками соединиться после сброса


class NetError(Exception):
    """Сетевая ошибка. kind: dns, connect, tls, timeout, proxy, protocol, closed."""

    def __init__(self, kind: str, message: str, *, url: str = "", transient: bool = False):
        super().__init__(message)
        self.kind = kind
        self.url = url
        self.transient = transient  # разовый сбой до отправки запроса: соединение можно открыть заново


@dataclass
class Timeouts:
    connect: float = 20.0
    read: float = 120.0  # тишина между кусками ответа
    total: float | None = None


def _no_proxy_match(host: str, no_proxy: str) -> bool:
    host = host.strip("[]").lower()
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        ip = None
    for raw in no_proxy.split(","):
        rule = raw.strip().lower()
        if not rule:
            continue
        if rule == "*":
            return True
        if ip is not None and "/" in rule:
            try:
                if ip in ipaddress.ip_network(rule, strict=False):
                    return True
            except ValueError:
                pass
            continue
        rule = rule.lstrip("*")
        if rule.startswith("."):
            if host.endswith(rule) or host == rule[1:]:
                return True
        elif host == rule or host.endswith("." + rule):
            return True
    return False


def proxy_for(scheme: str, host: str) -> str | None:
    """Адрес прокси из окружения для этого хоста или None."""
    no_proxy = os.environ.get("NO_PROXY") or os.environ.get("no_proxy") or ""
    if no_proxy and _no_proxy_match(host, no_proxy):
        return None
    if scheme == "https":
        return os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")
    return os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or os.environ.get("ALL_PROXY") or os.environ.get("all_proxy")


_ssl_default: ssl.SSLContext | None = None


def ssl_context(ca_file: str | None = None, verify: bool = True) -> ssl.SSLContext:
    global _ssl_default
    if verify and not ca_file:
        if _ssl_default is None:
            _ssl_default = ssl.create_default_context()
        return _ssl_default
    ctx = ssl.create_default_context(cafile=ca_file) if verify else ssl._create_unverified_context()  # noqa: S323 — только по явной просьбе
    return ctx


class _Conn:
    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, key: tuple):
        self.reader = reader
        self.writer = writer
        self.key = key
        self.idle_since = time.monotonic()
        self.reused = False

    def alive(self) -> bool:
        if self.reader.at_eof() or self.writer.is_closing():
            return False
        return time.monotonic() - self.idle_since < POOL_IDLE_SECONDS

    def close(self) -> None:
        try:
            self.writer.close()
        except Exception:  # noqa: BLE001
            pass


class Response:
    def __init__(self, client: "HttpClient", conn: _Conn, method: str, url: str, status: int, reason: str, version: str, headers: Headers, timeouts: Timeouts, *, deadline=None):
        self._client = client
        self._conn: _Conn | None = conn
        self.method = method
        self.url = url
        self.status = status
        self.reason = reason
        self.version = version
        self.headers = headers
        self._timeouts = timeouts
        self._deadline = deadline
        self._done = False
        self._consumed = False
        enc = (headers.get("content-encoding") or "").lower()
        self._decoder = zlib.decompressobj(16 + zlib.MAX_WBITS) if enc == "gzip" else zlib.decompressobj() if enc == "deflate" else None

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def _keep_alive(self) -> bool:
        conn_hdr = (self.headers.get("connection") or "").lower()
        if self.version == "HTTP/1.0":
            return conn_hdr == "keep-alive"
        return conn_hdr != "close"

    async def _raw_chunks(self) -> AsyncIterator[bytes]:
        conn = self._conn
        assert conn is not None
        reader = conn.reader
        rt = self._timeouts.read

        async def rd(coro):
            try:
                remaining = self._deadline-time.monotonic() if self._deadline is not None else rt
                if remaining <= 0:
                    coro.close()
                    raise asyncio.TimeoutError()
                return await asyncio.wait_for(coro, min(rt,remaining))
            except asyncio.TimeoutError:
                raise NetError("timeout", f"Сервер замолчал больше чем на {int(rt)} с", url=self.url) from None
            except asyncio.IncompleteReadError:
                raise NetError("closed", "Сервер оборвал ответ на середине", url=self.url) from None
            except (ConnectionError, OSError) as e:
                raise NetError("closed", f"Соединение оборвалось: {e}", url=self.url) from None

        if self.method == "HEAD" or self.status in (204, 304) or 100 <= self.status < 200:
            return
        te = (self.headers.get("transfer-encoding") or "").lower()
        if "chunked" in te:
            while True:
                line = await rd(reader.readuntil(b"\n"))
                try:
                    size = int(line.split(b";", 1)[0].strip(), 16)
                except ValueError:
                    raise NetError("protocol", "Сервер прислал сломанный кусок ответа", url=self.url) from None
                if size == 0:
                    while (await rd(reader.readuntil(b"\n"))) not in (b"\r\n", b"\n"):
                        pass
                    return
                left = size
                while left > 0:
                    piece = await rd(reader.read(min(left, 65536)))
                    if not piece:
                        raise NetError("closed", "Сервер оборвал ответ на середине", url=self.url)
                    left -= len(piece)
                    yield piece
                await rd(reader.readexactly(2))
        elif self.headers.get("content-length") is not None:
            try:
                left = int(self.headers.get("content-length") or "0")
            except ValueError:
                raise NetError("protocol", "Сервер прислал непонятную длину ответа", url=self.url) from None
            while left > 0:
                piece = await rd(reader.read(min(left, 65536)))
                if not piece:
                    raise NetError("closed", "Сервер оборвал ответ на середине", url=self.url)
                left -= len(piece)
                yield piece
        else:
            # читаем до закрытия — соединение после этого не переиспользуется
            self.headers.set("Connection", "close")
            while True:
                piece = await rd(reader.read(65536))
                if not piece:
                    return
                yield piece

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        """Тело ответа кусками (уже распакованное)."""
        if self._consumed:
            raise RuntimeError("тело ответа уже прочитано")
        self._consumed = True
        ok = False
        try:
            async for raw in self._raw_chunks():
                data = self._decoder.decompress(raw) if self._decoder else raw
                if data:
                    yield data
            if self._decoder:
                tail = self._decoder.flush()
                if tail:
                    yield tail
            ok = True
        finally:
            self._finish(reusable=ok)

    async def iter_lines(self) -> AsyncIterator[str]:
        buf = b""
        async for chunk in self.iter_bytes():
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                if len(line)>1024*1024:raise NetError("size","Слишком длинная строка ответа",url=self.url)
                yield line.rstrip(b"\r").decode("utf-8", "replace")
            if len(buf)>1024*1024:raise NetError("size","Слишком длинная строка ответа",url=self.url)
        if buf:
            yield buf.rstrip(b"\r").decode("utf-8", "replace")

    async def read(self, limit: int = 256 * 1024 * 1024) -> bytes:
        parts = []
        total = 0
        async for chunk in self.iter_bytes():
            total += len(chunk)
            if total > limit:
                await self.aclose()
                raise NetError("protocol", f"Ответ больше {limit // (1024 * 1024)} МБ", url=self.url)
            parts.append(chunk)
        return b"".join(parts)

    async def text(self) -> str:
        data = await self.read()
        charset = "utf-8"
        ctype = self.headers.get("content-type") or ""
        if "charset=" in ctype:
            charset = ctype.split("charset=", 1)[1].split(";")[0].strip().strip('"') or "utf-8"
        try:
            return data.decode(charset, "replace")
        except LookupError:
            return data.decode("utf-8", "replace")

    async def json(self):
        return jsonlib.loads(await self.text())

    def _finish(self, reusable: bool) -> None:
        if self._done:
            return
        self._done = True
        conn, self._conn = self._conn, None
        if conn is None:
            return
        if reusable and self._keep_alive():
            self._client._release(conn)
        else:
            conn.close()

    async def aclose(self) -> None:
        """Бросить ответ, не дочитывая: соединение закрывается (переиспользовать его нельзя)."""
        self._consumed = True
        self._finish(reusable=False)

    async def __aenter__(self) -> "Response":
        return self

    async def __aexit__(self, *exc) -> None:
        if not self._done:
            await self.aclose()


class HttpClient:
    def __init__(self, *, timeouts: Timeouts | None = None, use_env_proxy: bool = True, user_agent: str = USER_AGENT):
        self.timeouts = timeouts or Timeouts()
        self.use_env_proxy = use_env_proxy
        self.user_agent = user_agent
        self._pool: dict[tuple, list[_Conn]] = {}

    async def close(self) -> None:
        for conns in self._pool.values():
            for c in conns:
                c.close()
        self._pool.clear()

    # пул соединений

    def _release(self, conn: _Conn) -> None:
        # Уборка по событию, без фонового таймера: чужие хосты не копят сокеты.
        for key, entries in list(self._pool.items()):
            alive = []
            for old in entries:
                if old.alive():
                    alive.append(old)
                else:
                    old.close()
            if alive:
                self._pool[key] = alive
            else:
                self._pool.pop(key, None)
        if sum(map(len, self._pool.values())) >= POOL_TOTAL:
            oldest = min((old for entries in self._pool.values() for old in entries), key=lambda item: item.idle_since)
            entries = self._pool[oldest.key]
            entries.remove(oldest)
            oldest.close()
            if not entries:
                self._pool.pop(oldest.key, None)
        conn.idle_since = time.monotonic()
        conns = self._pool.setdefault(conn.key, [])
        if len(conns) >= POOL_PER_HOST:
            conn.close()
            return
        conns.append(conn)

    def _take(self, key: tuple) -> _Conn | None:
        conns = self._pool.get(key) or []
        while conns:
            c = conns.pop()
            if not conns:
                self._pool.pop(key, None)
            if c.alive():
                c.reused = True
                return c
            c.close()
        return None

    async def _open(self, scheme: str, host: str, port: int, proxy: str | None, timeouts: Timeouts, ssl_ctx: ssl.SSLContext | None, url: str) -> _Conn:
        # Сброс при соединении (провайдер, DPI, мигнувшая сеть) случается разово, а запрос ещё не ушёл: повтор безопасен.
        for pause in CONNECT_RETRIES:
            try:
                return await self._open_once(scheme, host, port, proxy, timeouts, ssl_ctx, url)
            except NetError as e:
                if not e.transient:
                    raise
            await asyncio.sleep(pause)
        return await self._open_once(scheme, host, port, proxy, timeouts, ssl_ctx, url)

    async def _open_once(self, scheme: str, host: str, port: int, proxy: str | None, timeouts: Timeouts, ssl_ctx: ssl.SSLContext | None, url: str) -> _Conn:
        key = (scheme, host, port, proxy, id(ssl_ctx) if ssl_ctx else None)
        try:
            if proxy and scheme == "https":
                sock = await asyncio.wait_for(asyncio.to_thread(_connect_tunnel, proxy, host, port, timeouts.connect), timeouts.connect + 1)
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(sock=sock, ssl=ssl_ctx, server_hostname=host, limit=MAX_HEADER_LINE), timeouts.connect
                )
            elif proxy:
                p = urlsplit(proxy)
                reader, writer = await asyncio.wait_for(asyncio.open_connection(p.hostname, p.port or 80, limit=MAX_HEADER_LINE), timeouts.connect)
            else:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port, ssl=ssl_ctx if scheme == "https" else None, server_hostname=host if scheme == "https" else None, limit=MAX_HEADER_LINE),
                    timeouts.connect,
                )
        except asyncio.TimeoutError:
            raise NetError("timeout", f"Не удалось соединиться с {host} за {int(timeouts.connect)} с", url=url) from None
        except ssl.SSLCertVerificationError as e:
            raise NetError("tls", f"Сертификат {host} не прошёл проверку: {e.verify_message}", url=url) from None
        except (ConnectionResetError, ConnectionAbortedError, ssl.SSLEOFError) as e:
            raise NetError("connect", f"Не удалось соединиться с {host}:{port}: {getattr(e, 'strerror', None) or e}", url=url, transient=True) from None
        except ssl.SSLError as e:
            raise NetError("tls", f"Ошибка TLS с {host}: {e}", url=url) from None
        except socket.gaierror:
            raise NetError("dns", f"Адрес {host} не найден (DNS). Проверь адрес и интернет.", url=url) from None
        except NetError:
            raise
        except ConnectionRefusedError:
            raise NetError("connect", f"{host}:{port} не принимает соединения — сервер не запущен?", url=url) from None
        except OSError as e:
            raise NetError("connect", f"Не удалось соединиться с {host}:{port}: {e.strerror or e}", url=url) from None
        return _Conn(reader, writer, key)

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        body: bytes | str | None = None,
        json=None,
        timeouts: Timeouts | None = None,
        ssl_ctx: ssl.SSLContext | None = None,
        proxy: str | None | bool = True,
        retry_stale: bool = True,
    ) -> Response:
        """Отправить запрос и дождаться заголовков ответа. Тело читается методами Response.

        proxy=True — из окружения, None/False — без прокси, строка — этот прокси."""
        timeouts = timeouts or self.timeouts
        split = urlsplit(url)
        scheme = (split.scheme or "http").lower()
        if scheme not in ("http", "https"):
            raise NetError("protocol", f"Неподдерживаемая схема адреса: {scheme}", url=url)
        host = split.hostname or ""
        if not host:
            raise NetError("protocol", "В адресе нет хоста", url=url)
        port = split.port or (443 if scheme == "https" else 80)
        if proxy is True:
            proxy = proxy_for(scheme, host) if self.use_env_proxy else None
        elif proxy is False:
            proxy = None
        if scheme == "https":
            ssl_ctx = ssl_ctx or ssl_context()
        # не-ASCII в пути и запросе — в %-кодировку (уже закодированное не трогаем)
        path = quote(split.path or "/", safe="/%:@!$&'()*+,;=-._~")
        if split.query:
            path += "?" + quote(split.query, safe="/%:@!$&'()*+,;=-._~?")
        target = f"{scheme}://{split.netloc.rsplit('@', 1)[-1]}{path}" if (proxy and scheme == "http") else path

        if json is not None:
            body = jsonlib.dumps(json, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        hdrs = Headers()
        host_hdr = host if ":" not in host else f"[{host}]"
        if split.port:
            host_hdr += f":{split.port}"
        hdrs.set("Host", host_hdr)
        hdrs.set("User-Agent", self.user_agent)
        hdrs.set("Accept-Encoding", "gzip, deflate")
        hdrs.set("Connection", "keep-alive")
        if json is not None:
            hdrs.set("Content-Type", "application/json")
        if split.username:
            token = base64.b64encode(f"{unquote(split.username)}:{unquote(split.password or '')}".encode()).decode()
            hdrs.set("Authorization", f"Basic {token}")
        if proxy and scheme == "http":
            p = urlsplit(proxy)
            if p.username:
                token = base64.b64encode(f"{unquote(p.username)}:{unquote(p.password or '')}".encode()).decode()
                hdrs.set("Proxy-Authorization", f"Basic {token}")
        for k, v in (headers or {}).items():
            if v is None:
                continue
            hdrs.set(k, str(v))
        if body is not None or method.upper() in ("POST", "PUT", "PATCH"):
            hdrs.set("Content-Length", str(len(body or b"")))
        lines = [f"{method.upper()} {target} HTTP/1.1"] + [f"{k}: {v}" for k, v in hdrs.items]
        for ln in lines:
            if "\r" in ln or "\n" in ln:
                raise NetError("protocol", "Перевод строки в заголовке запроса", url=url)
        head = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8")

        key = (scheme, host, port, proxy, id(ssl_ctx) if ssl_ctx else None)
        deadline = time.monotonic() + timeouts.total if timeouts.total else None
        for attempt in (1, 2):
            conn = self._take(key)
            fresh = conn is None
            if conn is None:
                conn = await self._open(scheme, host, port, proxy, timeouts, ssl_ctx, url)
            try:
                conn.writer.write(head + (body or b""))
                await conn.writer.drain()
                read_timeout = timeouts.read
                if deadline:
                    read_timeout = max(0.1, min(read_timeout, deadline - time.monotonic()))
                status_line = await asyncio.wait_for(conn.reader.readuntil(b"\n"), read_timeout)
                while status_line.startswith(b"HTTP/1.1 100") or status_line.startswith(b"HTTP/1.0 100"):
                    while (await asyncio.wait_for(conn.reader.readuntil(b"\n"), read_timeout)) not in (b"\r\n", b"\n"):
                        pass
                    status_line = await asyncio.wait_for(conn.reader.readuntil(b"\n"), read_timeout)
                resp_headers = Headers()
                while True:
                    hl = await asyncio.wait_for(conn.reader.readuntil(b"\n"), read_timeout)
                    if hl in (b"\r\n", b"\n"):
                        break
                    name, _, value = hl.decode("latin-1").partition(":")
                    resp_headers.add(name.strip(), value.strip())
                    if len(resp_headers.items) > 200:
                        raise NetError("protocol", "Слишком много заголовков в ответе", url=url)
            except asyncio.TimeoutError:
                conn.close()
                raise NetError("timeout", f"{host} не ответил за {int(timeouts.read)} с", url=url) from None
            except (asyncio.IncompleteReadError, ConnectionError, OSError) as e:
                conn.close()
                if conn.reused and attempt == 1 and retry_stale:
                    continue  # сервер закрыл простаивавшее соединение — пробуем свежее
                if isinstance(e, asyncio.IncompleteReadError):
                    raise NetError("closed", f"{host} закрыл соединение, не ответив", url=url) from None
                raise NetError("closed", f"Соединение с {host} оборвалось: {e}", url=url) from None
            except asyncio.LimitOverrunError:
                conn.close()
                raise NetError("protocol", "Слишком длинный заголовок в ответе", url=url) from None
            except asyncio.CancelledError:
                conn.close()  # запрос отменили посреди ответа (стоп, выключение) — соединение в пул не годится
                raise
            parts = status_line.decode("latin-1").strip().split(" ", 2)
            if len(parts) < 2 or not parts[0].startswith("HTTP/"):
                conn.close()
                raise NetError("protocol", f"{host} ответил не по HTTP", url=url)
            try:
                status = int(parts[1])
            except ValueError:
                conn.close()
                raise NetError("protocol", f"{host} прислал непонятный код ответа", url=url) from None
            _ = fresh
            return Response(self, conn, method.upper(), url, status, parts[2] if len(parts) > 2 else "", parts[0], resp_headers, timeouts, deadline=deadline)
        raise NetError("closed", f"Соединение с {host} оборвалось", url=url)

    async def get(self, url: str, **kw) -> Response:
        return await self.request("GET", url, **kw)

    async def post(self, url: str, **kw) -> Response:
        return await self.request("POST", url, **kw)


def _connect_tunnel(proxy: str, host: str, port: int, timeout: float) -> socket.socket:
    """Туннель CONNECT через HTTP-прокси обычным сокетом (в потоке): так работает и на Python 3.10."""
    p = urlsplit(proxy if "://" in proxy else "http://" + proxy)
    if (p.scheme or "http") not in ("http",):
        raise NetError("proxy", f"Прокси {p.scheme}:// не поддерживается — нужен http://")
    try:
        sock = socket.create_connection((p.hostname, p.port or 8080), timeout=timeout)
    except OSError as e:
        raise NetError("proxy", f"Прокси {p.hostname}:{p.port} недоступен: {e}") from None
    target = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
    req = f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\nUser-Agent: {USER_AGENT}\r\n"
    if p.username:
        token = base64.b64encode(f"{unquote(p.username)}:{unquote(p.password or '')}".encode()).decode()
        req += f"Proxy-Authorization: Basic {token}\r\n"
    req += "\r\n"
    try:
        sock.sendall(req.encode("latin-1"))
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = sock.recv(4096)
            if not chunk:
                raise NetError("proxy", "Прокси закрыл соединение")
            buf += chunk
            if len(buf) > 65536:
                raise NetError("proxy", "Прокси прислал слишком длинный ответ")
        status_line = buf.split(b"\r\n", 1)[0].decode("latin-1")
        parts = status_line.split(" ", 2)
        if len(parts) < 2 or parts[1] != "200":
            raise NetError("proxy", f"Прокси не пустил к {host}: {status_line}")
    except NetError:
        sock.close()
        raise
    except OSError as e:
        sock.close()
        raise NetError("proxy", f"Ошибка прокси: {e}") from None
    sock.settimeout(None)
    sock.setblocking(False)
    return sock
