"""Задачи по времени: таблица `jobs` и журнал запусков `job_runs`.

Задача = когда (разово / каждые N / cron) + что (напомнить хозяину / ход агента / рефлекс без модели) +
условия (окно активности, только в простое) + полномочия (что можно делать без вопроса, когда хозяина
рядом нет). Время следующего запуска хранится в `next_run` — бегун спит до ближайшего.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from ..core.errors import HumanError
from ..core.ids import new_id
from . import cron as cronmod
from . import when as whenmod

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
        schedule TEXT NOT NULL, action TEXT NOT NULL, window TEXT NOT NULL DEFAULT '', idle_only INTEGER NOT NULL DEFAULT 0,
        authority TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, created_by TEXT NOT NULL DEFAULT 'owner',
        source TEXT NOT NULL DEFAULT '', next_run REAL, last_run REAL, last_status TEXT, last_error TEXT,
        runs INTEGER NOT NULL DEFAULT 0, max_runs INTEGER, expires REAL, meta TEXT NOT NULL DEFAULT '{}')""",
    """CREATE INDEX IF NOT EXISTS jobs_next ON jobs(enabled, next_run)""",
    """CREATE TABLE IF NOT EXISTS job_runs (
        id INTEGER PRIMARY KEY, job_id TEXT NOT NULL, started REAL NOT NULL, finished REAL, status TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '', session_id TEXT, late REAL NOT NULL DEFAULT 0)""",
    """CREATE INDEX IF NOT EXISTS job_runs_job ON job_runs(job_id, started)""",
]

ACTIONS = ("remind", "agent", "reflex")
LATE_GRACE = {"remind": 7 * 86400, "agent": 6 * 3600, "reflex": 3600}  # пропущенное позже — не догоняем


@dataclass
class Job:
    id: str
    name: str
    schedule: dict
    action: dict
    enabled: bool = True
    window: str = ""
    idle_only: bool = False
    authority: list = field(default_factory=list)
    created_at: float = 0.0
    created_by: str = "owner"
    source: str = ""
    next_run: float | None = None
    last_run: float | None = None
    last_status: str | None = None
    last_error: str | None = None
    runs: int = 0
    max_runs: int | None = None
    expires: float | None = None
    meta: dict = field(default_factory=dict)

    @property
    def kind(self) -> str:
        return self.action.get("type", "agent")

    def describe_schedule(self) -> str:
        return describe(self.schedule)

    def public(self) -> dict:
        d = {k: getattr(self, k) for k in ("id", "name", "schedule", "action", "enabled", "window", "idle_only", "authority", "created_at",
                                            "created_by", "source", "next_run", "last_run", "last_status", "last_error", "runs", "max_runs", "expires", "meta")}
        d["kind"] = self.kind
        d["when"] = self.describe_schedule()
        d["next_human"] = whenmod.human(self.next_run) if self.next_run else None
        return d


def describe(s: dict) -> str:
    k = s.get("kind")
    if k == "at":
        return "разово, " + whenmod.human(s["at"]) if s.get("at", 0) > time.time() else "разово"
    if k == "every":
        sec = int(s.get("every") or 0)
        for unit, n, words in ((86400, "д", None), (3600, "ч", None), (60, "мин", None)):
            if sec % unit == 0:
                return f"каждые {sec // unit} {n}"
        return f"каждые {sec} с"
    if k == "cron":
        try:
            return cronmod.parse(s.get("expr", "")).describe()
        except HumanError:
            return f"cron «{s.get('expr')}»"
    return "?"


def make_schedule(*, at: str | float | None = None, every: str | int | None = None, cron: str | None = None, now: float | None = None) -> dict:
    now = now or time.time()
    given = [x for x in (at, every, cron) if x not in (None, "")]
    if len(given) != 1:
        raise HumanError("bad_schedule", "Когда запускать?", "Нужно ровно одно: at (разово), every (интервал) или cron.")
    if at not in (None, ""):
        ts = float(at) if isinstance(at, (int, float)) else whenmod.parse(str(at), now)
        return {"kind": "at", "at": ts}
    if every not in (None, ""):
        sec = int(every) if isinstance(every, (int, float)) else whenmod.every(str(every))
        if sec < 60:
            raise HumanError("bad_every", "Слишком часто", "Чаще раза в минуту задачи не запускаются.")
        return {"kind": "every", "every": sec, "anchor": now}
    cronmod.parse(str(cron))  # проверить сразу
    return {"kind": "cron", "expr": str(cron).strip()}


