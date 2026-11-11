"""Посредник Мастерской: публичные адреса одобренных доменов, без повторного DNS."""
from __future__ import annotations

import asyncio
import ipaddress
import os
from pathlib import Path
import re
import socket
from urllib.parse import urlsplit

from ..core.errors import HumanError
from ..tools.registry import ToolContext


def denied(title):
    return HumanError("workshop_network", title, "Проверь домены в одобренной карточке и включён ли сетевой посредник.")


def public_ip(raw):
    try:
        address = ipaddress.ip_address(raw)
        if isinstance(address, ipaddress.IPv6Address):
            if address.ipv4_mapped:
                return public_ip(str(address.ipv4_mapped))
            if address.sixtofour or address.teredo:
                return False
        return address.is_global and not any((address.is_multicast, address.is_reserved, address.is_unspecified,
                                               address.is_private, address.is_loopback, address.is_link_local))
    except ValueError:
        return False


def hostname(raw):
    if not isinstance(raw, str) or not raw or len(raw) > 253 or raw != raw.strip() or raw.endswith('.') or '%' in raw:
        raise denied("Нужно точное имя домена без пути, порта и звёздочек")
    try:
        name = raw.encode('idna').decode('ascii').lower()
        try:
            address = ipaddress.ip_address(name)
        except ValueError:
            if '.' not in name or not all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', part) for part in name.split('.')):
                raise ValueError()
            if name.endswith(('.localhost', '.local', '.internal', '.home', '.lan', '.test', '.invalid', '.onion')):
                raise ValueError()
        else:
            if not public_ip(str(address)):
                raise ValueError()
        return name
    except (ValueError, UnicodeError):
        raise denied("Локальные, служебные и неоднозначные адреса запрещены") from None


