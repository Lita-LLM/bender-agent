"""Тулы сердца: напоминания и задачи по расписанию. «Напомни в пятницу» — это задача планировщика,
а не проза в памяти: в пятницу она сработает, даже если агент про неё забудет."""

from __future__ import annotations

import json
import time

from ..guard.policy import CAPS, Action
from ..heart import when as whenmod
from ..heart.reflexes import TYPES as REFLEX_TYPES
from .registry import ToolContext, ToolResult


def _line(j) -> str:
    state = "" if j.enabled else " · на паузе" if j.runs == 0 or j.next_run else " · завершена"
    what = {"remind": "напомнить", "agent": "ход агента", "reflex": "рефлекс"}.get(j.kind, j.kind)
    nxt = f" · следующий: {whenmod.human(j.next_run)}" if j.next_run and j.enabled else ""
    extra = []
    if j.window:
        extra.append(f"окно {j.window}")
    if j.idle_only:
        extra.append("только в простое")
    if j.authority:
        extra.append("полномочия: " + ", ".join(f"{CAPS.get(a.get('capability'), a.get('capability'))} «{a.get('target', '*')}»" for a in j.authority))
    return f"- «{j.name}» ({what}, {j.describe_schedule()}{nxt}{state})" + (f" [{'; '.join(extra)}]" if extra else "") + f" id={j.id}"


async def api_background(ctx,preview):
    """Расписание исполняется обычным агентом вне API: расширение контекста подтверждает хозяин."""
    if not getattr(ctx.app,'_api_channel',False):return
    from ..core.errors import HumanError
    action=Action(capability='schedule',target='api/background',actor=ctx.actor,tool=ctx.tool,tainted=ctx.tainted,unattended=ctx.unattended,
                  summary='API предлагает фоновую работу обычного агента',detail={'preview':ctx.app.guard.scrub(json.dumps(preview,ensure_ascii=False))[:3000]})
    decision=await ctx.app.guard.require_owner(action,'Фоновая работа выполняется обычным агентом за пределами потолка API. Подтверди конкретное поручение в пульте.',session_id=ctx.session_id)
    if not decision.allowed or ctx.app._closed:raise HumanError('api_background','Фоновая работа API не разрешена','Настрой её в пульте или подтверди конкретное поручение; права API не переносятся молча.')


