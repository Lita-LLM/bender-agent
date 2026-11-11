"""Хранилище: один файл SQLite для всего машинного состояния Бендера.

Что внутри: настройки, сессии и сообщения, вызовы моделей («что ушло в модель»),
бортжурнал. Память (индекс поиска), задачи, расписание появятся в своих вехах —
каждая подсистема добавляет свои таблицы через migrate().

Правила:
- WAL и synchronous=NORMAL: запись не ждёт fsync на каждую транзакцию (диск телефона
  целее), но база не ломается при убийстве процесса — теряется не больше последних мгновений;
- одна блокировка на соединение: из потоков и из asyncio пишем по очереди;
- схемы только дополняются (новые таблицы и колонки), версии хранятся в meta;
  незнакомые таблицы и колонки от будущих версий не трогаем.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import zlib
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .ids import new_id

BASE_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS settings (grp TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY, kind TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
        created_at REAL NOT NULL, updated_at REAL NOT NULL, meta TEXT NOT NULL DEFAULT '{}')""",
    """CREATE TABLE IF NOT EXISTS messages (
        id TEXT PRIMARY KEY, session_id TEXT NOT NULL, role TEXT NOT NULL, data TEXT NOT NULL,
        created_at REAL NOT NULL, meta TEXT NOT NULL DEFAULT '{}')""",
    """CREATE INDEX IF NOT EXISTS messages_by_session ON messages(session_id, id)""",
    """CREATE TABLE IF NOT EXISTS calls (
        id TEXT PRIMARY KEY, session_id TEXT, created_at REAL NOT NULL, role TEXT, provider TEXT, model TEXT,
        usage TEXT, cost REAL NOT NULL DEFAULT 0, seconds REAL, notice TEXT, request BLOB, error TEXT)""",
    """CREATE INDEX IF NOT EXISTS calls_by_session ON calls(session_id, id)""",
    """CREATE TABLE IF NOT EXISTS journal (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL, actor TEXT NOT NULL,
        summary TEXT NOT NULL, data TEXT NOT NULL DEFAULT '{}', hash TEXT NOT NULL)""",
]


MIGRATION_COPIES = 2  # копий базы перед миграциями в local/migration-backups


def dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))


def pack(obj) -> bytes:
    """Большой JSON — сжатым (снимки запросов к моделям)."""
    return zlib.compress(dumps(obj).encode("utf-8"), 6)