def hosts(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 32:
        raise denied("Для net.read нужен список из 1–32 точных доменов")
    result = sorted({hostname(item) for item in value})
    if len(result) != len(value):
        raise denied("Убери повторяющиеся домены")
    return result


def status(agent_ctx):
    from ..guard import sandbox
    available = sandbox.for_app(agent_ctx).get('kind') == 'bwrap' and hasattr(socket, 'AF_UNIX')
    enabled = agent_ctx.settings.get('workshop', 'network_proxy', False)
    return {'enabled': enabled, 'available': available,
            'title': 'Сеть через посредник' if enabled and available else 'Сеть тулов-кода закрыта',
            'hint': 'Только одобренные домены, HTTP:80 / CONNECT:443. CONNECT передаёт зашифрованный поток: домен может получать данные.' if available else 'Нужны bubblewrap и Unix-сокеты; переменных HTTPS_PROXY без запрета прямой сети недостаточно.'}


class Proxy:
    def __init__(self, agent_ctx, path, allowed, *, module='', ctx=None, active=None):
        self.app, self.path, self.allowed = agent_ctx, Path(path), frozenset(hosts(allowed))
        self.module, self.ctx, self.active = module, ctx, active
        self.server = None
        self.tasks = set()
        self.writers = set()
        self.closed = False

    def require(self):
        if self.closed or self.app.safe or not self.app.settings.get('workshop', 'network_proxy', False) or not self.app.settings.get('workshop', 'enabled', True) or not self.app.settings.get('workshop', 'tools_enabled', True) or (self.active and not self.active()):
            raise denied("Сетевой доступ этого тула выключен или пакет изменился")

    async def start(self):
        self.require()
        try:
            self.server = await asyncio.start_unix_server(self.accept, path=str(self.path), limit=16384, backlog=8)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            self.close()
            raise HumanError('workshop_proxy', 'Не удалось открыть сокет посредника', 'Проверь права и длину пути папки данных.', detail=str(exc)) from None
        return self

    async def resolve(self, host):
        if host not in self.allowed:
            raise denied("Домен не входит в одобренную карточку тула")
        records = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM), 10)
        addresses = sorted({row[4][0] for row in records})
        if not addresses or not all(public_ip(address) for address in addresses):
            raise denied("DNS вернул локальный или служебный адрес; соединение запрещено")
        return addresses

    async def dial(self, address, port):
        # Числовой проверенный адрес: имя не отдаётся следующему резолверу.
        return await asyncio.wait_for(asyncio.open_connection(address, port), 10)

    def accept(self, reader, writer):
        if self.closed or len(self.tasks) >= 8:
            writer.close()
            return
        task = asyncio.create_task(self.handle(reader, writer))
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def pump(self, reader, writer):
        total = 0
        while True:
            data = await asyncio.wait_for(reader.read(65536), 60)
            if not data:
                return
            total += len(data)
            if total > 64 * 1024 * 1024:
                raise denied("Превышен предел 64 МБ на одно соединение")
            writer.write(data)
            await writer.drain()

    async def handle(self, reader, writer):
        self.writers.add(writer)
        upstream = None
        target, method, port = '', '', 0
        opened = False
        try:
            self.require()
            raw = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 10)
            if len(raw) > 16384:
                raise denied("Заголовки запроса слишком велики")
            lines = raw.decode('latin-1').split('\r\n')
            method, url, version = lines[0].split(' ')
            if version not in ('HTTP/1.0', 'HTTP/1.1') or len(url) > 4096 or any(ord(c) < 33 or ord(c) == 127 for c in url):
                raise denied("Некорректный запрос посреднику")
            parsed = urlsplit('//' + url if method == 'CONNECT' else url)
            if parsed.username or parsed.password or parsed.fragment:
                raise denied("Авторизация и фрагмент в адресе запрещены")
            target = hostname(parsed.hostname or '')
            port = parsed.port or (443 if method == 'CONNECT' else 80)
            if method == 'CONNECT':
                if port != 443 or parsed.path or parsed.query:
                    raise denied("CONNECT разрешён только к порту 443")
            elif method not in ('GET', 'HEAD') or parsed.scheme != 'http' or port != 80:
                raise denied("Посредник принимает HTTP GET/HEAD:80 и HTTPS CONNECT:443")
            fields = []
            for line in lines[1:-2]:
                key, value = line.split(':', 1)
                if not re.fullmatch(r'[!#$%&\x27*+.^_`|~0-9A-Za-z-]+', key) or any(ord(c) < 32 and c != '\t' or ord(c) == 127 for c in value):
                    raise denied("Некорректный заголовок запроса")
                if key.lower() == 'transfer-encoding' or key.lower() == 'content-length' and value.strip() != '0':
                    raise denied("Тело HTTP-запроса через посредник запрещено")
                if key.lower() not in ('host', 'connection', 'proxy-connection', 'proxy-authorization', 'content-length', 'upgrade', 'keep-alive', 'te', 'trailer'):
                    fields.append((key, value.strip()))
            addresses = await self.resolve(target)
            context = ToolContext(self.app, actor='module:'+self.module, tainted=True, tool='workshop.proxy',
                                  session_id=self.ctx.session_id if self.ctx else None, unattended=self.ctx.unattended if self.ctx else True)
            await context.check('net.read', ('https' if method == 'CONNECT' else 'http')+'://'+target,
                                'соединение тула через посредник', detail={'host':target, 'port':port, 'module':self.module})
            self.require()
            deadline = asyncio.get_running_loop().time() + 10
            failure = None
            for address in addresses[:16]:
                try:
                    remaining = max(.001, deadline - asyncio.get_running_loop().time())
                    remote_reader, upstream = await asyncio.wait_for(self.dial(address, port), remaining)
                    break
                except (OSError, asyncio.TimeoutError) as exc:
                    failure = exc
            if upstream is None:
                raise failure or OSError('Нет доступного публичного адреса')
            self.writers.add(upstream)
            self.require()
            self.app.journal.write('workshop.proxy', 'Соединение тула: '+target, actor='module:'+self.module,
                                   data={'host':target,'port':port,'method':method,'address':address,'verdict':'allow','session':context.session_id})
            if method == 'CONNECT':
                writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
                await writer.drain()
                opened = True
                tasks = [asyncio.create_task(self.pump(reader, upstream)), asyncio.create_task(self.pump(remote_reader, writer))]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            else:
                path = parsed.path or '/'
                if parsed.query:
                    path += '?' + parsed.query
                authority = '['+target+']' if ':' in target else target
                request = f'{method} {path} HTTP/1.1\r\nHost: {authority}\r\nConnection: close\r\n'
                request += ''.join(key+': '+value+'\r\n' for key,value in fields)+'\r\n'
                upstream.write(request.encode('latin-1'))
                await upstream.drain()
                opened = True
                await self.pump(remote_reader, writer)
        except asyncio.CancelledError:
            raise
        except (HumanError, OSError, ValueError, asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            error = exc if isinstance(exc, HumanError) else HumanError('workshop_proxy', 'Посредник не выполнил запрос', 'Проверь адрес, DNS и доступность сети.', detail=str(exc))
            self.app.journal.write('workshop.proxy', error.title, actor='module:'+self.module,
                                   data={'host':target,'port':port,'method':method,'verdict':'deny','reason':error.title})
            if not opened:
                body = (error.title+'. '+error.hint).encode()
                writer.write(b'HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Type: text/plain; charset=utf-8\r\nContent-Length: '+str(len(body)).encode()+b'\r\n\r\n'+body)
                try:
                    await writer.drain()
                except (OSError, asyncio.CancelledError):
                    pass
        finally:
            for stream in (upstream, writer):
                if stream:
                    self.writers.discard(stream)
                    stream.close()

    def close(self):
        self.closed = True
        if self.server:
            self.server.close()
        for task in tuple(self.tasks):
            task.cancel()
        for writer in tuple(self.writers):
            writer.close()
        self.path.unlink(missing_ok=True)
