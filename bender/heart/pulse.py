"""Пульс: агент сам просыпается проверить, не надо ли что-то сделать. Двухступенчато, чтобы не жечь деньги.

1. Рефлексы, без модели: ждут ли одобрения, упали ли задачи, тревоги тела, недоступна ли Главная модель,
   пора ли пройтись по чек-листу HEARTBEAT.md (изменился или давно не смотрели). Ничего — «пульс ровный»:
   ни вызова модели, ни записи на диск.
2. Суждение — только если есть что обсудить: модель роли «Быстрая» (или Главная, по настройке) отвечает
   JSON: промолчать / сказать хозяину / сделать самому (отдельный ход без хозяина рядом, в рамках
   Предохранителя).

Режимы (heart.mode): two_stage — так; always_model — суждение на каждом пульсе (как OpenClaw);
reflexes — только рефлексы, сигналы — хозяину строкой без модели; off — пульса нет.
Тихие часы: суждения нет, хозяину — только тревоги.

Состояние пульса (когда был, что уже сказано) — в памяти процесса: после перезапуска пульс просто
посмотрит ещё раз.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field

from . import when as whenmod

CHECKLIST_HOURS = 4.0      # чек-лист HEARTBEAT.md — не чаще, если не менялся
ALARM_REPEAT = 6 * 3600    # одна и та же тревога — не чаще
URGENT = ("disk", "temp", "battery")

SYSTEM = """Ты — пульс агента: он проснулся сам, хозяина рядом может не быть. Реши по сигналам и чек-листу, что сделать.
Варианты:
- "silent" — ничего важного (так чаще всего);
- "tell" — коротко сказать хозяину (message — одна-две фразы его языком, в характере агента);
- "do" — агенту стоит что-то сделать самому прямо сейчас (task — поручение самому себе; опасное без хозяина будет отклонено).
Не дёргай хозяина по пустякам и не повторяй уже сказанное. Ответ — только JSON:
{"action": "silent" | "tell" | "do", "message": "…", "task": "…"}"""


@dataclass
class Signal:
    kind: str
    text: str
    urgent: bool = False
    key: str = ""  # для «уже говорил»


@dataclass
class PulseState:
    next_at: float | None = None
    last_at: float | None = None
    last_judged: float | None = None
    checklist_hash: str | None = None
    said: dict = field(default_factory=dict)     # ключ сигнала → когда сказали
    last_jobs_check: float = field(default_factory=time.time)
    beats: int = 0
    calls: int = 0
    last_result: str = ""


def checklist(text: str) -> list[str]:
    """Пункты HEARTBEAT.md: строки-списки, кроме заглушек шаблона."""
    out = []
    for ln in (text or "").splitlines():
        s = ln.strip()
        m = re.match(r"^(?:[-*]|\d+[.)])\s+(?:\[[ xX]\]\s*)?(.+)$", s)
        if m and not m.group(1).startswith("_"):
            out.append(re.sub(r"<!--.*?-->", "", m.group(1)).strip())
    return [x for x in out if x]


def in_quiet(agent_ctx, ts: float | None = None) -> bool:
    q = str(agent_ctx.settings.get("heart", "quiet_hours", "") or "")
    try:
        return bool(q) and whenmod.window_ok(q, ts or time.time())
    except Exception:  # noqa: BLE001 — кривые тихие часы не ломают пульс
        return False


def interval(agent_ctx, ts: float | None = None) -> float:
    s = agent_ctx.settings.group("heart")
    minutes = float(s.get("night_interval_minutes", 120) if in_quiet(agent_ctx, ts) else s.get("interval_minutes", 30))
    try:
        if agent_ctx.body.mood()["code"] in ("tired", "hungry", "hot", "drained"):
            minutes *= 2  # устал, голоден, перегрет, разряжен — пульс реже
    except Exception:  # noqa: BLE001
        pass
    return max(60.0, minutes * 60)


def gather(agent_ctx, st: PulseState, now: float) -> list[Signal]:
    """Ступень 1: всё, что можно узнать без модели и без записи на диск."""
    sig: list[Signal] = []
    try:
        agent_ctx.heart.intents.expire(now)  # просроченные намерения закрываются (запись — только если есть что закрыть)
    except Exception:  # noqa: BLE001
        pass
    # одобрения, которые ждут хозяина
    waiting = [p["card"] for p in agent_ctx.guard.pending.values()] if hasattr(agent_ctx.guard, "pending") else []
    old = [c for c in waiting if now - c.get("ts", now) > 600]
    if old:
        sig.append(Signal("approvals", f"Ждут одобрения хозяина: {len(old)} (самое старое — {round((now - min(c['ts'] for c in old)) / 60)} мин): "
                          + "; ".join((c.get("summary") or c.get("what") or "")[:80] for c in old[:3]), key=f"approvals:{len(old)}"))
    # задачи, упавшие с прошлого пульса
    for r in agent_ctx.store.q("SELECT j.name, r.summary, r.started FROM job_runs r JOIN jobs j ON j.id = r.job_id WHERE r.status = 'error' AND r.started > ?",
                         (st.last_jobs_check,)):
        sig.append(Signal("job", f"Задача «{r['name']}» упала: {r['summary'][:200]}", key=f"job:{r['name']}:{int(r['started'])}"))
    st.last_jobs_check = now
    # тело
    try:
        from ..body import sensors

        snap_disk = None
        import shutil

        du = shutil.disk_usage(agent_ctx.dirs.root)
        snap_disk = du.free / max(1, du.total)
        if snap_disk < 0.05 or du.free < 1 * 2**30:
            sig.append(Signal("disk", f"Мало места на диске: свободно {du.free / 2**30:.1f} ГБ ({snap_disk * 100:.0f}%)", urgent=True, key="disk"))
        t = sensors.temperature()
        if t is not None and t > float(agent_ctx.settings.get("heart", "alarm_temp_c", 85)):
            sig.append(Signal("temp", f"Машина горячая: {t} °C", urgent=True, key="temp"))
        b = sensors.battery() if agent_ctx.settings.get("heart", "watch_battery", True) else None
        if b and b.get("percent") is not None and b["percent"] < 15 and not b.get("charging"):
            sig.append(Signal("battery", f"Батарея садится: {b['percent']}%", urgent=True, key="battery"))
    except Exception:  # noqa: BLE001
        pass
    # деньги
    try:
        b = agent_ctx.body.budget()
        if b["daily"] and b["share"] >= 1.0:
            sig.append(Signal("budget", f"Дневной бюджет ${b['daily']:.2f} кончился (потрачено ${b['spent']:.2f})"
                              + (" — перешёл на бесплатные модели" if b["economy_auto"] else ""), key=f"budget:{time.strftime('%Y-%m-%d')}"))
    except Exception:  # noqa: BLE001
        pass
    # Главная модель
    try:
        chain = agent_ctx.router.chain("main")
        if chain and all(agent_ctx.router.health.blocked(c.provider["id"], 0, c.model)[0] for c in chain):
            sig.append(Signal("models", "Все модели роли «Главная» сейчас недоступны (остывают после ошибок)", key="models"))
    except Exception:  # noqa: BLE001
        pass
    # чек-лист HEARTBEAT.md
    items = checklist(agent_ctx.selfdir.read("HEARTBEAT.md"))
    if items:
        h = hashlib.sha256("\n".join(items).encode()).hexdigest()[:16]
        due = st.last_judged is None or now - st.last_judged > CHECKLIST_HOURS * 3600 or h != st.checklist_hash
        if due:
            sig.append(Signal("checklist", "Пора пройтись по чек-листу HEARTBEAT.md:\n" + "\n".join(f"- {x}" for x in items[:20]), key=f"checklist:{h}"))
        st.checklist_hash = h
    # уже сказанное — не повторять
    fresh = []
    for s in sig:
        when_said = st.said.get(s.key)
        if s.kind != "checklist" and when_said and now - when_said < ALARM_REPEAT:
            continue
        fresh.append(s)
    return fresh


async def judge(agent_ctx, signals: list[Signal], st: PulseState) -> dict:
    from ..mind import side

    role = agent_ctx.settings.get("heart", "think_role", "fast")
    t = time.localtime()
    user = (f"Сейчас {time.strftime('%Y-%m-%d %H:%M', t)}.\n\nСигналы:\n" + ("\n".join(f"- {s.text}" for s in signals) or "- нет")
            + (f"\n\nЧто пульс сказал в прошлый раз: {st.last_result}" if st.last_result else ""))
    if role == "main":
        res = await agent_ctx.router.call([{"role": "system", "content": SYSTEM}, {"role": "user", "content": user}], role="main")
        agent_ctx.store.add_call(session_id=None, role="main", provider=res.provider_name, model=res.model, usage=res.usage.to_dict(), cost=res.cost,
                           seconds=res.seconds, notice="пульс", request={"messages": [{"role": "user", "content": user}]})
        text = res.text or ""
    else:
        text = await side.ask(agent_ctx, SYSTEM, user, session_id=None, purpose="пульс")
    data = side.parse_json(text) or {}
    act = str(data.get("action") or "silent").lower()
    if act not in ("silent", "tell", "do"):
        act = "silent"
    return {"action": act, "message": str(data.get("message") or "").strip(), "task": str(data.get("task") or "").strip()}


async def beat(agent_ctx, st: PulseState, *, force: bool = False) -> dict:
    """Один удар пульса. Возвращает, что произошло: {"signals": […], "action": …, "model": bool}."""
    mode = agent_ctx.settings.get("heart", "mode", "two_stage")
    now = time.time()
    st.last_at = now
    st.beats += 1
    if mode == "off" and not force:
        return {"signals": [], "action": "off", "model": False}
    signals = gather(agent_ctx, st, now)
    quiet = in_quiet(agent_ctx, now)
    out: dict = {"signals": [s.text for s in signals], "action": "silent", "model": False, "quiet": quiet}
    urgent = [s for s in signals if s.urgent]
    if quiet and not force:
        for s in urgent:
            _say(agent_ctx, st, s.key, f"🚨 {s.text}", kind="alarm", now=now)
        out["action"] = "alarm" if urgent else "silent"
        return out
    if mode == "reflexes":
        for s in signals:
            if s.kind != "checklist":
                _say(agent_ctx, st, s.key, ("🚨 " if s.urgent else "💓 ") + s.text, kind="alarm" if s.urgent else "pulse", now=now)
        out["action"] = "tell" if signals else "silent"
        return out
    if not signals and mode != "always_model":
        return out  # пульс ровный: ни модели, ни записи (и при «проверить сейчас» тоже)
    for s in urgent:  # тревоги — хозяину сразу, без ожидания модели
        _say(agent_ctx, st, s.key, f"🚨 {s.text}", kind="alarm", now=now)
    try:
        verdict = await judge(agent_ctx, signals, st)
    except Exception as e:  # noqa: BLE001 — модель недоступна: сказать сигналы как есть
        verdict = {"action": "tell" if [s for s in signals if not s.urgent and s.kind != "checklist"] else "silent",
                   "message": "; ".join(s.text for s in signals if not s.urgent and s.kind != "checklist")[:500], "error": str(getattr(e, "title", "") or e)[:200]}
    st.calls += 1
    st.last_judged = now
    for s in signals:
        if s.kind != "checklist":
            st.said[s.key] = now
    out.update(model=True, action=verdict["action"])
    if verdict["action"] == "tell" and verdict.get("message"):
        _say(agent_ctx, st, f"tell:{verdict['message'][:40]}", f"💓 {verdict['message']}", kind="pulse", now=now)
        st.last_result = verdict["message"][:300]
    elif verdict["action"] == "do" and verdict.get("task"):
        out["task"] = verdict["task"]
        st.last_result = "сделал сам: " + verdict["task"][:300]
        await _do(agent_ctx, verdict["task"])
    else:
        st.last_result = "промолчал"
    agent_ctx.journal.write("heart", f"Пульс: {', '.join(s.kind for s in signals) or 'по расписанию'} → {verdict['action']}"[:300], actor="heart",
                      data={"signals": [s.text[:200] for s in signals], "verdict": verdict})
    return out


def _say(agent_ctx, st: PulseState, key: str, text: str, *, kind: str, now: float) -> None:
    st.said[key] = now
    sid = agent_ctx.main_session()
    msg = {"role": "event", "kind": kind, "content": text}
    agent_ctx.store.add_message(sid, msg, {"pulse": True})
    agent_ctx.bus.publish({"session": sid, "type": "message", "message": msg})
    agent_ctx.bus.publish({"type": "notify", "kind": kind, "title": "Тревога" if kind == "alarm" else "Пульс", "text": text})


async def _do(agent_ctx, task: str) -> None:
    sid = agent_ctx.store.new_session("heartbeat", f"Пульс · {time.strftime('%d.%m %H:%M')}")
    head = "[Пульс: ты проснулся сам, хозяина рядом может не быть. Что требует его одобрения — будет отклонено; сделай, что можешь, и коротко отчитайся.]"
    res = await agent_ctx.mind.say(sid, head + "\n" + task, channel="heartbeat", actor="schedule", unattended=True) or {}
    text = (res.get("text") or "").strip()
    if text:
        main = agent_ctx.main_session()
        msg = {"role": "event", "kind": "job", "content": f"💓 Сделал сам: {text[:2000]}"}
        agent_ctx.store.add_message(main, msg, {"pulse": True, "session": sid})
        agent_ctx.bus.publish({"session": main, "type": "message", "message": msg})
        agent_ctx.bus.publish({"type": "notify", "kind": "pulse", "text": msg["content"]})
