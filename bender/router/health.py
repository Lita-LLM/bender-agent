"""Здоровье ключей и моделей: остывание после ошибок.

Ключ (провайдер + номер ключа) после лимита или перегрузки остывает: 30 с → 1 мин → 5 мин
(потолок), счётчик сбрасывается после успеха или через 30 минут тишины. Нет денег и
неверный ключ — выключение на 10 минут (провайдеры иногда отвечают так временно), потом
одна пробная попытка. Остывание может касаться одной модели, а не всего ключа: лимит на
модель X не мешает модели Y того же провайдера.

Хранится в памяти процесса: после перезапуска все снова «здоровы» — это честнее, чем
помнить вчерашние сбои.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

STEPS = (30.0, 60.0, 300.0)
LONG = 600.0
RESET_AFTER = 1800.0


@dataclass
class Mark:
    errors: int = 0
    until: float = 0.0
    reason: str = ""
    last_error: float = 0.0
    last_ok: float = 0.0
    message: str = ""


@dataclass
class Health:
    keys: dict[tuple, Mark] = field(default_factory=dict)
    models: dict[tuple, Mark] = field(default_factory=dict)

    def _get(self, table: dict, key: tuple) -> Mark:
        m = table.get(key)
        if m is None:
            m = table[key] = Mark()
        if m.errors and time.time() - m.last_error > RESET_AFTER:
            m.errors = 0
        return m

    def blocked(self, provider: str, key_idx: int, model: str) -> tuple[bool, float, str]:
        now = time.time()
        k = self.keys.get((provider, key_idx))
        if k and k.until > now:
            return True, k.until - now, k.reason
        m = self.models.get((provider, model))
        if m and m.until > now:
            return True, m.until - now, m.reason
        return False, 0.0, ""

    def fail(self, provider: str, key_idx: int, model: str, klass: str, retry_after: float | None, message: str = "") -> float:
        """Отметить сбой; вернуть, на сколько секунд остывает."""
        now = time.time()
        if klass in ("billing", "auth"):
            mark = self._get(self.keys, (provider, key_idx))
            wait = LONG
        elif klass == "not_found":
            mark = self._get(self.models, (provider, model))
            wait = LONG
        elif klass in ("rate_limit", "overloaded"):
            # лимит часто на модель, а не на ключ; перегрузка — на провайдера
            mark = self._get(self.models if klass == "rate_limit" else self.keys, (provider, model) if klass == "rate_limit" else (provider, key_idx))
            wait = STEPS[min(mark.errors, len(STEPS) - 1)]
            if retry_after:
                wait = min(max(wait, retry_after), 86400.0)
        else:
            mark = self._get(self.keys, (provider, key_idx))
            wait = STEPS[min(mark.errors, len(STEPS) - 1)] if mark.errors >= 2 else 0.0
        mark.errors += 1
        mark.last_error = now
        mark.reason = klass
        mark.message = message[:300]
        if wait:
            mark.until = max(mark.until, now + wait)
        return wait

    def ok(self, provider: str, key_idx: int, model: str) -> None:
        now = time.time()
        for table, key in ((self.keys, (provider, key_idx)), (self.models, (provider, model))):
            m = table.get(key)
            if m:
                m.errors = 0
                m.until = 0.0
                m.reason = ""
                m.last_ok = now

    def snapshot(self) -> dict:
        now = time.time()
        out = {"keys": [], "models": []}
        for name, table in (("keys", self.keys), ("models", self.models)):
            for key, m in table.items():
                out[name].append({"key": list(key), "errors": m.errors, "cooling": max(0.0, m.until - now), "reason": m.reason, "message": m.message})
        return out
