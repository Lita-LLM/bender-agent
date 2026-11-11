"""Сердце: задачи по времени (напоминания, ходы агента, рефлексы), позже — пульс и намерения.

Бегун не опрашивает: спит до ближайшей задачи (но не дольше 5 минут — чтобы заметить перевод часов
и сон машины) и просыпается раньше, если задачи поменялись. В простое без задач — ни записи, ни ЦП.

Ход по расписанию идёт «без хозяина рядом»: всё, что Предохранитель спросил бы, — отказ, кроме того,
что хозяин разрешил этой задаче заранее (полномочия). Пропущенное, пока Бендер был выключен,
догоняется один раз с пометкой «с опозданием», если опоздание разумное; иначе — «пропущено».
"""

from __future__ import annotations

import asyncio
import time

from ..core.errors import HumanError
from . import pulse as pulsemod
from . import when as whenmod
from .intents import Intents
from .jobs import LATE_GRACE, Job, Jobs, describe, make_schedule  # noqa: F401 — реэкспорт

MAX_SLEEP = 300.0
BUSY_RETRY = 60.0
IDLE_RETRY = 600.0
IDLE_MINUTES = 15


def late_words(sec: float) -> str:
    if sec < 90:
        return ""
    if sec < 3600:
        return f"с опозданием на {round(sec / 60)} мин"
    if sec < 86400:
        return f"с опозданием на {sec / 3600:.1f} ч".replace(".0 ", " ")
    return f"с опозданием на {round(sec / 86400)} дн"