def unpack(blob: bytes | None):
    if not blob:
        return None
    return json.loads(zlib.decompress(blob).decode("utf-8"))


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._settings_root = self
        self._settings_revision = 0
        self._after_commit = []
        self._migration_backup = False
        self.db = None
        try:
            self.db = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None, timeout=10)
            self.db.row_factory = sqlite3.Row
            # SQLite lower() понимает только латиницу; архив и запасной поиск — по-русски.
            self.db.create_function('pylower',1,lambda text:text.lower() if isinstance(text,str) else text,deterministic=True)
            for pragma in ("journal_mode=WAL", "synchronous=NORMAL", "foreign_keys=ON", "temp_store=MEMORY", "busy_timeout=10000"):
                self.db.execute(f"PRAGMA {pragma}")
            self.migrate("base", 1, BASE_SCHEMA)
        except sqlite3.Error as exc:
            if self.db:self.db.close()
            from .errors import HumanError
            raise HumanError('database_open', 'Не удалось открыть базу Бендера', 'Файл не удалён. Проверь место/права; повреждённую базу восстанови из бекапа в отдельный слот.', detail=str(exc)) from None

    def close(self) -> None:
        with self._lock:
            try:
                self.db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error:
                pass
            self.db.close()

    @contextmanager
    def _locked(self):
        with self._lock:
            try:
                yield
            except sqlite3.Error as exc:
                if any(text in str(exc).lower() for text in ('disk is full','readonly','malformed','not a database','disk i/o')):
                    from .errors import HumanError
                    raise HumanError('database_write', 'База недоступна или на диске нет места', 'Освободи место, проверь права и повтори. При порче восстанови целую копию в отдельный слот.', detail=str(exc)) from None
                raise

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        callbacks=[]
        with self._locked():
            self.db.execute("BEGIN IMMEDIATE")
            root=self._settings_root
            root._after_commit=[]
            try:
                yield self.db
                self.db.execute("COMMIT")
                callbacks,root._after_commit=root._after_commit,[]
            except BaseException:
                root._after_commit=[]
                if self.db.in_transaction:
                    try:self.db.execute("ROLLBACK")
                    except sqlite3.Error:pass
                raise
        for callback in callbacks:callback()

    def after_commit(self, callback):
        """Наблюдатели настроек срабатывают после успешной общей транзакции."""
        with self._locked():
            if self.db.in_transaction:
                self._settings_root._after_commit.append(callback)
                return
        callback()

    def q(self, sql: str, params: tuple | list = ()) -> list[sqlite3.Row]:
        with self._locked():
            return self.db.execute(sql, params).fetchall()

    def one(self, sql: str, params: tuple | list = ()) -> sqlite3.Row | None:
        with self._locked():
            return self.db.execute(sql, params).fetchone()

    def x(self, sql: str, params: tuple | list = ()) -> int:
        """Выполнить изменение. INSERT — номер новой строки, остальное — сколько строк изменено."""
        with self._locked():
            cur = self.db.execute(sql, params)
            if sql.lstrip()[:7].upper() in ("INSERT ", "REPLACE"):
                return cur.lastrowid or 0
            return cur.rowcount

    # версии схем подсистем

    def schema_version(self, part: str) -> int:
        row = self.one("SELECT value FROM meta WHERE key = ?", (f"schema:{part}",)) if self._has_meta() else None
        return int(row["value"]) if row else 0

    def _has_meta(self) -> bool:
        return bool(self.db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone())

    def migrate(self, part: str, version: int, statements: list[str]) -> None:
        """Применить схему подсистемы, если она старее. Только дополняющие изменения."""
        with self._lock:
            current = self.schema_version(part)
            if current >= version:
                return
            # Откат слота использует ту же базу: схемы расширяются, данные не удаляются.
            import re

            for statement in statements:
                sql = statement.strip()
                if not re.match(r"(?is)^(CREATE\s+(?:UNIQUE\s+)?(?:TABLE|INDEX)\s|ALTER\s+TABLE\s+\w+\s+ADD\s+(?:COLUMN\s+)?\w+\s)", sql):
                    from .errors import HumanError

                    raise HumanError("migration_compat", "Миграция может сломать откат версии", "Допустимы CREATE TABLE/INDEX и ALTER TABLE ADD COLUMN; удаление и перезапись данных запрещены.")
                if re.match(r"(?is)^ALTER\s", sql) and re.search(r"(?is)\bNOT\s+NULL\b", sql) and not re.search(r"(?is)\bDEFAULT\s+(?!NULL\b)", sql):
                    from .errors import HumanError

                    raise HumanError("migration_compat", "Новая колонка требует поля от прежней версии", "Для NOT NULL нужен ненулевой DEFAULT; старая версия должна продолжать свои INSERT.")
            if self._has_meta() and not self._migration_backup:
                folder = self.path.parent / "local" / "migration-backups"
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                self.backup_to(folder / (new_id() + ".db"))
                self.prune_migration_copies()
                self._migration_backup = True
            with self.tx() as db:
                for st in statements:
                    altered = re.match(r"(?is)^\s*ALTER\s+TABLE\s+(\w+)\s+ADD\s", st)
                    before = {row["name"] for row in db.execute("PRAGMA table_info(" + altered.group(1) + ")")} if altered else set()
                    db.execute(st)
                    if altered:
                        for column in db.execute("PRAGMA table_info(" + altered.group(1) + ")"):
                            default = column["dflt_value"]
                            if column["name"] not in before and column["notnull"] and (default is None or str(default).strip(" ()\t\r\n").upper() == "NULL"):
                                from .errors import HumanError

                                raise HumanError("migration_compat", "Новая колонка несовместима с прежними INSERT", "Обязательному полю нужен ненулевой DEFAULT.")
                db.execute("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (f"schema:{part}", str(version)))

    # meta

    def meta_get(self, key: str, default=None):
        row = self.one("SELECT value FROM meta WHERE key = ?", (key,))
        return json.loads(row["value"]) if row else default

    def meta_set(self, key: str, value) -> None:
        self.x("INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, dumps(value)))

    # сессии и сообщения

    def new_session(self, kind: str, title: str = "", meta: dict | None = None, sid: str | None = None) -> str:
        sid = sid or new_id()
        now = time.time()
        self.x("INSERT OR IGNORE INTO sessions(id, kind, title, created_at, updated_at, meta) VALUES(?,?,?,?,?,?)", (sid, kind, title, now, now, dumps(meta or {})))
        return sid

    def session(self, sid: str) -> dict | None:
        row = self.one("SELECT * FROM sessions WHERE id = ?", (sid,))
        if not row:
            return None
        d = dict(row)
        d["meta"] = json.loads(d["meta"])
        return d

    def sessions(self, kind: str | None = None, limit: int = 100) -> list[dict]:
        rows = self.q("SELECT * FROM sessions" + (" WHERE kind = ?" if kind else "") + " ORDER BY updated_at DESC LIMIT ?", ((kind, limit) if kind else (limit,)))
        out = []
        for r in rows:
            d = dict(r)
            d["meta"] = json.loads(d["meta"])
            out.append(d)
        return out

    def update_session(self, sid: str, *, title: str | None = None, meta: dict | None = None) -> None:
        with self.tx() as db:
            if title is not None:
                db.execute("UPDATE sessions SET title = ? WHERE id = ?", (title, sid))
            if meta is not None:
                row = db.execute("SELECT meta FROM sessions WHERE id = ?", (sid,)).fetchone()
                cur = json.loads(row["meta"]) if row else {}
                cur.update(meta)
                db.execute("UPDATE sessions SET meta = ?, updated_at = ? WHERE id = ?", (dumps(cur), time.time(), sid))

    def add_message(self, sid: str, msg: dict, meta: dict | None = None) -> str:
        mid = new_id()
        now = time.time()
        with self.tx() as db:
            if msg.get('role') == 'user':
                from .dates import prefix
                previous = db.execute("SELECT created_at FROM messages WHERE session_id=? AND role='user' ORDER BY id DESC LIMIT 1", (sid,)).fetchone()
                meta = {**(meta or {}), 'time_prefix': prefix(now, previous['created_at'] if previous else None)}
            db.execute("INSERT INTO messages(id, session_id, role, data, created_at, meta) VALUES(?,?,?,?,?,?)", (mid, sid, msg.get("role", ""), dumps(msg), now, dumps(meta or {})))
            db.execute("UPDATE sessions SET updated_at = ? WHERE id = ?", (now, sid))
        return mid

    def import_messages(self, sid: str, rows: list[tuple[str, dict, float, dict]]) -> int:
        """Вставить готовые сообщения пачкой: (id, сообщение, время, meta). Для переезда из других систем."""
        if not rows:
            return 0
        with self.tx() as db:
            db.executemany("INSERT OR IGNORE INTO messages(id, session_id, role, data, created_at, meta) VALUES(?,?,?,?,?,?)",
                           [(mid, sid, m.get("role", ""), dumps(m), ts, dumps(meta)) for mid, m, ts, meta in rows])
            db.execute("UPDATE sessions SET updated_at = MAX(updated_at, ?) WHERE id = ?", (max(r[2] for r in rows), sid))
        return len(rows)

    def message_count(self, sid: str, *, exclude_meta_key: str | None = None) -> int:
        if exclude_meta_key:
            row = self.one("SELECT COUNT(*) AS n FROM messages WHERE session_id = ? AND json_extract(meta, ?) IS NULL", (sid, f"$.{exclude_meta_key}"))
        else:
            row = self.one("SELECT COUNT(*) AS n FROM messages WHERE session_id = ?", (sid,))
        return int(row["n"]) if row else 0

    def prune_migration_copies(self) -> int:
        """Копия базы перед миграцией нужна для отката этого обновления; старые — нет, а каждая весит как вся база."""
        folder = self.path.parent / "local" / "migration-backups"
        if folder.is_symlink() or not folder.is_dir():
            return 0
        stale = sorted(p for p in folder.glob("*.db") if p.is_file() and not p.is_symlink())[:-MIGRATION_COPIES]
        for path in stale:
            path.unlink(missing_ok=True)
        return len(stale)

    def backup_to(self, path: Path | str) -> None:
        """Согласованная копия базы (sqlite backup) — перед переездом и обновлениями."""
        # Своё соединение: WAL даёт согласованный снимок, а общий замок не держится всё копирование —
        # иначе на большой базе замирает весь Бендер вместе с пультом.
        src = sqlite3.connect(str(self.path), timeout=30)
        dst = sqlite3.connect(str(path))
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()

    def messages(self, sid: str, limit: int | None = None) -> list[dict]:
        if limit:
            rows = self.q("SELECT * FROM (SELECT * FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?) ORDER BY id", (sid, limit))
        else:
            rows = self.q("SELECT * FROM messages WHERE session_id = ? ORDER BY id", (sid,))
        out = []
        for r in rows:
            out.append({"id": r["id"], "created_at": r["created_at"], "meta": json.loads(r["meta"]), **json.loads(r["data"])})
        return out

    # вызовы моделей

    def add_call(self, *, session_id: str | None, role: str, provider: str, model: str, usage: dict, cost: float, seconds: float,
                 notice: str | None, request: dict | None, error: dict | None = None) -> str:
        cid = new_id()
        self.x(
            "INSERT INTO calls(id, session_id, created_at, role, provider, model, usage, cost, seconds, notice, request, error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (cid, session_id, time.time(), role, provider, model, dumps(usage), cost, seconds, notice, pack(request) if request else None, dumps(error) if error else None),
        )
        return cid

    def call(self, cid: str) -> dict | None:
        row = self.one("SELECT * FROM calls WHERE id = ?", (cid,))
        if not row:
            return None
        d = dict(row)
        d["usage"] = json.loads(d["usage"] or "{}")
        d["request"] = unpack(d["request"])
        d["error"] = json.loads(d["error"]) if d["error"] else None
        return d

    def cost_since(self, ts: float) -> float:
        row = self.one("SELECT COALESCE(SUM(cost), 0) AS c FROM calls WHERE created_at >= ?", (ts,))
        return float(row["c"]) if row else 0.0

    def prune_call_requests(self, keep_days: float = 7.0) -> int:
        """Снимки запросов старше срока убираются (сами записи о вызовах и деньги остаются) — у всех агентов стаи:
        у каждого своя таблица agent_<id>__calls в том же файле."""
        cutoff = time.time() - keep_days * 86400
        total = 0
        with self._locked():
            tables = [r[0] for r in self.db.execute("SELECT name FROM sqlite_master WHERE type='table' AND (name = 'calls' OR name LIKE 'agent\\_%\\_\\_calls' ESCAPE '\\')").fetchall()]
            for table in tables:
                if not self.db.execute(f'SELECT 1 FROM "{table}" WHERE request IS NOT NULL AND created_at < ? LIMIT 1', (cutoff,)).fetchone():
                    continue  # нечего чистить — ничего и не пишем
                total += self.db.execute(f'UPDATE "{table}" SET request = NULL WHERE request IS NOT NULL AND created_at < ?', (cutoff,)).rowcount
        return total
