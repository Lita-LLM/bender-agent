"""Вебхуки: чужие программы стучатся к Бендеру (`POST /hook/<id>`) — GitHub, мониторинг, умный дом, IFTTT, свои скрипты.

Вход — секретом вебхука: заголовок `Authorization: Bearer <секрет>` (или `X-Bender-Token`), параметр `?token=`
(для служб, которые умеют только адрес), или подпись тела HMAC-SHA256 (`X-Hub-Signature-256: sha256=…` как у GitHub,
`X-Gitea-Signature`, `X-Signature-256`). Имя сервера, кука пульта и CSRF для `/hook/` не проверяются: без секрета
запрос ничего не сделает, а службы снаружи приходят через туннель со своим именем.

Что делать с запросом, решает хозяин при создании:
- **notify** — строка хозяину («🪝 имя: …»), без модели;
- **agent** — ход агента: поручение хозяина + содержимое запроса как НЕДОВЕРЕННОЕ (ход заражён, хозяина рядом нет —
  рискованное Предохранитель отклонит).
Лимит запросов в час — от лавины (и от счёта за модель).

Адресат — агент стаи из настройки вебхука (поле `agent`, по умолчанию main). Заголовок `X-Bender-Agent` и `?agent=`
здесь не действуют: адресата выбирает хозяин, а не тот, кто стучится.

Ход агента идёт через постоянную очередь `hook_events`: запрос сначала записывается в базу, потом отвечает 202 с номером
события. Ход начинается, когда главный чат агента свободен (в чужой идущий ход событие не вклинивается). Состояния:
queued (принято) → waiting (агент занят) → running (передано в ход) → done | failed; running при перезапуске
становится interrupted и само не повторяется — ход мог успеть что-то сделать, повторяет хозяин. Повтор того же
`Idempotency-Key` (или `X-Bender-Event-Id`, `X-GitHub-Delivery`) возвращает прежнее событие, второго хода нет.
Судьба события: `GET /hook/<id>/events/<номер>` тем же секретом.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import json
import secrets as pysecrets
import asyncio
import time
from collections import deque

from ..core.errors import HumanError
from ..core.ids import new_id

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS hooks (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, action TEXT NOT NULL DEFAULT 'notify', instruction TEXT NOT NULL DEFAULT '',
        enabled INTEGER NOT NULL DEFAULT 1, per_hour INTEGER NOT NULL DEFAULT 30, count INTEGER NOT NULL DEFAULT 0,
        last_at REAL, last_status TEXT, created REAL NOT NULL)""",
    "ALTER TABLE hooks ADD COLUMN agent TEXT NOT NULL DEFAULT 'main'",
    """CREATE TABLE IF NOT EXISTS hook_events (
        id TEXT PRIMARY KEY, hook TEXT NOT NULL, agent TEXT NOT NULL, event_key TEXT, text TEXT NOT NULL,
        received REAL NOT NULL, state TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0, error TEXT,
        started REAL, finished REAL)""",
    "CREATE UNIQUE INDEX IF NOT EXISTS hook_events_key ON hook_events(hook, event_key) WHERE event_key IS NOT NULL",
    "CREATE INDEX IF NOT EXISTS hook_events_state ON hook_events(agent, state, received)",
]
EVENT_HEADERS = ("idempotency-key", "x-bender-event-id", "x-github-delivery", "x-gitea-delivery")
OPEN_STATES = ("queued", "waiting")
ACTIONS = {"notify": "сказать хозяину", "agent": "ход агента"}
MAX_PAYLOAD = 256 * 1024
TEXT_KEYS = ("text", "message", "title", "summary", "description", "body", "status", "event")


def _secret_name(hid: str) -> str:
    return f"hook:{hid}"


def brief(payload, limit: int = 300) -> str:
    """Короткая строка из запроса для уведомления: знакомое поле JSON или начало текста."""
    if isinstance(payload, dict):
        for k in TEXT_KEYS:
            v = payload.get(k)
            if isinstance(v, (str, int, float)) and str(v).strip():
                return str(v).strip()[:limit]
        return json.dumps(payload, ensure_ascii=False)[:limit]
    return str(payload or "").strip()[:limit] or "(пустой запрос)"


