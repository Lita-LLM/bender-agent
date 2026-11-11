"""ULID: 48 бит времени в миллисекундах + 80 случайных бит, Crockford base32.

ID внутри одного процесса монотонны: если время не сдвинулось, случайная часть
увеличивается на единицу, так что сортировка по ID совпадает с порядком создания.
"""

from __future__ import annotations

import secrets
import threading
import time

ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_lock = threading.Lock()
_last_ms = -1
_last_rand = 0


def b32encode(value: int, length: int) -> str:
    out = []
    for _ in range(length):
        out.append(ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(out))


def new_id() -> str:
    global _last_ms, _last_rand
    with _lock:
        ms = time.time_ns() // 1_000_000
        if ms <= _last_ms:
            ms = _last_ms
            _last_rand = (_last_rand + 1) & ((1 << 80) - 1)
            if _last_rand == 0:
                ms += 1
        else:
            _last_rand = secrets.randbits(80)
        _last_ms = ms
        return b32encode(ms, 10) + b32encode(_last_rand, 16)


def id_at(ms: int) -> str:
    """ID с заданным временем (для перенесённых сообщений: порядок по ID = порядок по времени)."""
    return b32encode(max(0, int(ms)), 10) + b32encode(secrets.randbits(80) >> 1, 16)
