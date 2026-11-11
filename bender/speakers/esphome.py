"""ESPHome Native API без шифрования: ровно столько, сколько нужно голосу колонки.

Кадр открытого текста: байт 0x00, varint длины, varint типа, protobuf-полезная нагрузка.
Номера сообщений и полей сверены с api.proto ESPHome и с сервером Pulsar.
"""
from __future__ import annotations

import asyncio
import struct
import time

from ..core.errors import HumanError

HELLO_REQ, HELLO_RESP = 1, 2
DISCONNECT_REQ, DISCONNECT_RESP = 5, 6
PING_REQ, PING_RESP = 7, 8
DEVICE_INFO_REQ, DEVICE_INFO_RESP = 9, 10
GET_TIME_REQ, GET_TIME_RESP = 36, 37
SUBSCRIBE_VA_REQ = 89
VA_REQUEST, VA_RESPONSE, VA_EVENT = 90, 91, 92
VA_AUDIO = 106
VA_ANNOUNCE_REQ, VA_ANNOUNCE_FINISHED = 119, 120
LIST_ENTITIES_REQ, LIST_LIGHT, LIGHT_COMMAND = 11, 15, 32

# События голосового конвейера (VoiceAssistantEventType).
ERROR, RUN_START, RUN_END, STT_START, STT_END = 0, 1, 2, 3, 4
INTENT_START, INTENT_END, TTS_START, TTS_END = 5, 6, 7, 8
STT_VAD_START, STT_VAD_END, INTENT_PROGRESS = 11, 12, 100

API_MAJOR, API_MINOR = 1, 10
MAX_FRAME = 1 << 20  # больше кадра колонка не шлёт; защищает память Бендера


def failed(title, hint='Проверь адрес колонки и что на ней запущен Pulsar.'):
    return HumanError('speaker', title, hint)


def varint(value: int) -> bytes:
    if value < 0:
        value += 1 << 64  # отрицательный int32/int64 в protobuf — десять байт
    out = bytearray()
    while value >= 0x80:
        out.append(value & 0x7F | 0x80)
        value >>= 7
    out.append(value)
    return bytes(out)