class Hooks:
    def __init__(self, app):
        self.app = app
        app.store.migrate("hooks", 2, SCHEMA)
        self._recent: dict[str, deque] = {}
        self._workers: dict[str, asyncio.Task] = {}
        self.closing = False

    # хранение

    def get(self, hid: str) -> dict | None:
        r = self.app.store.one("SELECT * FROM hooks WHERE id = ?", (hid,))
        return dict(r) if r else None

    def all(self) -> list[dict]:
        return [dict(r) for r in self.app.store.q("SELECT * FROM hooks ORDER BY created")]

    def secret(self, hid: str) -> str | None:
        return self.app.secrets.get(_secret_name(hid))

    def public(self, h: dict) -> dict:
        return {**h, "agent": h.get("agent") or "main", "enabled": bool(h["enabled"]), "path": f"/hook/{h['id']}", "secret": self.secret(h["id"]), "action_title": ACTIONS.get(h["action"], h["action"])}

    def _target(self, agent: str) -> str:
        """Адресат — существующий агент не в архиве (и при включённой стае)."""
        agent = str(agent or "main").strip() or "main"
        self.app.agents.get(agent)
        if self.app.agents.record(agent)["temporary"]:
            raise HumanError("hook_agent", "Временному агенту вебхук не назначить", "Выбери постоянного агента стаи.")
        return agent

    def _check(self, action: str, name: str, instruction: str) -> None:
        if action not in ACTIONS:
            raise HumanError("hook_action", f"Не знаю действия «{action}»", "Бывает: " + ", ".join(f"{k} ({v})" for k, v in ACTIONS.items()))
        if not name.strip():
            raise HumanError("hook_name", "Нужно имя вебхука", "Например «GitHub» или «датчик в ванной».")
        if action == "agent" and not instruction.strip():
            raise HumanError("hook_instruction", "Нужно поручение агенту", "Что сделать, когда придёт запрос? Например: «если сборка упала — разберись и скажи мне».")

    def create(self, name: str, action: str = "notify", instruction: str = "", per_hour: int = 30, agent: str = "main") -> dict:
        self._check(action, name, instruction)
        agent = self._target(agent)
        hid = new_id().lower()[-12:]
        self.app.store.x("INSERT INTO hooks(id, name, action, instruction, per_hour, created, agent) VALUES(?,?,?,?,?,?,?)",
                         (hid, name.strip()[:80], action, instruction.strip()[:4000], max(1, min(int(per_hour or 30), 3600)), time.time(), agent))
        self.app.secrets.set(_secret_name(hid), pysecrets.token_urlsafe(24), note=f"вебхук «{name.strip()[:40]}»")
        self.app.journal.write("channel", f"Вебхук «{name.strip()[:80]}» создан ({ACTIONS[action]})", actor="owner", data={"hook": hid})
        return self.get(hid)

    def update(self, hid: str, changes: dict) -> dict:
        h = self.get(hid)
        if not h:
            raise HumanError("no_hook", "Нет такого вебхука", "", status=404)
        new = {**h, **{k: v for k, v in changes.items() if k in ("name", "action", "instruction", "enabled", "per_hour", "agent")}}
        self._check(new["action"], str(new["name"]), str(new["instruction"]))
        agent = self._target(new["agent"]) if "agent" in changes else (h.get("agent") or "main")
        self.app.store.x("UPDATE hooks SET name = ?, action = ?, instruction = ?, enabled = ?, per_hour = ?, agent = ? WHERE id = ?",
                         (str(new["name"]).strip()[:80], new["action"], str(new["instruction"]).strip()[:4000], int(bool(new["enabled"])),
                          max(1, min(int(new["per_hour"] or 30), 3600)), agent, hid))
        if agent != (h.get("agent") or "main"):
            self.app.journal.write("channel", f"Вебхук «{h['name']}»: адресат — агент {agent}", actor="owner", data={"hook": hid, "agent": agent})
        return self.get(hid)

    def new_secret(self, hid: str) -> str:
        if not self.get(hid):
            raise HumanError("no_hook", "Нет такого вебхука", "", status=404)
        s = pysecrets.token_urlsafe(24)
        self.app.secrets.set(_secret_name(hid), s)
        self.app.journal.write("channel", "Вебхук: новый секрет (старый больше не действует)", actor="owner", data={"hook": hid})
        return s

    def remove(self, hid: str) -> None:
        self.app.store.x("DELETE FROM hooks WHERE id = ?", (hid,))
        self.app.secrets.delete(_secret_name(hid))
        self._recent.pop(hid, None)

    # приём

    def authorized(self, h: dict, req) -> bool:
        secret = self.secret(h["id"]) or ""
        if not secret:
            return False
        auth = req.headers.get("authorization") or ""
        given = auth[7:].strip() if auth.lower().startswith("bearer ") else (req.headers.get("x-bender-token") or req.q("token") or "")
        if given and hmac.compare_digest(given.encode(), secret.encode()):
            return True
        mac = hmac.new(secret.encode(), req.body or b"", hashlib.sha256).hexdigest()
        for header in ("x-hub-signature-256", "x-signature-256", "x-gitea-signature", "x-gogs-signature"):
            sig = (req.headers.get(header) or "").strip()
            if sig and hmac.compare_digest(sig.split("=", 1)[-1].lower().encode(), mac.encode()):
                return True
        return False

    def _limited(self, h: dict) -> bool:
        now = time.time()
        if h["action"] == "agent":  # счёт по базе: перезапуск лимит не сбрасывает
            row = self.app.store.one("SELECT COUNT(*) AS n FROM hook_events WHERE hook = ? AND received > ?", (h["id"], now - 3600))
            return row["n"] >= int(h["per_hour"] or 30)
        q = self._recent.setdefault(h["id"], deque())
        while q and q[0] < now - 3600:
            q.popleft()
        if len(q) >= int(h["per_hour"] or 30):
            return True
        q.append(now)
        return False

    async def receive(self, hid: str, req) -> tuple[int, dict]:
        """Обработать запрос. Возвращает (код ответа, тело). Ход агента идёт в фоне — служба ответа не ждёт."""
        app = self.app
        h = self.get(hid)
        if not h or not self.authorized(h, req):  # нет вебхука и нет секрета — один ответ, чтобы не угадывали номера
            return 404, {"ok": False, "error": "нет такого вебхука или неверный секрет"}
        if not h["enabled"]:
            return 403, {"ok": False, "error": "вебхук выключен"}
        if app.settings.group("module_channels") and not app.modules.channel_enabled(hid):
            return 403, {"ok": False, "error": "модуль канала выключен или изменён"}
        if app.safe:
            return 503, {"ok": False, "error": "Бендер в безопасном режиме"}
        if len(req.body or b"") > MAX_PAYLOAD:
            return 413, {"ok": False, "error": "слишком большой запрос"}
        key = next((str(req.headers.get(name) or "").strip()[:200] for name in EVENT_HEADERS if str(req.headers.get(name) or "").strip()), "") or None
        if key and h["action"] == "agent":
            old = app.store.one("SELECT * FROM hook_events WHERE hook = ? AND event_key = ?", (hid, key))
            if old:  # повтор доставки: прежнее событие, второго хода нет
                return 200, {"ok": True, "duplicate": True, **self._event_public(dict(old))}
        if self._limited(h):
            self._status(h, "лимит")
            return 429, {"ok": False, "error": f"не больше {h['per_hour']} запросов в час"}
        raw = (req.body or b"").decode("utf-8", "replace")
        try:
            payload = json.loads(raw) if raw.strip()[:1] in ("{", "[") else raw
        except ValueError:
            payload = raw
        if not raw.strip() and req.query:
            payload = {k: v[0] if len(v) == 1 else v for k, v in req.query.items() if k != "token"}
        event = req.headers.get("x-github-event") or req.headers.get("x-gitea-event") or req.headers.get("x-event") or ""
        event = re.sub(r"[^A-Za-z0-9_.:-]", "", event)[:64]  # стоит вне обёртки, рядом с поручением хозяина
        try:
            target = app.agents.get(h.get("agent") or "main")
        except HumanError as e:  # агент в архиве, стая выключена, агента нет — вход его не обходит
            self._status(h, "агент недоступен")
            return 409, {"ok": False, "error": f"агент-адресат недоступен: {e.title}"}
        sid = target.main_session()
        if h["action"] == "notify":
            text = f"🪝 {h['name']}" + (f" ({event})" if event else "") + f": {brief(payload)}"
            msg = {"role": "event", "kind": "hook", "content": text}  # событие: модели не уходит
            app.store.add_message(sid, msg, {"hook": hid})
            app.bus.publish({"session": sid, "type": "message", "message": msg})
            app.bus.publish({"type": "notify", "kind": "hook", "title": h["name"], "text": text})
            self._status(h, "ok")
            app.journal.write("channel", f"Вебхук «{h['name']}»: запрос" + (f" ({event})" if event else ""), actor="hook", data={"hook": hid, "bytes": len(req.body or b"")})
            return 202, {"ok": True}
        else:
            from ..tools.web import untrusted

            body = json.dumps(payload, ensure_ascii=False, indent=1) if not isinstance(payload, str) else payload
            text = (f"[Вебхук «{h['name']}»{f', событие {event}' if event else ''}]\nПоручение хозяина: {h['instruction']}\n\n"
                    + untrusted(f"вебхук «{h['name']}»", body[:20000]))
            eid = new_id().lower()
            app.store.x("INSERT OR IGNORE INTO hook_events(id, hook, agent, event_key, text, received) VALUES(?,?,?,?,?,?)",
                        (eid, hid, target.agent_id, key, text, time.time()))
            row = dict(app.store.one("SELECT * FROM hook_events WHERE id = ?", (eid,)) or
                       app.store.one("SELECT * FROM hook_events WHERE hook = ? AND event_key = ?", (hid, key)))
            if row["id"] != eid:
                return 200, {"ok": True, "duplicate": True, **self._event_public(row)}
        self._status(h, "ok")
        app.journal.write("channel", f"Вебхук «{h['name']}»: запрос" + (f" ({event})" if event else ""), actor="hook", data={"hook": hid, "bytes": len(req.body or b""), "event": eid, "agent": target.agent_id})
        self.kick(target.agent_id)
        return 202, {"ok": True, "duplicate": False, **self._event_public(row)}

    def _status(self, h: dict, status: str) -> None:
        self.app.store.x("UPDATE hooks SET count = count + 1, last_at = ?, last_status = ? WHERE id = ?", (time.time(), status, h["id"]))
        self.app.bus.publish({"type": "channel", "channel": "hooks"})

    # очередь событий: записано в базу до ответа 202, ход — когда главный чат агента свободен

    def _event_public(self, row: dict) -> dict:
        return {"event": row["id"], "state": row["state"], "attempts": row["attempts"], "error": row["error"],
                "received": row["received"], "started": row["started"], "finished": row["finished"],
                "status_path": f"/hook/{row['hook']}/events/{row['id']}"}

    def event_status(self, hid: str, eid: str, req) -> tuple[int, dict]:
        h = self.get(hid)
        if not h or not self.authorized(h, req):
            return 404, {"ok": False, "error": "нет такого вебхука или неверный секрет"}
        row = self.app.store.one("SELECT * FROM hook_events WHERE id = ? AND hook = ?", (eid, hid))
        if not row:
            return 404, {"ok": False, "error": "нет такого события"}
        return 200, {"ok": True, **self._event_public(dict(row))}

    def events(self, hid: str, limit: int = 50) -> list[dict]:
        rows = self.app.store.q("SELECT * FROM hook_events WHERE hook = ? ORDER BY received DESC LIMIT ?", (hid, max(1, min(int(limit), 500))))
        return [{**self._event_public(dict(r)), "agent": r["agent"], "event_key": r["event_key"]} for r in rows]

    def retry(self, eid: str) -> dict:
        """Хозяин повторяет упавшее или прерванное событие. Само ядро прерванное не повторяет."""
        row = self.app.store.one("SELECT * FROM hook_events WHERE id = ?", (eid,))
        if not row:
            raise HumanError("no_hook_event", "Нет такого события", "", status=404)
        if row["state"] not in ("failed", "interrupted"):
            raise HumanError("hook_event_state", "Это событие не нужно повторять", f"Оно сейчас в состоянии {row['state']}.", status=409)
        self.app.store.x("UPDATE hook_events SET state = 'queued', error = NULL, finished = NULL WHERE id = ?", (eid,))
        self.app.journal.write("channel", "Вебхук: событие поставлено в очередь повторно", actor="owner", data={"event": eid})
        self.kick(row["agent"])
        return self._event_public(dict(self.app.store.one("SELECT * FROM hook_events WHERE id = ?", (eid,))))

    def start(self) -> int:
        """После перезапуска: running → interrupted (без повтора), принятые — снова в работу."""
        now = time.time()
        stuck = self.app.store.q("SELECT * FROM hook_events WHERE state = 'running'")
        for row in stuck:
            self._set(row["id"], "interrupted", error="Ход прерван перезапуском; действие могло успеть выполниться. Повторить — вручную.", finished=now)
            try:
                ctx = self.app.agents.get(row["agent"])
                ctx.mind._save_event(ctx.main_session(), "interrupted", "🪝 Событие вебхука прервано перезапуском и само не повторится. Проверь результат; повторить можно в «Каналах».", {"hook_event": row["id"]})
            except HumanError:
                pass
        if stuck:
            self.app.journal.write("channel", "Вебхук: события, прерванные перезапуском", data={"count": len(stuck)})
        for row in self.app.store.q("SELECT DISTINCT agent FROM hook_events WHERE state IN ('queued','waiting')"):
            self.kick(row["agent"])
        return len(stuck)

    def kick(self, agent: str) -> None:
        if self.closing:
            return
        task = self._workers.get(agent)
        if task and not task.done():
            return
        self._workers[agent] = asyncio.ensure_future(self._drain(agent))

    async def close(self) -> None:
        """События в работе остаются running — при следующем запуске они станут interrupted."""
        self.closing = True
        tasks = [t for t in self._workers.values() if not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def _set(self, eid: str, state: str, **fields) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.app.store.x(f"UPDATE hook_events SET state = ?{', ' + cols if cols else ''} WHERE id = ?", (state, *fields.values(), eid))
        self.app.bus.publish({"type": "channel", "channel": "hooks"})

    async def _drain(self, agent: str) -> None:
        store = self.app.store
        while not self.closing:
            row = store.one("SELECT * FROM hook_events WHERE agent = ? AND state IN ('queued','waiting') ORDER BY received, rowid LIMIT 1", (agent,))
            if not row:
                return
            eid = row["id"]
            try:
                ctx = self.app.agents.get(agent)
            except HumanError as e:  # агента архивировали после приёма — не обходим, отказ виден
                self._set(eid, "failed", error=f"Агент-адресат недоступен: {e.title}", finished=time.time())
                continue
            sid = ctx.main_session()
            while ctx.mind.running(sid):  # в чужой идущий ход не вклиниваемся: ждём конца
                if row["state"] != "waiting":
                    self._set(eid, "waiting")
                    row = {**dict(row), "state": "waiting"}
                await asyncio.wait({ctx.mind.turns[sid].task})
            store.x("UPDATE hook_events SET state = 'running', attempts = attempts + 1, started = ?, error = NULL WHERE id = ?", (time.time(), eid))
            try:
                # между проверкой «свободен» и say нет await: событие начинает свой ход, а не ложится в чужой
                await ctx.mind.say(sid, row["text"], channel="hook", actor="hook", tainted=True, unattended=True, wait=False,
                                   message_meta={"hook": row["hook"], "hook_event": eid})
                turn = ctx.mind.turns.get(sid)
                if turn is None or turn.actor != "hook" or not turn.task:
                    raise RuntimeError("ход не начался")
                await asyncio.wait({turn.task})  # wait, а не await: отмена очереди не отменяет ход
                if turn.task.cancelled():
                    raise RuntimeError("ход отменён")
                error = (turn.task.result() or {}).get("error")
            except HumanError as e:
                if e.code == "closing":  # Бендер выключается: ход не начат, событие ждёт следующего запуска
                    store.x("UPDATE hook_events SET state = 'queued', attempts = attempts - 1 WHERE id = ?", (eid,))
                    return
                error = {"title": e.title}
            except asyncio.CancelledError:
                raise  # выключение: событие остаётся running → interrupted при запуске
            except Exception as e:  # noqa: BLE001
                error = {"title": str(e)[:300] or type(e).__name__}
            if error:
                self._set(eid, "failed", error=str(error.get("title") or error.get("code") or "ошибка")[:500], finished=time.time())
            else:
                self._set(eid, "done", finished=time.time())
