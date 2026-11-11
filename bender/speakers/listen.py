"""Конец фразы по громкости и упаковка звука колонки в WAV.

Колонка присылает уже очищенный звук (эхо и шум сняты её VQE): 16 кГц, моно, 16 бит.
Поэтому хватает простого порога над уровнем тишины, без моделей.
"""
from __future__ import annotations

import array
import io
import math
import sys
import wave

RATE = 16000
FRAME = 480  # 30 мс


def rms(samples: array.array) -> float:
    if not samples:
        return 0.0
    return math.sqrt(sum(s * s for s in samples) / len(samples))


class Phrase:
    """Копит звук и решает, когда человек договорил.

    feed() возвращает 'speech' при начале речи, 'end' при конце фразы, 'silence' — если
    так ничего и не сказали, 'limit' — если фраза длиннее предела; иначе None.
    """

    def __init__(self, *, silence_ms: int = 800, limit_s: int = 15, wait_s: float = 8.0, floor: float = 300.0):
        self.silence_frames = max(1, silence_ms // 30)
        self.limit = limit_s * RATE
        self.wait = int(wait_s * RATE)
        self.floor = floor
        self.noise = None
        self.audio = bytearray()
        self.tail = b''
        self.speaking = False
        self.quiet = 0
        self.loud = 0
        self.done = None

    def threshold(self) -> float:
        return max(self.floor, (self.noise or 0) * 3)

    def feed(self, chunk: bytes):
        if self.done:
            return None
        self.audio += chunk
        data = self.tail + chunk
        usable = len(data) - len(data) % (FRAME * 2)
        self.tail = data[usable:]
        result = None
        for start in range(0, usable, FRAME * 2):
            samples = array.array('h', data[start:start + FRAME * 2])
            if sys.byteorder == 'big':
                samples.byteswap()
            level = rms(samples)
            if not self.speaking:
                # Уровень тишины падает сразу, а растёт медленно (~в 2 раза за 1 с): речь сразу
                # после слова вызова не принимается за фон, а ровный шум комнаты — принимается.
                self.loud = self.loud + 1 if level > self.threshold() else 0
                base = self.floor / 3 if self.noise is None else self.noise
                self.noise = level if level < base else min(level, base * 1.02)
                if self.loud >= 3:  # 90 мс подряд — не щелчок
                    self.speaking, self.quiet, result = True, 0, 'speech'
            else:
                self.quiet = self.quiet + 1 if level <= self.threshold() else 0
                if self.quiet >= self.silence_frames:
                    self.done = 'end'
                    return 'end'
        samples_total = len(self.audio) // 2
        if samples_total >= self.limit:
            self.done = 'limit' if self.speaking else 'silence'
            return self.done
        if not self.speaking and samples_total >= self.wait:
            self.done = 'silence'
            return 'silence'
        return result

    def seconds(self) -> float:
        return len(self.audio) / 2 / RATE

    def wav(self) -> bytes:
        out = io.BytesIO()
        with wave.open(out, 'wb') as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(bytes(self.audio[:len(self.audio) - len(self.audio) % 2]))
        return out.getvalue()


SAY = {'°C': ' градусов', '°': ' градусов', '№': 'номер ', '×': ' на ', '≈': ' примерно ', '≤': ' не больше ', '≥': ' не меньше ',
       '±': ' плюс-минус ', '=': ' равно ', '…': '...', '—': ' — ', '«': '', '»': '', '"': '', '„': '', '“': '', '”': ''}


def unsayable(text: str) -> str:
    """Эмодзи, стрелки, значки и невидимые символы синтез читает криво или называет вслух — их нет в речи."""
    import unicodedata
    for sign, word in SAY.items():
        text = text.replace(sign, word)
    out = []
    for char in text:
        kind = unicodedata.category(char)
        if kind in ('So', 'Sk', 'Co', 'Cs', 'Cn', 'Cf') or char in '\ufe0e\ufe0f\u20e3' or '\u2190' <= char <= '\u21ff' or char in '#*~^<>{}[]\\_':
            out.append(' ')
        else:
            out.append(char)
    return ''.join(out)


def spoken(text: str, limit: int = 1200) -> str:
    """Убрать разметку, которую синтез прочёл бы вслух: звёздочки, решётки, ссылки, код."""
    import re
    text = re.sub(r'```.*?```', ' (код — в пульте) ', text, flags=re.S)
    text = re.sub(r'`([^`]*)`', r'\1', text)
    text = re.sub(r'!?\[([^\]]*)\]\([^)]*\)', r'\1', text)
    text = re.sub(r'https?://\S+', 'ссылка', text)
    text = re.sub(r'^\s{0,3}(#{1,6}|>|[-*•]|\d+[.)])\s+', '', text, flags=re.M)
    text = re.sub(r'(\*\*|__|\*|_|~~|==|\|\|)', '', text)
    text = re.sub(r'\|', ', ', text)
    text = unsayable(text)
    text = re.sub(r'\s+', ' ', text).strip()
    text = re.sub(r'\s+([.,!?;:])', r'\1', text)
    text = re.sub(r'([,;:])(?:\s*[,;:])+', r'\1', text)
    text = re.sub(r'^[\s,.;:!?]+', '', text)
    if len(text) > limit:
        cut = text[:limit]
        end = max(cut.rfind('. '), cut.rfind('! '), cut.rfind('? '))
        text = (cut[:end + 1] if end > limit // 2 else cut) + ' Остальное — в пульте.'
    return text