class Heart:
    def __init__(self, agent_ctx):
        self.app = agent_ctx
        self.jobs = Jobs(agent_ctx.store)
        self.intents = Intents(agent_ctx.store)
        self.task: asyncio.Task | None = None
        self._stopping=False
        self._wake: asyncio.Event | None = None
        self.firing: str | None = None
        self.started_at: float | None = None
        # рефлекс проверил и всё спокойно — следующий раз держим в памяти, а не в базе: ноль записей в простое
        self._listening = False
        self._quiet: dict[str, float | None] = {}
        self.pulse = pulsemod.PulseState()

    # включено ли

    def enabled(self) -> bool:
        if self.app.agent_id != 'main' and not self.app.host.settings.get('flock', 'enabled', True):
            return False
        return bool(self.app.settings.get("heart", "enabled", True)) and not self.app.safe

    # жизнь

    def start(self) -> None:
        if self.task and not self.task.done():
            return
        self._stopping=False
        self._wake = asyncio.Event()
        self.started_at = time.time()
        try:
            from .adopt import adopt

            adopt(self.app)  # перенесённое из OpenClaw до появления сердца (пишет, только если есть что принять)
        except Exception as e:  # noqa: BLE001
            self.app.journal.write("heart", f"Не удалось принять расписания из OpenClaw: {e}"[:300], actor="heart")
        if not self._listening:
            self.app.bus.listen(self._on_event)
            self._listening = True
        self.task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        self._stopping=True
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self.task = None

    def poke(self) -> None:
        """Задачи поменялись — пересчитать, когда просыпаться."""
        if self._wake is not None:
            self._wake.set()

    def _on_event(self, ev: dict) -> None:
        if ev.get('type') == 'settings' and ev.get('group') in ('updates', 'flock'):
            self.poke()
        if ev.get("type") == "settings" and ev.get("group") == "backups":
            if self.app._backups:
                self.app._backups.next_at = None
            self.poke()
        if ev.get("type") == "settings" and ev.get("group") == "heart":
            self.pulse.next_at = None  # интервал или режим поменялись — пересчитать
            self.poke()
        elif ev.get("type") == "settings" and ev.get("group") == "sleep" and getattr(self.app, "sleep", None):
            self.app.sleep.next_at = None
            self.poke()

    async def _run(self) -> None:
        while not self._stopping:
            try:
                nxt = self.next_due() if self.enabled() else None
                pul = self._pulse_next() if self.enabled() else None
                slp = self._sleep_next() if self.enabled() else None
                bak = self.app.backups.due() if self.app.agent_id == 'main' and self.enabled() and self.app.settings.get("backups", "auto_hours", 0) and self.app.settings.get("backups", "mode", "idle") == "idle" else None
                upd = self.app.updater.due() if self.app.agent_id == 'main' and self.enabled() else None
                soon = min([t for t in (nxt, pul, slp, bak, upd) if t is not None], default=None)
                delay = MAX_SLEEP if soon is None else min(MAX_SLEEP, max(0.0, soon - time.time()))
                if delay > 0:
                    try:
                        await asyncio.wait_for(self._wake.wait(), delay)
                    except asyncio.TimeoutError:
                        pass
                    self._wake.clear()
                    continue
                await self.app.host.agents.calm()  # плановые дела не добавляют работы перегруженному циклу
                for job in self.due(time.time()):
                    if not self.enabled() or self.app.guard.halted:
                        break
                    await self.fire(job)
                if pul is not None and pul <= time.time() and self.enabled() and not self.app.guard.halted:
                    await self.beat()
                if slp is not None and slp <= time.time() and self.enabled() and not self.app.guard.halted:
                    await self.app.sleep.tick()
                if bak is not None and bak <= time.time() and self.enabled():
                    await self.app.backups.tick()
                if upd is not None and upd <= time.time() and self.enabled() and not self.app.guard.halted:
                    await self.app.updater.tick()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 — сердце не останавливается из-за одной задачи
                self.app.journal.write("heart", f"Сбой бегуна задач: {e}"[:300], actor="heart")
                await asyncio.sleep(BUSY_RETRY)
            if self.app.guard.halted:
                await asyncio.sleep(BUSY_RETRY)  # стоп-кран: задачи ждут, пока хозяин не снимет

    # пульс

    def _pulse_next(self) -> float | None:
        if self.app.settings.get("heart", "mode", "two_stage") == "off":
            self.pulse.next_at = None
            return None
        if self.pulse.next_at is None:
            # первый — вскоре после старта; у каждого агента своя доля минуты, чтобы стая не просыпалась разом
            import zlib
            self.pulse.next_at = time.time() + min(120.0, pulsemod.interval(self.app)) + (zlib.crc32(self.app.agent_id.encode()) % 45 if self.app.agent_id != 'main' else 0)
        return self.pulse.next_at

    def _sleep_next(self) -> float | None:
        sl = getattr(self.app, "sleep", None)
        if sl is None or not sl.enabled():
            return None
        if sl.next_at is None:
            sl.next_at = sl.plan_next()
        return sl.next_at

    async def beat(self, *, force: bool = False) -> dict:
        if not force:
            async with self.app.host.agents.background():return await self._beat(force=force)
        return await self._beat(force=force)

    async def _beat(self, *, force: bool = False) -> dict:
        """Удар пульса (по времени или «проверить сейчас»). Идёт разговор с хозяином — подождёт 5 минут."""
        now = time.time()
        if not force and any(self.app.mind.running(sid) for sid in list(self.app.mind.turns)):
            self.pulse.next_at = now + 300
            return {"action": "postponed"}
        try:
            res = await pulsemod.beat(self.app, self.pulse, force=force)
        finally:
            self.pulse.next_at = time.time() + pulsemod.interval(self.app)
        if res.get("model") or res.get("action") not in ("silent", "off"):
            self.app.bus.publish({"type": "pulse", **{k: v for k, v in res.items() if k != "signals"}, "signals": len(res.get("signals") or [])})
        return res

    # когда что

    def _effective(self, job: Job) -> float | None:
        return self._quiet[job.id] if job.id in self._quiet else job.next_run

    def next_due(self) -> float | None:
        ts = [t for t in (self._effective(j) for j in self.jobs.all(enabled=True)) if t is not None]
        return min(ts) if ts else None

    def due(self, now: float) -> list[Job]:
        out = [(t, j) for j in self.jobs.all(enabled=True) for t in [self._effective(j)] if t is not None and t <= now]
        return [j for _, j in sorted(out, key=lambda x: x[0])]

    # простой

    def owner_idle(self, minutes: float = IDLE_MINUTES) -> bool:
        if any(self.app.mind.running(sid) for sid in list(self.app.mind.turns)):
            return False
        r = self.app.store.one("SELECT MAX(created_at) AS t FROM messages WHERE role = 'user' AND json_extract(meta, '$.actor') = 'owner'")
        return not (r and r["t"] and time.time() - r["t"] < minutes * 60)

    # запуск задачи

    async def fire(self, job: Job, *, manual: bool = False) -> dict:
        """Выполнить задачу. manual — хозяин нажал «запустить сейчас»: окна, простой и опоздания не проверяются."""
        agent_ctx = self.app
        now = time.time()
        late = 0.0 if manual else max(0.0, now - (self._effective(job) or now))
        if not manual:
            if late > LATE_GRACE.get(job.kind, 3600):
                job = self.jobs.finish(job, started=now, status="missed", summary=f"пропущено: {late_words(late)} — Бендер был выключен или машина спала", late=late)
                agent_ctx.journal.write("heart", f"Задача «{job.name}» пропущена ({late_words(late)})", actor="heart", data={"job": job.id})
                return {"status": "missed"}
            if job.window and not whenmod.window_ok(job.window, now):
                self.jobs.postpone(job, whenmod.next_window_start(job.window, now))
                return {"status": "postponed", "why": "window"}
            if job.idle_only and not self.owner_idle():
                if late > 86400:
                    self.jobs.finish(job, started=now, status="missed", summary="хозяин был занят весь день — пропущено", late=late)
                    return {"status": "missed"}
                self.jobs.postpone(job, now + IDLE_RETRY)
                return {"status": "postponed", "why": "busy"}
            if job.kind == "agent" and job.action.get("session", "main") == "main" and agent_ctx.mind.running(agent_ctx.main_session()):
                self.jobs.postpone(job, now + BUSY_RETRY)  # идёт разговор с хозяином — не влезаем
                return {"status": "postponed", "why": "turn"}
        said = None
        if job.kind == "reflex" and not manual:
            r = await self._reflex_tick(job, now, late)
            if "say" not in r:
                return r
            said = r["say"]
            job = self.jobs.get(job.id) or job
        self._quiet.pop(job.id, None)
        self.firing = job.id
        status, summary, error, sid = "ok", "", None, None
        try:
            if said is not None:
                summary = said
            elif job.kind == "remind":
                summary = self._remind(job, late)
            elif job.kind == "agent":
                summary, sid, error = await self._agent(job, late)
                status = "error" if error else "ok"
            elif job.kind == "reflex":
                summary = await self._reflex(job)
            else:
                raise HumanError("bad_job", f"Неизвестное действие «{job.kind}»", "")
        except asyncio.CancelledError:
            status, error = "error", "остановлено"
            raise
        except Exception as e:  # noqa: BLE001
            status, error = "error", str(getattr(e, "title", "") or e)[:500]
        finally:
            self.firing = None
            job = self.jobs.finish(job, started=now, status=status, summary=summary, error=error, session_id=sid, late=late)
            agent_ctx.journal.write("heart", f"Задача «{job.name}»: {'выполнена' if status == 'ok' else 'ошибка — ' + (error or '')}"[:300], actor="heart",
                              data={"job": job.id, "status": status, "late": round(late), "manual": manual})
            agent_ctx.bus.publish({"type": "job", "job": job.public(), "status": status, "error": error})
        return {"status": status, "summary": summary, "error": error, "session": sid}

    def _remind(self, job: Job, late: float) -> str:
        agent_ctx = self.app
        text = str(job.action.get("text") or job.name)
        lw = late_words(late)
        line = f"⏰ {text}" + (f" ({lw})" if lw else "")
        sid = agent_ctx.main_session()
        msg = {"role": "event", "kind": "remind", "content": line}
        agent_ctx.store.add_message(sid, msg, {"job": job.id})
        agent_ctx.bus.publish({"session": sid, "type": "message", "message": msg})
        agent_ctx.bus.publish({"type": "notify", "kind": "remind", "title": "Напоминание", "text": text, "job": job.id})
        return text

    async def _agent(self, job: Job, late: float) -> tuple[str, str, str | None]:
        agent_ctx = self.app
        target = job.action.get("session", "main")
        if target == "isolated":
            sid = agent_ctx.store.new_session("cron", f"{job.name} · {time.strftime('%d.%m %H:%M')}", meta={"job": job.id})
        else:
            sid = agent_ctx.main_session()
        lw = late_words(late)
        head = f"[Задача по расписанию «{job.name}»" + (f", {lw}" if lw else "") + ". Хозяина рядом может не быть: что требует его одобрения, будет отклонено — сделай, что можешь, и скажи, что осталось.]"
        grants = [(a.get("capability"), a.get("target") or "*") for a in job.authority if isinstance(a, dict) and a.get("capability")]
        if grants:
            agent_ctx.guard.grant(sid, grants)
        try:
            res = await agent_ctx.mind.say(sid, head + "\n" + str(job.action.get("prompt") or job.name), channel="cron", actor="schedule", unattended=True)
        finally:
            if grants:
                agent_ctx.guard.revoke(sid, grants)
        res = res or {}
        text = (res.get("text") or "").strip()
        err = (res.get("error") or {}).get("title") if res.get("error") else None
        if target == "isolated" and job.action.get("deliver", "chat") != "silent" and (text or err):
            main = agent_ctx.main_session()
            line = f"📋 «{job.name}»: " + (text[:3000] if text else f"не вышло — {err}")
            msg = {"role": "event", "kind": "job", "content": line}
            agent_ctx.store.add_message(main, msg, {"job": job.id, "session": sid})
            agent_ctx.bus.publish({"session": main, "type": "message", "message": msg})
            agent_ctx.bus.publish({"type": "notify", "kind": "job", "title": job.name, "text": (text or err or "")[:300], "job": job.id})
        return text[:500], sid, err

    async def _reflex(self, job: Job) -> str:
        from . import reflexes

        return await reflexes.run(self.app, job)

    async def _reflex_tick(self, job: Job, now: float, late: float) -> dict:
        """Плановая проверка рефлекса. Всё спокойно — следующий раз в памяти, без записи ({"status": "quiet"}).
        Сработало — сообщить хозяину и вернуть {"say": текст}: дальше обычный путь с записью запуска."""
        from . import reflexes
        from .jobs import next_fire

        try:
            say, text, meta = await reflexes.evaluate(self.app, job)
        except Exception as e:  # noqa: BLE001 — сломанная проверка: записать ошибку как запуск
            err = str(getattr(e, "title", "") or e)[:300]
            self._quiet.pop(job.id, None)
            job = self.jobs.finish(job, started=now, status="error", error=err, late=late)
            self.app.journal.write("heart", f"Рефлекс «{job.name}»: ошибка — {err}"[:300], actor="heart", data={"job": job.id})
            self.app.bus.publish({"type": "job", "job": job.public(), "status": "error", "error": err})
            return {"status": "error", "error": err}
        if meta is not None:
            job = self.jobs.update(job.id, meta=meta) or job  # состояние поменялось (новый хеш, условие прошло) — это событие
        if not say:
            self._quiet[job.id] = next_fire(job.schedule, now, window=job.window)
            return {"status": "quiet"}
        agent_ctx = self.app
        sid = agent_ctx.main_session()
        msg = {"role": "event", "kind": "reflex", "content": f"🔔 {text}"}
        agent_ctx.store.add_message(sid, msg, {"job": job.id})
        agent_ctx.bus.publish({"session": sid, "type": "message", "message": msg})
        agent_ctx.bus.publish({"type": "notify", "kind": "reflex", "title": job.name, "text": text, "job": job.id})
        return {"say": text}

    # для тулов и пульта

    def add(self, name: str, *, kind: str, text: str = "", prompt: str = "", at=None, every=None, cron=None, session: str = "main",
            window: str = "", idle_only: bool = False, authority: list | None = None, created_by: str = "owner", source: str = "",
            enabled: bool = True, deliver: str = "chat", check: dict | None = None) -> Job:
        schedule = make_schedule(at=at, every=every, cron=cron)
        if kind == "remind":
            if not (text or name).strip():
                raise HumanError("bad_job", "О чём напомнить?", "Нужен текст напоминания.")
            action = {"type": "remind", "text": (text or name).strip()}
        elif kind == "agent":
            if not (prompt or "").strip():
                raise HumanError("bad_job", "Что делать по расписанию?", "Нужен prompt — что агенту сделать, когда придёт время.")
            if session not in ("main", "isolated"):
                raise HumanError("bad_job", "Где делать?", "session — main (в разговоре с хозяином) или isolated (отдельно, итог — строкой в разговор).")
            action = {"type": "agent", "prompt": prompt.strip(), "session": session, "deliver": deliver}
        elif kind == "reflex":
            action = {"type": "reflex", "check": dict(check or {})}
        else:
            raise HumanError("bad_job", f"Не знаю действие «{kind}»", "remind, agent или reflex.")
        job = self.jobs.add(name, schedule, action, window=window, idle_only=idle_only, authority=authority, created_by=created_by,
                            source=source, enabled=enabled)
        self.app.journal.write("heart", f"Новая задача «{job.name}»: {job.describe_schedule()}", actor=created_by, data=job.public())
        self.app.bus.publish({"type": "job", "job": job.public(), "status": "added"})
        self.poke()
        return job

    def remove(self, jid: str, *, by: str = "owner") -> bool:
        self._quiet.pop(jid, None)
        job = self.jobs.get(jid)
        ok = self.jobs.remove(jid)
        if ok and job:
            self.app.journal.write("heart", f"Задача «{job.name}» удалена", actor=by, data={"job": jid})
            self.app.bus.publish({"type": "job", "job": {"id": jid}, "status": "removed"})
            self.poke()
        return ok

    def set_enabled(self, jid: str, on: bool, *, by: str = "owner") -> Job | None:
        self._quiet.pop(jid, None)
        job = self.jobs.update(jid, enabled=on)
        if job:
            self.app.journal.write("heart", f"Задача «{job.name}» {'включена' if on else 'на паузе'}", actor=by, data={"job": jid})
            self.app.bus.publish({"type": "job", "job": job.public(), "status": "enabled" if on else "paused"})
            self.poke()
        return job
