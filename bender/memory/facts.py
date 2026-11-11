"""Факты: «субъект — связь — объект» с окном действительности (как в Zep/Graphiti).

Изменился факт («Миша переехал в Казань») — старый не удаляется, а закрывается датой (valid_to),
новый открывается: агент помнит и как сейчас, и как было. Связь может быть одиночной (set: у человека
один город) или множественной (add: любит чай, любит кофе).

У факта есть провенанс: от хозяина, вывел сам, недоверенное (записан в заражённом ходе) — недоверенное
не продвигается в ядро и в поиске помечено. Факты попадают в индекс памяти (kind=fact) и находятся
тулом recall; в контекст сами не кладутся.
"""

from __future__ import annotations

import re
import time

from .index import Chunk

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS facts (
        id INTEGER PRIMARY KEY, subject TEXT NOT NULL, relation TEXT NOT NULL, object TEXT NOT NULL, key TEXT NOT NULL,
        valid_from REAL NOT NULL, valid_to REAL, replaced_by INTEGER, provenance TEXT NOT NULL DEFAULT 'bender',
        importance INTEGER NOT NULL DEFAULT 5, source TEXT NOT NULL DEFAULT '', created REAL NOT NULL)""",
    """CREATE INDEX IF NOT EXISTS facts_key ON facts(key, valid_to)""",
]


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower().replace("ё", "е"))


def day(ts: float | None) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(ts)) if ts else ""


class Facts:
    def __init__(self, store, index):
        self.store = store
        self.index = index
        store.migrate("facts", 1, SCHEMA)

    def _row(self, r) -> dict:
        return {k: r[k] for k in r.keys()}

    def active(self, subject: str | None = None, relation: str | None = None) -> list[dict]:
        sql = "SELECT * FROM facts WHERE valid_to IS NULL"
        params: list = []
        if subject:
            sql += " AND key LIKE ?"
            params.append(norm(subject) + "|%")
        if relation:
            sql += " AND key LIKE ?"
            params.append("%|" + norm(relation))
        return [self._row(r) for r in self.store.q(sql + " ORDER BY subject, relation, valid_from", params)]

    def history(self, subject: str, relation: str | None = None) -> list[dict]:
        sql = "SELECT * FROM facts WHERE key LIKE ?"
        params = [norm(subject) + "|" + (norm(relation) if relation else "%")]
        return [self._row(r) for r in self.store.q(sql + " ORDER BY valid_from", params)]

    def add(self, subject: str, relation: str, obj: str, *, single: bool = False, provenance: str = "bender", importance: int = 5,
            source: str = "", when: float | None = None) -> dict:
        """Добавить факт. single — связь одиночная: прежнее значение закрывается. Тот же факт ещё раз — не дублируется."""
        subject, relation, obj = subject.strip(), relation.strip(), obj.strip()
        if not subject or not relation or not obj:
            raise ValueError("нужны субъект, связь и объект")
        key = f"{norm(subject)}|{norm(relation)}"
        now = when or time.time()
        # сравнение в Python: lower() в SQLite не понижает кириллицу
        same = next((r for r in self.store.q("SELECT * FROM facts WHERE key = ? AND valid_to IS NULL", (key,)) if norm(r["object"]) == norm(obj)), None)
        if same:
            return {**self._row(same), "status": "already"}
        closed = []
        with self.store.tx() as db:
            cur = db.execute("INSERT INTO facts(subject, relation, object, key, valid_from, provenance, importance, source, created) VALUES(?,?,?,?,?,?,?,?,?)",
                             (subject, relation, obj, key, now, provenance, max(1, min(10, int(importance))), source, time.time()))
            fid = cur.lastrowid
            if single:
                for r in db.execute("SELECT id, object FROM facts WHERE key = ? AND valid_to IS NULL AND id != ?", (key, fid)).fetchall():
                    db.execute("UPDATE facts SET valid_to = ?, replaced_by = ? WHERE id = ?", (now, fid, r[0]))
                    closed.append(r[1])
        self.reindex()
        row = self._row(self.store.one("SELECT * FROM facts WHERE id = ?", (fid,)))
        return {**row, "status": "replaced" if closed else "added", "closed": closed}

    def close(self, subject: str, relation: str, obj: str | None = None) -> int:
        """Факт перестал быть верным — закрыть (без замены)."""
        key = f"{norm(subject)}|{norm(relation)}"
        ids = [r["id"] for r in self.store.q("SELECT id, object FROM facts WHERE key = ? AND valid_to IS NULL", (key,)) if not obj or norm(r["object"]) == norm(obj)]
        n = 0
        for fid in ids:
            n += self.store.x("UPDATE facts SET valid_to = ? WHERE id = ?", (time.time(), fid))
        if n:
            self.reindex()
        return n

    def text(self, f: dict) -> str:
        period = f"с {day(f['valid_from'])}" + (f" по {day(f['valid_to'])} (уже неверно)" if f.get("valid_to") else "")
        return f"Факт: {f['subject']} — {f['relation']} — {f['object']} ({period})"

    def reindex(self) -> None:
        if self.index is None:
            return
        chunks = []
        for r in self.store.q("SELECT * FROM facts ORDER BY id"):
            f = self._row(r)
            chunks.append(Chunk(text=self.text(f), ref=f"fact:{f['id']}", ts=f["valid_from"], provenance=f["provenance"],
                                importance=f["importance"] if not f["valid_to"] else max(1, f["importance"] - 3)))
        self.index.add_chunks("facts", "fact", chunks, title="факты")

    def count(self) -> dict:
        row = self.store.one("SELECT COUNT(*) AS n, SUM(valid_to IS NULL) AS a FROM facts")
        return {"total": row["n"] or 0, "active": row["a"] or 0}
