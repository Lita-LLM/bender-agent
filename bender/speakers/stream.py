"""Потоковая речь колонки: говорить по предложениям, пока модель дописывает ответ.

Один поток звука на ответ: колонка получила ссылку в начале разговора (RUN_START url) и
открывает её по знаку INTENT_PROGRESS tts_start_streaming=1. Сюда дописываются куски WAV
без заголовков, а заголовок с «бесконечной» длиной идёт первым. Озвучка — одна очередь:
пока звучит одно предложение, синтезируется следующее; что модель успела дописать
за это время, озвучивается одним куском.
"""
from __future__ import annotations

import asyncio
import io
import re
import struct
import time
import wave

from ..core.errors import HumanError
from .listen import spoken

# конец предложения: знак и пробел после него (а не «3.5») или новая строка
END = re.compile(r'[.!?…]+[»")\]]*\s+|\n+')
LONG = 300  # длинную фразу без точек режем по запятой, чтобы не ждать до конца абзаца


def wav_header(channels: int, width: int, rate: int) -> bytes:
    """Заголовок WAV с неизвестной длиной: проигрыватель читает до конца соединения."""
    unknown = 0xFFFFFFFF
    return (b'RIFF' + struct.pack('<I', unknown) + b'WAVEfmt ' + struct.pack('<IHHIIHH', 16, 1, channels, rate, rate * channels * width, channels * width, width * 8)
            + b'data' + struct.pack('<I', unknown))


def cut(text: str, final: bool = False) -> int:
    """Сколько символов текста уже можно говорить."""
    if final:
        return len(text)
    last = 0
    for match in END.finditer(text):
        last = match.end()
    if not last and len(text) > LONG:
        comma = text.rfind(', ', 0, len(text) - 20)
        last = comma + 2 if comma > 40 else 0
    return last


def pcm(audio: bytes):
    with wave.open(io.BytesIO(audio)) as reader:
        return (reader.getnchannels(), reader.getsampwidth(), reader.getframerate()), reader.readframes(reader.getnframes())


class Streamer:
    def __init__(self, ctx, sid, stream, start, allow):
        self.ctx, self.sid, self.stream, self.start, self.allow = ctx, sid, stream, start, allow
        self.raw = ''          # весь текст ответа, пришедший кусками
        self.used = 0          # сколько из него уже отдано на озвучку
        self.said = []         # что прозвучало (после очистки)
        self.format = None
        self.first = None      # когда в поток лёг первый звук
        self.error = None
        self.closing = False
        self.wake = asyncio.Event()
        self.queue = ctx.bus.subscribe(maxsize=10000)
        self.reader = asyncio.ensure_future(self.listen())
        self.worker = asyncio.ensure_future(self.work())

    @property
    def started(self):
        return self.first is not None

    async def listen(self):
        while True:
            event = await self.queue.get()
            if event.get('session') != self.sid:
                continue
            if event.get('type') == 'delta' and event.get('kind') == 'text':
                self.raw += str(event.get('text') or '')
                self.wake.set()
            elif event.get('type') == 'tool_start' and self.raw[-1:] not in ('', '\n'):
                self.raw += '\n'  # текст до тула и после — разные фразы
                self.wake.set()

    async def work(self):
        while True:
            await self.wake.wait()
            self.wake.clear()
            while True:
                tail = self.raw[self.used:]
                size = cut(tail, self.closing)
                if not size:
                    break
                self.used += size
                await self.say(tail[:size])
            if self.closing and self.used >= len(self.raw):
                return

    async def say(self, text):
        speech = spoken(text)
        if not speech or self.error:
            return
        try:
            if self.format is None:
                await self.allow()  # тот же Предохранитель, что и у озвучки целиком
            result = await self.ctx.voice.speak(speech, session_id=self.sid, format='wav')
            params, frames = pcm(result['audio'])
        except Exception as exc:  # noqa: BLE001 — отказ или сбой: дальше обычной озвучкой целиком
            self.error = exc
            return
        if self.format is None:
            self.format = params
            self.stream.write(wav_header(*params))
        elif params != self.format:
            return  # другой формат в тот же поток не склеить; голос агента один, так не бывает
        self.stream.write(frames)
        self.said.append(speech)
        if self.first is None:
            self.first = time.monotonic()
            try:
                await self.start()  # знак колонке: открывай поток
            except (HumanError, OSError, ConnectionError):
                pass

    async def finish(self, answer):
        """Ход закончен: договорить хвост. Ответ не из потока (send_message, вопрос предохранителя) — тоже сюда."""
        self.reader.cancel()
        self.ctx.bus.unsubscribe(self.queue)
        if answer and squash(answer) not in squash(self.raw):
            self.raw += '\n' + answer
        self.closing = True
        self.wake.set()
        try:
            await self.worker
        finally:
            if self.started:
                self.stream.close()
        return ' '.join(self.said)

    def cancel(self):
        self.reader.cancel()
        self.worker.cancel()
        self.ctx.bus.unsubscribe(self.queue)
        self.stream.close()


def squash(text):
    return re.sub(r'\W+', '', str(text or '')).lower()
