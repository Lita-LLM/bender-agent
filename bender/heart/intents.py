"""Намерения по событию: «когда зайдёт речь о релизе — напомни про changelog».

Проверка — на каждом сообщении хозяина, без модели: ключевые слова сравниваются по основам (как поиск
памяти). Сработавшее намерение кладётся рядом с сообщением хозяина («Ты сам себе напомнил: …») — не
больше 3 за ход. У намерения есть срок (по умолчанию 90 дней), бюджет срабатываний (3) и пауза между
ними (24 ч). Истёкшие закрывает пульс; всё видно в пульте.
"""

from __future__ import annotations

import json
import time

from ..core.errors import HumanError
from ..core.ids import new_id
from ..memory.stem import stems

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS intents (
        id TEXT PRIMARY KEY, text TEXT NOT NULL, keywords TEXT NOT NULL, created_at REAL NOT NULL, created_by TEXT NOT NULL DEFAULT 'bender',
        expires REAL, budget INTEGER NOT NULL DEFAULT 3, fired INTEGER NOT NULL DEFAULT 0, pause_hours REAL NOT NULL DEFAULT 24,
        last_fired REAL, status TEXT NOT NULL DEFAULT 'armed', source TEXT NOT NULL DEFAULT '', meta TEXT NOT NULL DEFAULT '{}')""",
    """CREATE INDEX IF NOT EXISTS intents_status ON intents(status)""",
]
MAX_PER_TURN = 3


def _row(r) -> dict:
    d = {k: r[k] for k in r.keys()}
    d["keywords"] = json.loads(d["keywords"])
    d["meta"] = json.loads(d.get("meta") or "{}")
    return d


def matches(keywords: list[str], text: str) -> str | None:
    """Какое ключевое выражение встретилось: все основы выражения есть в тексте (префиксно для длинных)."""
    have = set(stems(text))
    for kw in keywords:
        need = stems(kw)
        if not need:
            continue
        if all(s in have or (len(s) >= 5 and any(h.startswith(s[:-1]) for h in have)) for s in need):
            return kw
    return None


class Intents:
    def __init__(self, store):
        self.store = store
        store.migrate("intents", 1, SCHEMA)

    def add(self, text: str, keywords: list[str] | str, *, days: float = 90, budget: int = 3, pause_hours: float = 24,
            created_by: str = "bender", source: str = "") -> dict:
        kws = [k.strip() for k in (keywords.split(",") if isinstance(keywords, str) else keywords) if k and k.strip()]
        if not text.strip():
            raise HumanError("bad_intent", "О чём напомнить себе?", "Нужен текст намерения.")
        if not kws or not any(stems(k) for k in kws):
            raise HumanError("bad_intent", "На какие слова срабатывать?", "Нужны ключевые слова: «релиз», «changelog, журнал изменений».")
        now = time.time()
        iid = new_id()
        self.store.x("INSERT INTO intents(id, text, keywords, created_at, created_by, expires, budget, pause_hours, source) VALUES(?,?,?,?,?,?,?,?,?)",
                     (iid, text.strip()[:500], json.dumps(kws, ensure_ascii=False), now, created_by, now + days * 86400 if days else None,
                      max(1, int(budget)), max(0.0, float(pause_hours)), source))
        return self.get(iid)

    def get(self, iid: str) -> dict | None:
        r = self.store.one("SELECT * FROM intents WHERE id = ?", (iid,))
        return _row(r) if r else None

    def all(self, *, active_only: bool = False) -> list[dict]:
        sql = "SELECT * FROM intents" + (" WHERE status = 'armed'" if active_only else "") + " ORDER BY created_at DESC"
        return [_row(r) for r in self.store.q(sql)]

    def find(self, key: str) -> dict | None:
        d = self.get(key)
        if d:
            return d
        k = (key or "").strip().lower()
        return next((x for x in self.all(active_only=True) if k and (k in x["text"].lower() or k in [w.lower() for w in x["keywords"]])), None)

    def cancel(self, iid: str) -> bool:
        return bool(self.store.x("UPDATE intents SET status = 'cancelled' WHERE id = ? AND status = 'armed'", (iid,)))

    def check(self, text: str, *, now: float | None = None) -> list[dict]:
        """Сработавшие на этом тексте (и отметить срабатывание). Пишет только когда что-то сработало."""
        now = now or time.time()
        out = []
        for d in self.all(active_only=True):
            if d["expires"] and d["expires"] < now:
                continue
            if d["last_fired"] and now - d["last_fired"] < d["pause_hours"] * 3600:
                continue
            kw = matches(d["keywords"], text)
            if not kw:
                continue
            fired = d["fired"] + 1
            status = "done" if fired >= d["budget"] else "armed"
            self.store.x("UPDATE intents SET fired = ?, last_fired = ?, status = ? WHERE id = ?", (fired, now, status, d["id"]))
            out.append({**d, "fired": fired, "status": status, "matched": kw})
            if len(out) >= MAX_PER_TURN:
                break
        return out

    def expire(self, now: float | None = None) -> int:
        """Закрыть просроченные (пульс, сон). Пишет, только если есть что закрыть."""
        now = now or time.time()
        if not self.store.one("SELECT 1 FROM intents WHERE status = 'armed' AND expires IS NOT NULL AND expires < ? LIMIT 1", (now,)):
            return 0
        return self.store.x("UPDATE intents SET status = 'expired' WHERE status = 'armed' AND expires IS NOT NULL AND expires < ?", (now,))


def hints(agent_ctx, text: str) -> str:
    """Строка для запроса: сработавшие намерения рядом с сообщением хозяина."""
    if not text.strip() or not agent_ctx.settings.get("heart", "intents", True):
        return ""
    try:
        hits = agent_ctx.heart.intents.check(text)
    except Exception:  # noqa: BLE001 — намерения не должны ломать ход
        return ""
    if not hits:
        return ""
    for h in hits:
        agent_ctx.journal.write("heart", f"Намерение сработало на «{h['matched']}»: {h['text'][:120]}", actor="heart", data={"intent": h["id"], "fired": h["fired"]})
        agent_ctx.bus.publish({"type": "intent", "intent": h})
    return "[Ты сам себе напомнил (намерение сработало на слова хозяина):\n" + "\n".join(f"• {h['text']}" for h in hits) + "]\n"
