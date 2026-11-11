"""Поиск по смыслу без numpy: векторы от модели роли «Эмбеддинги» + битовые подписи.

Вектор куска хранится в mem_chunks.emb (float32, нормированный), рядом — подпись mem_chunks.sig:
знак каждой компоненты в бит. Поиск: расстояние Хэмминга между подписями — одно `(a ^ b).bit_count()`
на кусок (на уровне C, десятки тысяч сравнений — миллисекунды) → топ-200 кандидатов → точный косинус
→ топ-k. Подписи читаются из базы на каждый поиск, в памяти между поисками ничего не держится.

Векторы досчитываются в фоне после ходов, пачками; нет роли «Эмбеддинги» — модуль молчит, поиск
работает словами. Сменилась модель эмбеддингов — старые векторы сбрасываются (векторы разных
моделей несравнимы) и досчитываются заново.
"""

from __future__ import annotations

import asyncio
import heapq
import math
from array import array

CANDIDATES = 200
BATCH = 32
KINDS = ("diary", "session", "fact", "note", "core")


def pack(vec: list[float]) -> tuple[bytes, bytes]:
    n = math.sqrt(sum(v * v for v in vec)) or 1.0
    unit = array("f", (v / n for v in vec))
    bits = 0
    for i, v in enumerate(unit):
        if v > 0:
            bits |= 1 << i
    return unit.tobytes(), bits.to_bytes((len(unit) + 7) // 8, "little")


def unpack(blob: bytes) -> array:
    a = array("f")
    a.frombytes(blob)
    return a


class Embedder:
    def __init__(self, agent_ctx):
        self.app = agent_ctx
        self.store = agent_ctx.store
        self._lock = asyncio.Lock()
        self.last_error: str | None = None

    # настроено ли

    def available(self) -> bool:
        if not self.app.settings.get("memory", "vectors", True):
            return False
        return bool([c for c in self.app.router.chain("embed", fallback_main=False) if c.provider.get("kind", "openai") == "openai"])

    def model_key(self) -> str | None:
        chain = [c for c in self.app.router.chain("embed", fallback_main=False) if c.provider.get("kind", "openai") == "openai"]
        return f"{chain[0].provider['id']}/{chain[0].model}" if chain else None

    # досчёт

    def pending(self, limit: int = 1000) -> list[tuple[int, str]]:
        marks = ",".join("?" * len(KINDS))
        return [(r[0], r[1]) for r in self.store.q(f"SELECT id, text FROM mem_chunks WHERE emb IS NULL AND kind IN ({marks}) ORDER BY ts DESC LIMIT ?",
                                                   (*KINDS, limit))]

    def _check_model(self) -> None:
        key = self.model_key()
        if key and self.store.meta_get("embed:model") != key:
            if self.store.meta_get("embed:model") is not None:
                self.store.x("UPDATE mem_chunks SET emb = NULL, sig = NULL WHERE emb IS NOT NULL")  # другая модель — векторы несравнимы
            self.store.meta_set("embed:model", key)

    async def fill(self, *, max_items: int = 512) -> int:
        """Посчитать векторы для кусков без них. Возвращает, сколько посчитано."""
        if not self.available():
            return 0
        async with self._lock:
            await asyncio.to_thread(self.app.index.sync)
            self._check_model()
            todo = self.pending(max_items)
            done = 0
            for i in range(0, len(todo), BATCH):
                part = todo[i:i + BATCH]
                try:
                    vecs, _ = await self.app.router.embed([t[:4000] for _, t in part])
                except Exception as e:  # noqa: BLE001 — векторы не обязательны: запомним ошибку для пульта
                    self.last_error = str(getattr(e, "title", "") or e)[:300]
                    break
                with self.store.tx() as db:
                    for (cid, _), v in zip(part, vecs):
                        emb, sig = pack(v)
                        db.execute("UPDATE mem_chunks SET emb = ?, sig = ? WHERE id = ?", (emb, sig, cid))
                done += len(part)
                self.last_error = None
            return done

    async def embed_query(self, text: str, timeout: float = 8.0) -> list[float] | None:
        if not self.available():
            return None
        try:
            vecs, key = await asyncio.wait_for(self.app.router.embed([text[:2000]]), timeout)
        except Exception as e:  # noqa: BLE001
            self.last_error = str(getattr(e, "title", "") or e)[:300]
            return None
        if self.store.meta_get("embed:model") not in (None, key):
            return None  # индекс посчитан другой моделью — сравнивать нельзя, пока не пересчитан
        return vecs[0]

    # поиск

    def nearest(self, qvec: list[float], k: int = 40) -> list[int]:
        """Подписи читаются из базы на каждый поиск, а не держатся в памяти: десятки миллисекунд на запрос
        против мегабайт постоянно — для бортового агента память дороже."""
        if not qvec:
            return []
        qemb, qsig_b = pack(qvec)
        qsig = int.from_bytes(qsig_b, "little")
        q = unpack(qemb)
        rows = self.store.q("SELECT id, sig FROM mem_chunks WHERE sig IS NOT NULL")
        best = heapq.nsmallest(CANDIDATES, (((int.from_bytes(sig, "little") ^ qsig).bit_count(), cid) for cid, sig in rows))
        if not best:
            return []
        cand = [cid for _, cid in best]
        scored = []
        for cid, blob in self.store.q(f"SELECT id, emb FROM mem_chunks WHERE id IN ({','.join('?' * len(cand))})", cand):
            v = unpack(blob)
            if len(v) != len(q):
                continue
            scored.append((sum(a * b for a, b in zip(q, v)), cid))
        scored.sort(reverse=True)
        return [cid for sim, cid in scored[:k] if sim > 0.2]

    def status(self) -> dict:
        row = self.store.one("SELECT COUNT(*) AS n FROM mem_chunks WHERE emb IS NULL")
        return {"available": self.available(), "model": self.store.meta_get("embed:model"), "pending": row["n"] if row else 0, "error": self.last_error}
