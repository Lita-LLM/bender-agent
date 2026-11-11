"""Бортжурнал: всё, что Бендер сделал и что решил Предохранитель, с цепочкой хешей.

Каждая запись подписана HMAC-SHA256 от (хеш предыдущей записи + содержимое) ключом,
который лежит в data/local/journal.key. Удалить или поправить запись в середине —
проверка цепочки покажет, где разрыв. Удалить последние записи цепочка сама не покажет,
поэтому рядом с ключом лежит якорь (journal.anchor) — номер и хеш последней записи:
если в базе записей меньше, чем помнит якорь, хвост обрезан (или базу откатили).
Это не защищает от того, кто украл и ключ, и базу, и якорь, но честная история действий
агента всегда видна.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from ..core.fsutil import atomic_write
from ..core.store import Store, dumps

GENESIS = "0" * 64


class Journal:
    def __init__(self, store: Store, key_path: Path, on_entry=None):
        self.store = store
        self.key = self._load_key(key_path)
        self.anchor = Path(key_path).with_name("journal.anchor")
        self.on_entry = on_entry
        row = store.one("SELECT hash FROM journal ORDER BY seq DESC LIMIT 1")
        self._last = row["hash"] if row else GENESIS

    @staticmethod
    def _load_key(path: Path) -> bytes:
        try:
            data = path.read_bytes()
            if len(data) >= 32:
                return data[:32]
        except FileNotFoundError:
            pass
        key = secrets.token_bytes(32)
        atomic_write(path, key)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return key

    def _sign(self, prev: str, ts: float, kind: str, actor: str, summary: str, data: str) -> str:
        msg = "\x1f".join([prev, repr(ts), kind, actor, summary, data]).encode("utf-8")
        return hmac.new(self.key, msg, hashlib.sha256).hexdigest()

    def write(self, kind: str, summary: str, *, actor: str = "bender", data: dict | None = None) -> int:
        ts = time.time()
        payload = dumps(data or {})
        with self.store.tx() as db:
            row = db.execute("SELECT hash FROM journal ORDER BY seq DESC LIMIT 1").fetchone()
            prev = row["hash"] if row else GENESIS
            h = self._sign(prev, ts, kind, actor, summary, payload)
            if getattr(self, 'agent_column', False):
                cur = db.execute("INSERT INTO journal(ts,kind,actor,summary,data,hash,agent_id) VALUES(?,?,?,?,?,?,?)", (ts,kind,actor,summary,payload,h,(data or {}).get('agent_id','main')))
            else:
                cur = db.execute("INSERT INTO journal(ts, kind, actor, summary, data, hash) VALUES(?,?,?,?,?,?)", (ts, kind, actor, summary, payload, h))
            seq = cur.lastrowid
        self._last = h
        try:
            atomic_write(self.anchor, f"{seq}\t{h}\n".encode())
        except OSError:
            pass
        if self.on_entry:
            self.on_entry({"seq": seq, "ts": ts, "kind": kind, "actor": actor, "summary": summary, "data": data or {}})
        return seq

    def recent(self, limit: int = 200, before: int | None = None, kind: str | None = None, actor: str = "", capability: str = "", verdict: str = "", channel: str = "", session: str = "", agent_id: str = "") -> list[dict]:
        sql = "SELECT * FROM journal WHERE 1=1"
        params: list = []
        if agent_id:
            sql += " AND COALESCE(json_extract(data, '$.agent_id'), 'main') = ?"; params.append(agent_id)
        if before:
            sql += " AND seq < ?"
            params.append(before)
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if actor:
            sql += " AND actor = ?"; params.append(actor)
        for field,value in (("capability",capability),("verdict",verdict),("channel",channel),("session",session)):
            if value:
                sql += " AND json_extract(data, '$."+field+"') = ?"; params.append(value)
        sql += " ORDER BY seq DESC LIMIT ?"
        params.append(max(1,min(5000,int(limit))))
        return [{"seq": r["seq"], "ts": r["ts"], "kind": r["kind"], "actor": r["actor"], "summary": r["summary"], "data": json.loads(r["data"])} for r in self.store.q(sql, params)]

    def verify(self) -> dict:
        """Проверить всю цепочку. Возвращает {ok, checked, broken_at}."""
        prev = GENESIS
        n = 0
        last = 0
        for r in self.store.q("SELECT * FROM journal ORDER BY seq"):
            expect = self._sign(prev, r["ts"], r["kind"], r["actor"], r["summary"], r["data"])
            if not hmac.compare_digest(expect, r["hash"]):
                return {"ok": False, "checked": n, "broken_at": r["seq"]}
            prev = r["hash"]
            last = r["seq"]
            n += 1
        anchor = self._read_anchor()
        if anchor:
            a_seq, a_hash = anchor
            row = self.store.one("SELECT hash FROM journal WHERE seq = ?", (a_seq,))
            if row is None:
                return {"ok": False, "checked": n, "broken_at": None, "truncated": True, "anchor": a_seq, "last": last if n else 0}
            if not hmac.compare_digest(row["hash"], a_hash):
                return {"ok": False, "checked": n, "broken_at": a_seq}
        return {"ok": True, "checked": n, "broken_at": None}

    def _read_anchor(self) -> tuple[int, str] | None:
        try:
            seq, h = self.anchor.read_text(encoding="ascii").split()
            return int(seq), h
        except (OSError, ValueError):
            return None
