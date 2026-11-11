"""Раздача звука ответов колонкам: отдельный крошечный HTTP только с короткоживущими ссылками.

Пульт остаётся на 127.0.0.1. Колонка же забирает ответ по URL, поэтому здесь слушается
один адрес компьютера с Бендером — тот, через который видна колонка. Кроме случайных ссылок на звук
последних ответов тут ничего нет: ни пульта, ни API, ни файлов.
"""
from __future__ import annotations

import asyncio
import secrets
import socket
import time

from ..core.errors import HumanError

TTL = 300.0
KEEP = 16
TYPES = {'wav': 'audio/wav', 'mp3': 'audio/mpeg', 'opus': 'audio/ogg', 'ogg': 'audio/ogg', 'flac': 'audio/flac'}


def local_address(peer: str) -> str:
    """Адрес компьютера, которым он смотрит на колонку (без отправки пакетов)."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.connect((peer, 9))
        return probe.getsockname()[0]


class Stream:
    """Звук, который ещё пишется: колонка читает его, пока ответ не закончится (одно соединение)."""

    def __init__(self):
        self.data = bytearray()
        self.closed = False
        self.grew = asyncio.Event()

    def write(self, chunk: bytes):
        if chunk and not self.closed:
            self.data += chunk
            self.grew.set()

    def close(self):
        self.closed = True
        self.grew.set()


class Media:
    def __init__(self):
        self.items: dict[str, tuple[float, str, bytes | Stream]] = {}
        self.served: set[str] = set()
        self.server = None
        self.address = None
        self.port = None

    async def start(self, address: str, port: int):
        if self.server and (self.address, self.port) == (address, port):
            return
        await self.stop()
        try:
            self.server = await asyncio.start_server(self.handle, address, port)
        except OSError as exc:
            raise HumanError('speaker_media', f'Не удалось открыть раздачу звука для колонок на {address}:{port}',
                             'Порт занят или адрес недоступен. Смени «Порт раздачи звука» в настройках колонок.', detail=str(exc)) from None
        self.address, self.port = address, self.server.sockets[0].getsockname()[1]

    async def stop(self):
        server, self.server = self.server, None
        if server:
            server.close()
            try:
                await asyncio.wait_for(server.wait_closed(), 2)
            except asyncio.TimeoutError:
                pass

    def stream(self, ext: str = 'wav') -> tuple[str, Stream]:
        """Ссылка заранее: колонка получает её в начале разговора, а звук дописывается по ходу ответа."""
        stream = Stream()
        return self.put(stream, ext), stream

    def put(self, audio: bytes | Stream, ext: str) -> str:
        if not self.server:
            raise HumanError('speaker_media', 'Раздача звука для колонок не запущена', 'Включи колонки в настройках.')
        now = time.monotonic()
        for key in [k for k, (born, _, body) in self.items.items() if now - born > TTL and not (isinstance(body, Stream) and not body.closed)]:
            del self.items[key]
        self.served &= set(self.items)
        while len(self.items) >= KEEP:
            del self.items[next(iter(self.items))]
        token = secrets.token_hex(16) + '.' + ext
        self.items[token] = (now, TYPES.get(ext, 'application/octet-stream'), audio)
        return f'http://{self.address}:{self.port}/speaker/{token}'

    def was_served(self, url: str) -> bool:
        return url.rsplit('/', 1)[-1] in self.served

    async def handle(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            while True:  # заголовки не нужны, но их надо дочитать
                header = await asyncio.wait_for(reader.readline(), 10)
                if header in (b'\r\n', b'\n', b''):
                    break
            parts = line.decode('latin-1').split()
            item = None
            if len(parts) >= 2 and parts[0] in ('GET', 'HEAD') and parts[1].startswith('/speaker/'):
                found = self.items.get(parts[1][9:])
                if found and (time.monotonic() - found[0] <= TTL or isinstance(found[2], Stream) and not found[2].closed):
                    item = found
            if item is None:
                writer.write(b'HTTP/1.0 404 Not Found\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            else:
                _, ctype, body = item
                self.served.add(parts[1][9:])
                if isinstance(body, Stream):
                    # без Content-Length: конец звука — закрытие соединения
                    writer.write(f'HTTP/1.0 200 OK\r\nContent-Type: {ctype}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n'.encode())
                    sent = 0
                    while parts[0] == 'GET':
                        body.grew.clear()
                        if sent < len(body.data):
                            writer.write(bytes(body.data[sent:]))
                            sent = len(body.data)
                            await writer.drain()
                        if body.closed and sent >= len(body.data):
                            break
                        await asyncio.wait_for(body.grew.wait(), TTL)
                else:
                    writer.write(f'HTTP/1.0 200 OK\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n'.encode())
                    if parts[0] == 'GET':
                        writer.write(body)
            await writer.drain()
        except (asyncio.TimeoutError, OSError, ConnectionError, UnicodeError):
            pass
        finally:
            writer.close()
