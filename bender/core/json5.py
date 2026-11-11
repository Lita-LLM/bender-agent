"""Разбор JSON5 — формата конфига OpenClaw (`~/.openclaw/openclaw.json`). Свой, маленький.

Понимает: комментарии // и /* */, висячие запятые, ключи без кавычек, строки в одинарных
кавычках, переносы строк внутри строк через `\\`, шестнадцатеричные числа, числа с точкой в
начале или в конце, +/-Infinity, NaN, ведущий +. Обычный JSON — частный случай.
"""

from __future__ import annotations

import math
import re

_ID_START = re.compile(r"[A-Za-z_$À-￿]")
_ID_CHAR = re.compile(r"[A-Za-z0-9_$À-￿]")
_NUM = re.compile(r"[+-]?(0[xX][0-9a-fA-F]+|Infinity|NaN|(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?)")
_ESC = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "0": "\0", "/": "/", "\\": "\\", "'": "'", '"': '"'}


class JSON5Error(ValueError):
    def __init__(self, msg: str, text: str, pos: int):
        line = text.count("\n", 0, pos) + 1
        col = pos - (text.rfind("\n", 0, pos) + 1) + 1
        super().__init__(f"{msg} (строка {line}, столбец {col})")
        self.line = line
        self.col = col


class _P:
    def __init__(self, text: str):
        self.t = text
        self.i = 0
        self.depth = 0

    def err(self, msg: str):
        raise JSON5Error(msg, self.t, self.i)

    def ws(self) -> None:
        t = self.t
        while self.i < len(t):
            c = t[self.i]
            if c in " \t\r\n﻿   ":
                self.i += 1
            elif t.startswith("//", self.i):
                j = t.find("\n", self.i)
                self.i = len(t) if j < 0 else j + 1
            elif t.startswith("/*", self.i):
                j = t.find("*/", self.i + 2)
                if j < 0:
                    self.err("незакрытый комментарий /*")
                self.i = j + 2
            else:
                break

    def value(self):
        if self.depth >= 100:self.err('Слишком глубокая вложенность JSON5')
        self.depth += 1
        try:return self._value()
        finally:self.depth -= 1

    def _value(self):
        self.ws()
        if self.i >= len(self.t):
            self.err("неожиданный конец")
        c = self.t[self.i]
        if c == "{":
            return self.obj()
        if c == "[":
            return self.arr()
        if c in "\"'":
            return self.string()
        for word, val in (("true", True), ("false", False), ("null", None)):
            if self.t.startswith(word, self.i) and not self._idchar_at(self.i + len(word)):
                self.i += len(word)
                return val
        m = _NUM.match(self.t, self.i)
        if m and m.group(0) not in ("+", "-"):
            self.i = m.end()
            s = m.group(0)
            sign = -1 if s.startswith("-") else 1
            body = s.lstrip("+-")
            if body == "Infinity":
                return sign * math.inf
            if body == "NaN":
                return math.nan
            if body.lower().startswith("0x"):
                return sign * int(body, 16)
            if re.fullmatch(r"\d+", body):
                if len(body)>4096:self.err("Слишком длинное число")
                return sign * int(body)
            return sign * float(body)
        self.err(f"непонятный символ {c!r}")

    def _idchar_at(self, j: int) -> bool:
        return j < len(self.t) and bool(_ID_CHAR.match(self.t[j]))

    def string(self) -> str:
        q = self.t[self.i]
        self.i += 1
        out = []
        t = self.t
        while True:
            if self.i >= len(t):
                self.err("незакрытая строка")
            c = t[self.i]
            if c == q:
                self.i += 1
                return "".join(out)
            if c == "\\":
                self.i += 1
                e = t[self.i] if self.i < len(t) else ""
                if e == "\n":
                    self.i += 1
                    continue
                if e == "\r":
                    self.i += 2 if t.startswith("\r\n", self.i) else 1
                    continue
                if e == "u":
                    hexs = t[self.i + 1 : self.i + 5]
                    if not re.fullmatch(r"[0-9a-fA-F]{4}", hexs):
                        self.err("плохая последовательность \\u")
                    code = int(hexs, 16)
                    self.i += 5
                    # суррогатная пара
                    if 0xD800 <= code <= 0xDBFF and t.startswith("\\u", self.i):
                        lower = t[self.i + 2 : self.i + 6]
                        if not re.fullmatch(r'[0-9a-fA-F]{4}',lower):self.err('Неполная суррогатная пара')
                        low = int(lower, 16)
                        if 0xDC00 <= low <= 0xDFFF:
                            code = 0x10000 + ((code - 0xD800) << 10) + (low - 0xDC00)
                            self.i += 6
                    out.append(chr(code))
                    continue
                if e == "x":
                    digits=t[self.i + 1 : self.i + 3]
                    if not re.fullmatch(r'[0-9a-fA-F]{2}',digits):self.err('Плохая шестнадцатеричная последовательность')
                    out.append(chr(int(digits,16)))
                    self.i += 3
                    continue
                out.append(_ESC.get(e, e))
                self.i += 1
                continue
            if c == "\n":
                self.err("перевод строки внутри строки")
            out.append(c)
            self.i += 1

    def key(self) -> str:
        self.ws()
        c = self.t[self.i] if self.i < len(self.t) else ""
        if c in "\"'":
            return self.string()
        if not _ID_START.match(c or " "):
            self.err("ожидалось имя ключа")
        j = self.i
        while self._idchar_at(j):
            j += 1
        k = self.t[self.i : j]
        self.i = j
        return k

    def obj(self) -> dict:
        self.i += 1
        out: dict = {}
        while True:
            self.ws()
            if self.i < len(self.t) and self.t[self.i] == "}":
                self.i += 1
                return out
            k = self.key()
            self.ws()
            if self.i >= len(self.t) or self.t[self.i] != ":":
                self.err("ожидалось «:» после ключа")
            self.i += 1
            out[k] = self.value()
            self.ws()
            if self.i < len(self.t) and self.t[self.i] == ",":
                self.i += 1
                continue
            if self.i < len(self.t) and self.t[self.i] == "}":
                self.i += 1
                return out
            self.err("ожидалась «,» или «}»")

    def arr(self) -> list:
        self.i += 1
        out: list = []
        while True:
            self.ws()
            if self.i < len(self.t) and self.t[self.i] == "]":
                self.i += 1
                return out
            out.append(self.value())
            self.ws()
            if self.i < len(self.t) and self.t[self.i] == ",":
                self.i += 1
                continue
            if self.i < len(self.t) and self.t[self.i] == "]":
                self.i += 1
                return out
            self.err("ожидалась «,» или «]»")


def loads(text: str):
    if not isinstance(text,str):raise JSON5Error('Нужен текст JSON5','',0)
    if len(text)>16*1024*1024:raise JSON5Error('JSON5 слишком велик','',0)
    p = _P(text)
    v = p.value()
    p.ws()
    if p.i != len(p.t):
        p.err("лишнее после значения")
    return v