def register(reg) -> None:
    @reg.tool("remind", "Напомнить", group="heart")
    async def remind(ctx: ToolContext, text: str, when: str) -> ToolResult:
        """Поставить напоминание хозяину на время. Оно придёт строкой «⏰ …» в разговор (и в каналы связи), без модели.
        Если хозяин просит что-то СДЕЛАТЬ в это время (а не просто напомнить) — используй schedule.

        text — о чём напомнить, как скажешь хозяину («позвонить маме»)
        when — когда: «через 20 минут», «+2h», «завтра в 9:00», «в пятницу 18:30», «23.10 10:00», «2026-10-23 09:00»
        """
        agent_ctx = ctx.app
        ts = whenmod.parse(when)
        await ctx.check("schedule", f"напомнить {whenmod.human(ts)}: {text[:200]}", f"напоминание {whenmod.human(ts)}: {text[:120]}")
        job = agent_ctx.heart.add(text[:80], kind="remind", text=text, at=ts, created_by="bender")
        off = "" if agent_ctx.heart.enabled() else " Внимание: сердце выключено в настройках — напоминание сработает, только когда его включат."
        return ToolResult(f"Напомню {whenmod.human(ts)}: «{text}» (id={job.id}).{off}", summary=f"⏰ {whenmod.human(ts)}: {text[:50]}")

    @reg.tool("schedule", "Расписание", group="heart")
    async def schedule(ctx: ToolContext, action: str = "list", name: str = "", prompt: str = "", at: str = "", every: str = "", cron: str = "",
                       session: str = "main", window: str = "", idle_only: bool = False, check: dict | None = None, authority: list[dict] | None = None,
                       job: str = "") -> ToolResult:
        """Задачи по времени: ход агента по расписанию («каждое утро в 9 присылай сводку»), рефлекс без модели
        («следи за местом на диске») или напоминание. Задачи живут, пока их не удалят.

        action — list (показать) | add (добавить) | remove (удалить) | pause | resume | run (запустить сейчас) | history (запуски)
        name — короткое имя задачи
        prompt — что сделать, когда придёт время (для хода агента): пиши как поручение самому себе
        at — разово: «завтра в 9», «2026-10-23 09:00» (для add, ровно одно из at / every / cron)
        every — повторять: «30m», «2h», «1d», «каждые 15 минут»
        cron — cron-выражение: «0 9 * * 1-5» (в 9:00 по будням)
        session — main (в разговоре с хозяином) или isolated (отдельно, итог — строкой в разговор)
        window — окно активности «08:00-23:00»: вне окна задача ждёт
        idle_only — запускать, только когда хозяин не пишет 15 минут
        check — для рефлекса: {"type": disk|file|url|battery|temp|command, …} (тогда prompt не нужен)
        authority — полномочия задачи без вопроса хозяину: [{"capability": "exec", "target": "df -h"}] — спросит хозяина
        job — имя или id задачи (для remove, pause, resume, run, history)
        """
        agent_ctx = ctx.app
        heart = agent_ctx.heart
        if action == "list":
            jobs = heart.jobs.all()
            if not jobs:
                return ToolResult("Задач нет.", summary="расписание пусто")
            head = "" if heart.enabled() else "Сердце выключено в настройках — задачи сейчас не запускаются.\n"
            return ToolResult(head + "\n".join(_line(j) for j in jobs), summary=f"задач: {len(jobs)}")
        if action == "add":
            kind = "reflex" if check else "agent" if prompt else "remind"
            if kind == "reflex" and (check or {}).get("type") not in REFLEX_TYPES:
                return ToolResult("Ошибка: check.type — " + ", ".join(f"{k} ({v})" for k, v in REFLEX_TYPES.items()), ok=False, summary="плохой рефлекс")
            preview = {"name": name, "prompt": prompt, "at": at, "every": every, "cron": cron, "session": session, "window": window, "check": check}
            await ctx.check("schedule", f"{name}: {at or every or cron}", f"задача «{name or prompt[:40]}» ({at or every or cron})",
                            detail={"preview": json.dumps({k: v for k, v in preview.items() if v}, ensure_ascii=False)[:1500]})
            if kind in ('agent','reflex'):await api_background(ctx,preview)
            if authority:
                a = Action(capability="schedule", target=json.dumps(authority, ensure_ascii=False), actor=ctx.actor, tool="schedule",
                           summary=f"дать задаче «{name}» право без вопроса: " + ", ".join(f"{CAPS.get(x.get('capability'), x.get('capability'))} «{x.get('target', '*')}»" for x in authority),
                           tainted=ctx.tainted, unattended=ctx.unattended)
                d = await agent_ctx.guard.require_owner(a, "Полномочия расширяют самостоятельность агента — решает хозяин.", session_id=ctx.session_id)
                if not d.allowed:
                    return ToolResult("Хозяин не дал полномочий — задача не создана. Можно создать без них: опасное тогда будет отклоняться.", ok=False, summary="полномочия не даны")
            j = heart.add(name or prompt[:60], kind=kind, text=name, prompt=prompt, at=at or None, every=every or None, cron=cron or None,
                          session=session, window=window, idle_only=idle_only, authority=authority, created_by="bender", check=check)
            return ToolResult(f"Задача создана:\n{_line(j)}", summary=f"🗓 «{j.name}» {j.describe_schedule()}")
        if action in ("remove", "pause", "resume", "run", "history"):
            j = heart.jobs.find(job or name)
            if not j:
                return ToolResult(f"Нет задачи «{job or name}». Посмотри список: action=list.", ok=False, summary="нет задачи")
            if action == "history":
                rows = heart.jobs.history(j.id)
                lines = [f"- {time.strftime('%d.%m %H:%M', time.localtime(r['started']))}: {r['status']}" + (f" — {r['summary'][:200]}" if r["summary"] else "")
                         for r in rows]
                return ToolResult("\n".join(lines) or "Запусков ещё не было.", summary=f"история «{j.name}»")
            if action in ('run','resume') and j.kind in ('agent','reflex'):await api_background(ctx,j.public())
            await ctx.check("schedule", f"{action}: {j.name}", f"{ {'remove': 'удалить', 'pause': 'приостановить', 'resume': 'возобновить', 'run': 'запустить сейчас'}[action] } задачу «{j.name}»")
            if action == "remove":
                heart.remove(j.id, by=ctx.actor)
                return ToolResult(f"Задача «{j.name}» удалена.", summary=f"🗑 «{j.name}»")
            if action in ("pause", "resume"):
                j = heart.set_enabled(j.id, action == "resume", by=ctx.actor)
                return ToolResult(_line(j), summary=f"{'▶' if action == 'resume' else '⏸'} «{j.name}»")
            if j.kind == "agent" and j.action.get("session", "main") == "main" and agent_ctx.mind.running(agent_ctx.main_session()):
                return ToolResult("Сейчас идёт ход в главном разговоре — запусти задачу в отдельной сессии или подожди.", ok=False, summary="занято")
            res = await heart.fire(j, manual=True)
            return ToolResult(f"Запущено: {res.get('status')}" + (f" — {res.get('summary')}" if res.get("summary") else "") + (f" ({res.get('error')})" if res.get("error") else ""),
                              ok=res.get("status") == "ok", summary=f"▶ «{j.name}»")
        return ToolResult("Ошибка: action — list, add, remove, pause, resume, run или history.", ok=False, summary="плохое действие")

    @reg.tool("intent", "Намерение", group="heart")
    async def intent(ctx: ToolContext, action: str = "list", text: str = "", keywords: str = "", days: int = 90, times: int = 3) -> ToolResult:
        """Намерение по событию: «когда зайдёт речь о X — напомни себе Y». Срабатывает на слова хозяина, без модели:
        текст ляжет рядом с его сообщением. Для «в такое-то время» — remind или schedule, не это.

        action — add | list | cancel
        text — что напомнить себе, когда сработает («спроси про changelog»)
        keywords — слова-триггеры через запятую («релиз, выпуск версии»); выражение срабатывает, если в сообщении есть все его слова
        days — сколько дней действует (по умолчанию 90)
        times — сколько раз сработать (по умолчанию 3; между срабатываниями — сутки)
        """
        it = ctx.app.heart.intents
        if action == "list":
            rows = it.all(active_only=True)
            return ToolResult("\n".join(f"- {r['text']} — на слова: {', '.join(r['keywords'])} (сработало {r['fired']} из {r['budget']}) id={r['id']}" for r in rows)
                              or "Намерений нет.", summary=f"намерений: {len(rows)}")
        if action == "add":
            await ctx.check("schedule", f"намерение на «{keywords}»: {text[:200]}", f"намерение: когда «{keywords[:60]}» — {text[:80]}")
            await api_background(ctx,{'intent':text,'keywords':keywords})
            r = it.add(text, keywords, days=days, budget=times, created_by="bender")
            return ToolResult(f"Запомнил намерение: когда зайдёт речь о «{', '.join(r['keywords'])}» — {r['text']} (до {time.strftime('%d.%m.%Y', time.localtime(r['expires'])) if r['expires'] else 'бессрочно'}).",
                              summary=f"🎯 {', '.join(r['keywords'])[:40]}")
        if action == "cancel":
            r = it.find(text or keywords)
            if not r:
                return ToolResult("Нет такого намерения — посмотри list.", ok=False, summary="нет намерения")
            await ctx.check("schedule", f"отменить намерение: {r['text'][:200]}", f"отменить намерение «{r['text'][:60]}»")
            it.cancel(r["id"])
            return ToolResult(f"Отменено: {r['text']}", summary="🎯 отменено")
        return ToolResult("Ошибка: action — add, list или cancel.", ok=False, summary="плохое действие")