def next_fire(schedule: dict, after: float, *, window: str = "") -> float | None:
    """Следующее срабатывание после after с учётом окна активности."""
    k = schedule.get("kind")
    t: float | None
    if k == "at":
        t = schedule["at"] if schedule["at"] > after else None
    elif k == "every":
        sec = max(60, int(schedule["every"]))
        anchor = float(schedule.get("anchor") or after)
        n = int((after - anchor) // sec) + 1 if after >= anchor else 0
        t = anchor + n * sec
        if t <= after:
            t += sec
    elif k == "cron":
        t = cronmod.parse(schedule["expr"]).next_after(after)
    else:
        return None
    if t is None or not window:
        return t
    for _ in range(400):  # окно: для cron — следующее подходящее, для остального — начало окна
        if whenmod.window_ok(window, t):
            return t
        if k == "cron":
            t = cronmod.parse(schedule["expr"]).next_after(t)
            if t is None:
                return None
        else:
            return whenmod.next_window_start(window, t)
    return None


class Jobs:
    def __init__(self, store):
        self.store = store
        store.migrate("jobs", 1, SCHEMA)

    def _job(self, r) -> Job:
        return Job(id=r["id"], name=r["name"], schedule=json.loads(r["schedule"]), action=json.loads(r["action"]), enabled=bool(r["enabled"]),
                   window=r["window"], idle_only=bool(r["idle_only"]), authority=json.loads(r["authority"] or "[]"), created_at=r["created_at"],
                   created_by=r["created_by"], source=r["source"], next_run=r["next_run"], last_run=r["last_run"], last_status=r["last_status"],
                   last_error=r["last_error"], runs=r["runs"], max_runs=r["max_runs"], expires=r["expires"], meta=json.loads(r["meta"] or "{}"))

    def get(self, jid: str) -> Job | None:
        r = self.store.one("SELECT * FROM jobs WHERE id = ?", (jid,))
        return self._job(r) if r else None

    def find(self, key: str) -> Job | None:
        """По id или имени (без учёта регистра)."""
        j = self.get(key)
        if j:
            return j
        for r in self.store.q("SELECT * FROM jobs ORDER BY created_at DESC"):
            if r["name"].strip().lower() == (key or "").strip().lower():
                return self._job(r)
        return None

    def all(self, *, enabled: bool | None = None) -> list[Job]:
        sql = "SELECT * FROM jobs" + ("" if enabled is None else f" WHERE enabled = {1 if enabled else 0}") + " ORDER BY COALESCE(next_run, 9e18), created_at"
        return [self._job(r) for r in self.store.q(sql)]

    def add(self, name: str, schedule: dict, action: dict, *, window: str = "", idle_only: bool = False, authority: list | None = None,
            created_by: str = "owner", source: str = "", enabled: bool = True, max_runs: int | None = None, expires: float | None = None,
            meta: dict | None = None, now: float | None = None) -> Job:
        now = now or time.time()
        if action.get("type") not in ACTIONS:
            raise HumanError("bad_job", "Не знаю, что делать по расписанию", "Действие: remind (напомнить), agent (ход агента) или reflex (проверка без модели).")
        if window:
            whenmod.window_ok(window, now)  # проверить формат
        if schedule.get("kind") == "at" and max_runs is None:
            max_runs = 1
        nxt = next_fire(schedule, now - 1 if schedule.get("kind") == "at" else now, window=window)
        if nxt is None and enabled:
            raise HumanError("bad_schedule", "Такое время никогда не наступит", describe(schedule))
        job = Job(id=new_id(), name=(name or "").strip()[:120] or (action.get("text") or action.get("prompt") or "задача")[:60], schedule=schedule,
                  action=action, enabled=enabled, window=window, idle_only=idle_only, authority=list(authority or []), created_at=now,
                  created_by=created_by, source=source, next_run=nxt if enabled else None, max_runs=max_runs, expires=expires, meta=dict(meta or {}))
        self.store.x("""INSERT INTO jobs(id, name, enabled, schedule, action, window, idle_only, authority, created_at, created_by, source, next_run,
                        max_runs, expires, meta) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (job.id, job.name, int(enabled), json.dumps(schedule, ensure_ascii=False), json.dumps(action, ensure_ascii=False), window, int(idle_only),
                      json.dumps(job.authority, ensure_ascii=False), now, created_by, source, job.next_run, max_runs, expires, json.dumps(job.meta, ensure_ascii=False)))
        return job

    def update(self, jid: str, **fields) -> Job | None:
        job = self.get(jid)
        if not job:
            return None
        for k, v in fields.items():
            setattr(job, k, v)
        if {"schedule", "window", "enabled"} & set(fields):
            job.next_run = next_fire(job.schedule, time.time() - (1 if job.schedule.get("kind") == "at" else 0), window=job.window) if job.enabled else None
        self.store.x("""UPDATE jobs SET name=?, enabled=?, schedule=?, action=?, window=?, idle_only=?, authority=?, next_run=?, max_runs=?, expires=?, meta=?
                        WHERE id=?""", (job.name, int(job.enabled), json.dumps(job.schedule, ensure_ascii=False), json.dumps(job.action, ensure_ascii=False),
                                        job.window, int(job.idle_only), json.dumps(job.authority, ensure_ascii=False), job.next_run, job.max_runs,
                                        job.expires, json.dumps(job.meta, ensure_ascii=False), jid))
        return job

    def remove(self, jid: str) -> bool:
        with self.store.tx() as db:
            n = db.execute("DELETE FROM jobs WHERE id = ?", (jid,)).rowcount
            db.execute("DELETE FROM job_runs WHERE job_id = ?", (jid,))
        return bool(n)

    def due(self, now: float) -> list[Job]:
        return [self._job(r) for r in self.store.q("SELECT * FROM jobs WHERE enabled = 1 AND next_run IS NOT NULL AND next_run <= ? ORDER BY next_run", (now,))]

    def next_due(self) -> float | None:
        r = self.store.one("SELECT MIN(next_run) AS t FROM jobs WHERE enabled = 1 AND next_run IS NOT NULL")
        return r["t"] if r and r["t"] is not None else None

    def postpone(self, job: Job, until: float) -> None:
        self.store.x("UPDATE jobs SET next_run = ? WHERE id = ?", (until, job.id))

    def finish(self, job: Job, *, started: float, status: str, summary: str = "", error: str | None = None, session_id: str | None = None,
               late: float = 0.0, now: float | None = None) -> Job:
        """Записать запуск и посчитать следующий. Разовые и исчерпанные — выключаются."""
        now = now or time.time()
        runs = job.runs + (1 if status in ("ok", "error") else 0)
        nxt = next_fire(job.schedule, max(now, job.next_run or now), window=job.window)
        enabled = job.enabled
        if nxt is None or (job.max_runs and runs >= job.max_runs) or (job.expires and nxt > job.expires):
            nxt, enabled = None, False
        with self.store.tx() as db:
            db.execute("INSERT INTO job_runs(job_id, started, finished, status, summary, session_id, late) VALUES(?,?,?,?,?,?,?)",
                       (job.id, started, now, status, summary[:2000], session_id, late))
            db.execute("UPDATE jobs SET last_run=?, last_status=?, last_error=?, runs=?, next_run=?, enabled=? WHERE id=?",
                       (started, status, error, runs, nxt, int(enabled), job.id))
            # журнал запусков не растёт бесконечно: последние 50 на задачу
            db.execute("DELETE FROM job_runs WHERE job_id = ? AND id NOT IN (SELECT id FROM job_runs WHERE job_id = ? ORDER BY id DESC LIMIT 50)", (job.id, job.id))
        job.runs, job.next_run, job.enabled, job.last_status, job.last_run = runs, nxt, enabled, status, started
        return job

    def history(self, jid: str, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self.store.q("SELECT * FROM job_runs WHERE job_id = ? ORDER BY id DESC LIMIT ?", (jid, limit))]