def read_varint(data: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while pos < len(data) and shift < 70:
        byte = data[pos]
        pos += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value & ((1 << 64) - 1), pos
        shift += 7
    raise failed('Колонка прислала испорченное сообщение')


def encode(*fields) -> bytes:
    """Поля (номер, значение): bool/int — varint, str/bytes — длина, float — fixed32, dict/list пар — вложенное."""
    out = bytearray()
    for number, value in fields:
        if value is None:
            continue
        if isinstance(value, bool) or isinstance(value, int):
            out += varint(number << 3) + varint(int(value))
        elif isinstance(value, float):
            out += varint(number << 3 | 5) + struct.pack('<f', value)
        else:
            raw = value.encode('utf-8') if isinstance(value, str) else encode(*value) if isinstance(value, (list, tuple)) else bytes(value)
            out += varint(number << 3 | 2) + varint(len(raw)) + raw
    return bytes(out)


def fixed32(number: int, value: int) -> bytes:
    return varint(number << 3 | 5) + struct.pack('<I', value & 0xFFFFFFFF)


def light_command(key: int, color=None) -> bytes:
    """Команда свету: color=(r, g, b) от 0 до 1 — включить этим цветом, None — выключить."""
    if color is None:
        return fixed32(1, key) + encode((2, True), (3, False))
    red, green, blue = (float(c) for c in color)
    return fixed32(1, key) + encode((2, True), (3, True), (4, True), (5, 1.0), (8, True), (9, red), (10, green), (11, blue))


def decode(data: bytes) -> dict[int, list]:
    """Номер поля → значения: int для varint/fixed, bytes для длины."""
    fields: dict[int, list] = {}
    pos = 0
    while pos < len(data):
        key, pos = read_varint(data, pos)
        number, kind = key >> 3, key & 7
        if kind == 0:
            value, pos = read_varint(data, pos)
        elif kind == 1:
            if pos + 8 > len(data): raise failed('Колонка прислала испорченное сообщение')
            value, pos = int.from_bytes(data[pos:pos+8], 'little'), pos + 8
        elif kind == 5:
            if pos + 4 > len(data): raise failed('Колонка прислала испорченное сообщение')
            value, pos = int.from_bytes(data[pos:pos+4], 'little'), pos + 4
        elif kind == 2:
            size, pos = read_varint(data, pos)
            if pos + size > len(data): raise failed('Колонка прислала испорченное сообщение')
            value, pos = data[pos:pos+size], pos + size
        else:
            raise failed('Колонка прислала сообщение неизвестного формата')
        fields.setdefault(number, []).append(value)
    return fields


def text(fields: dict, number: int) -> str:
    value = (fields.get(number) or [b''])[-1]
    return value.decode('utf-8', 'replace') if isinstance(value, bytes) else ''


def number(fields: dict, key: int) -> int:
    value = (fields.get(key) or [0])[-1]
    return value if isinstance(value, int) else 0


def frame(kind: int, payload: bytes = b'') -> bytes:
    return b'\x00' + varint(len(payload)) + varint(kind) + payload


async def _varint(reader) -> int:
    value = shift = 0
    while shift < 35:
        byte = (await reader.readexactly(1))[0]
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value
        shift += 7
    raise failed('Колонка прислала испорченный кадр')


async def read_frame(reader) -> tuple[int, bytes]:
    first = (await reader.readexactly(1))[0]
    if first == 1:
        raise failed('Колонка требует шифрования ESPHome', 'Убери ключ API на колонке (ctl.sh api-key) или дождись шифрования в Бендере.')
    if first != 0:
        raise failed('Колонка отвечает не по ESPHome Native API', 'Проверь порт: у Pulsar это 6053.')
    size = await _varint(reader)
    kind = await _varint(reader)
    if size > MAX_FRAME:
        raise failed('Колонка прислала слишком большой кадр')
    return kind, await reader.readexactly(size)


def posix_tz() -> str:
    """Часовой пояс компьютера с Бендером в виде POSIX TZ: колонке нужен для часов."""
    offset = -(time.altzone if time.daylight and time.localtime().tm_isdst > 0 else time.timezone)
    sign = '-' if offset >= 0 else '+'  # в POSIX знак обратный
    hours, rest = divmod(abs(offset), 3600)
    label = ('+' if offset >= 0 else '-') + f'{hours:02d}' + (f'{rest // 60:02d}' if rest else '')
    return f'<{label}>{sign}{hours}' + (f':{rest // 60:02d}' if rest else '')


class Link:
    """Одно соединение с колонкой. Чтение — в своей задаче, обработчик получает (тип, поля)."""

    def __init__(self, host: str, port: int, client: str = 'Bender'):
        self.host, self.port, self.client = host, port, client
        self.reader = self.writer = None
        self.info: dict = {}
        self.lock = asyncio.Lock()

    async def open(self, timeout: float = 10.0):
        try:
            self.reader, self.writer = await asyncio.wait_for(asyncio.open_connection(self.host, self.port), timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            raise failed('Колонка не отвечает по сети', f'{self.host}:{self.port} — {exc.__class__.__name__}. Проверь, что колонка включена и Pulsar запущен.') from None
        await self.send(HELLO_REQ, encode((1, self.client), (2, API_MAJOR), (3, API_MINOR)))
        hello = await self._expect(HELLO_RESP, timeout)
        await self.send(DEVICE_INFO_REQ)
        info = await self._expect(DEVICE_INFO_RESP, timeout)
        self.info = {'name': text(info, 2), 'mac': text(info, 3), 'model': text(info, 6), 'project': text(info, 8),
                     'version': text(info, 9), 'friendly_name': text(info, 13), 'server': text(hello, 3)}
        return self.info

    async def _expect(self, kind: int, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise failed('Колонка не ответила на приветствие')
            try:
                got, payload = await asyncio.wait_for(read_frame(self.reader), left)
            except asyncio.TimeoutError:
                raise failed('Колонка не ответила на приветствие') from None
            except (asyncio.IncompleteReadError, OSError):
                raise failed('Колонка закрыла соединение', 'Возможно, к ней уже подключено много клиентов или она перезапускается.') from None
            if got == kind:
                return decode(payload)
            if got == PING_REQ:
                await self.send(PING_RESP)

    async def send(self, kind: int, payload: bytes = b''):
        if self.writer is None:
            raise failed('Нет соединения с колонкой')
        async with self.lock:
            self.writer.write(frame(kind, payload))
            await self.writer.drain()

    idle = 20.0  # тишина в линии до своей проверки связи; столько же ждём ответа

    async def frames(self):
        """Кадры до закрытия соединения; на служебные отвечает сам.

        Колонка, перезагруженная по питанию, не закрывает соединение: без проверки связи
        Бендер ждал бы её вечно и не переподключился. Молчит после проверки — соединение мёртвое.
        """
        pending = None
        try:
            while True:
                pending = asyncio.ensure_future(read_frame(self.reader))  # не отменяем чтение посреди кадра
                done, _ = await asyncio.wait({pending}, timeout=self.idle)
                if not done:
                    try:
                        await asyncio.wait_for(self.send(PING_REQ), self.idle)
                    except (OSError, ConnectionError, asyncio.TimeoutError, HumanError):
                        return
                    done, _ = await asyncio.wait({pending}, timeout=self.idle)
                    if not done:
                        return
                try:
                    kind, payload = pending.result()
                except (asyncio.IncompleteReadError, OSError, ConnectionError):
                    return
                if kind == PING_REQ:
                    await self.send(PING_RESP)
                    continue
                if kind == PING_RESP:
                    continue
                if kind == GET_TIME_REQ:
                    await self.send(GET_TIME_RESP, struct.pack('<B', 1 << 3 | 5) + struct.pack('<I', int(time.time()) & 0xFFFFFFFF) + encode((2, posix_tz())))
                elif kind == DISCONNECT_REQ:
                    await self.send(DISCONNECT_RESP)
                    return
                else:
                    yield kind, decode(payload)
        finally:
            if pending is not None and not pending.done():
                pending.cancel()

    async def event(self, kind: int, **data):
        pairs = [(2, [(1, key), (2, str(value))]) for key, value in data.items() if value is not None]
        await self.send(VA_EVENT, encode((1, kind), *pairs))

    async def close(self):
        writer, self.writer = self.writer, None
        if writer is None:
            return
        try:
            writer.write(frame(DISCONNECT_REQ))
            await asyncio.wait_for(writer.drain(), 2)
        except (OSError, asyncio.TimeoutError, ConnectionError):
            pass
        writer.close()
        try:
            await asyncio.wait_for(writer.wait_closed(), 2)
        except (OSError, asyncio.TimeoutError, ConnectionError):
            pass
