"""Тулы «про себя»: план, свои настройки, состояние тела, вопрос хозяину."""

from __future__ import annotations

import json

from ..core.settings import SCHEMA, NeedsOwner
from ..guard.policy import Action
from .registry import ToolContext, ToolResult


def render_plan(items: list[dict]) -> str:
    marks = {"done": "☑", "doing": "◐", "todo": "☐"}
    return "\n".join(f"{marks.get(i.get('status', 'todo'), '☐')} {i.get('text', '')}" for i in items)


def register(reg) -> None:
    @reg.tool("plan", "План", group="self")
    async def plan(ctx: ToolContext, items: list) -> ToolResult:
        """Вести видимый план для длинной задачи: список шагов со статусами. Передавай весь план целиком при каждом обновлении.

        items — список шагов: {"text": "…", "status": "todo" | "doing" | "done"}
        """
        clean = []
        for it in items or []:
            if isinstance(it, str):
                clean.append({"text": it, "status": "todo"})
            elif isinstance(it, dict) and it.get("text"):
                clean.append({"text": str(it["text"])[:300], "status": it.get("status", "todo") if it.get("status") in ("todo", "doing", "done") else "todo"})
        ctx.turn["plan"] = clean
        ctx.app.bus.publish({"type": "plan", "session": ctx.session_id, "items": clean})
        done = sum(1 for i in clean if i["status"] == "done")
        return ToolResult(render_plan(clean), summary=f"план: {done}/{len(clean)}")

    @reg.tool("settings", "Мои настройки", group="self")
    async def settings(ctx: ToolContext, group: str = "", changes: dict | None = None, reason: str = "") -> ToolResult:
        """Посмотреть или изменить свои настройки (пульс, сон, лимиты, Предохранитель…). Без group — список групп.
        Изменение, которое ослабляет защиту или расширяет твою самостоятельность, уйдёт хозяину на подтверждение.

        group — группа настроек; team_roles — предложить роль хозяину (changes: имя → title/prompt/capabilities/model/schema/enabled)
        roles — цепочки моделей {"fast": [{"provider":"подключённый id", "model":"id"}]}; платный переход/удаление запасных требуют хозяина.
        reasoning — усилия этого агента {"effort":"high"} или default/off/minimal/low/medium/xhigh/max/adaptive; enabled=false — не задавать их.
        У кандидата roles можно указать params.effort и params.effort_mode; они важнее умолчания. Текущий перевод виден в about_me.
        reason — причина смены роли, обязательна для roles
        changes — что изменить: {"ключ": значение}
        """
        agent_ctx = ctx.app
        if not group:
            rows = [f"{g.name} — {g.title}: {g.hint}" for g in SCHEMA.values() if g.fields or g.name in ("team_roles", "roles")]
            return ToolResult("\n".join(rows), summary="группы настроек")
        g = SCHEMA.get(group)
        if not g or (not g.fields and group not in ("team_roles", "roles")):
            return ToolResult(f"Ошибка: нет группы «{group}» (или её меняют другими тулами).", ok=False, summary="нет группы")
        if group == 'roles':
            if not changes: return ToolResult(json.dumps(agent_ctx.models.roles(), ensure_ascii=False), summary='роли моделей')
            if not reason.strip(): return ToolResult('Укажи reason: зачем меняешь модель роли. Хозяин увидит объяснение.', ok=False, summary='нужна причина')
        if group == "team_roles":
            from ..team.roles import public, validate_roles

            if not changes:
                return ToolResult(json.dumps({k: public(v) for k,v in agent_ctx.team.roles().items()}, ensure_ascii=False, indent=2), summary="роли команды")
            validate_roles({**agent_ctx.settings.group(group), **changes})
        if not changes:
            cur = agent_ctx.settings.group(group)
            rows = [f"{f.key} = {json.dumps(cur.get(f.key), ensure_ascii=False)} — {f.title}" + (f" (варианты: {', '.join(f.choices)})" if f.choices else "") for f in g.fields]
            if group == "guard":  # сохранённое может перекрываться потолком прав — показываем, что действует на самом деле
                st = agent_ctx.guard.state()
                rows.append(f"Действует сейчас: {'включён' if st['enabled'] else 'выключен'}, профиль {st['profile']} — {st['source']}. "
                            "Строки выше — сохранённые значения; при расхождении решает действующее.")
            return ToolResult(f"{g.title}\n" + "\n".join(rows), summary=f"настройки {group}")
        await ctx.check("settings", f"{group}: {json.dumps(changes, ensure_ascii=False)}", f"изменить настройки «{g.title}»: {json.dumps(changes, ensure_ascii=False)[:200]}")
        try:
            diff = agent_ctx.settings.set(group, changes, by="agent")
        except NeedsOwner as e:
            a = Action(capability="settings", target=f"{group}: {json.dumps(e.changes, ensure_ascii=False)}", actor=ctx.actor, tool="settings",
                       summary=f"ослабить: {e.hint}", tainted=ctx.tainted, unattended=ctx.unattended)
            d = await agent_ctx.guard.require_owner(a, f"Ослабляет защиту или расширяет самостоятельность: {e.hint}", session_id=ctx.session_id)
            if not d.allowed:
                return ToolResult(f"Хозяин не подтвердил: {e.hint}", ok=False, summary="не подтверждено")
            diff = agent_ctx.settings.set(group, e.changes, by="owner")
        if group == 'roles' and diff:
            from ..mind.notices import home
            from ..mind.passport import clean
            def chain(entries): return ' → '.join(f"{e.get('provider')}/{e.get('model')}" for e in entries or []) or 'не задана'
            explanation = clean(agent_ctx, reason.strip().replace('\n', ' ')[:500])
            for role, (before, after) in diff.items():
                await home(agent_ctx, f'Переключил {role}: {chain(before)} → {chain(after)}, потому что {explanation}', 'models')
        agent_ctx.journal.write("settings", f"Настройки «{g.title}»: " + ", ".join(f"{k}: {a} → {b}" for k, (a, b) in diff.items()), actor=ctx.actor, data={"group": group, "diff": {k: list(v) for k, v in diff.items()}})
        return ToolResult("Изменено: " + (", ".join(f"{k}: {a} → {b}" for k, (a, b) in diff.items()) or "ничего (уже так)"), summary=f"⚙️ {g.title}")

    @reg.tool("status", "Самочувствие", group="self")
    async def status(ctx: ToolContext) -> ToolResult:
        """Нагрузка и самочувствие машины: память, диск, батарея, температура, расходы и режим. Названия моделей, цепочки и свои папки — about_me."""
        snap = ctx.app.body.snapshot(full=True)
        text = ctx.app.body.describe(snap)
        if snap.get("top"):
            text += "\nБольше всех памяти едят: " + ", ".join(f"{p['name']} {p['rss_mb']} МБ" for p in snap["top"][:5])
        return ToolResult(text, summary=f"самочувствие: {snap['mood']['word']}", data=snap)

    @reg.tool("about_me", "Кто я", group="self")
    async def about_me(ctx: ToolContext, topic: str = "status") -> ToolResult:
        """Модели всех ролей, проверенная песочница, папки, тулы, каналы и лимиты. Для нагрузки машины бери status.

        topic — status: живой паспорт; help: справка об устройстве Бендера
        """
        from ..mind.passport import snapshot
        from pathlib import Path

        if topic == "help":
            text = (Path(__file__).parents[1] / 'mind' / 'self.md').read_text(encoding='utf-8')
            return ToolResult(text, summary="справка об устройстве")
        data = snapshot(ctx.app, ctx.app.mind.turns.get(ctx.session_id), detailed=True)
        return ToolResult(json.dumps(data, ensure_ascii=False), data=data, summary="живой паспорт")

    @reg.tool("ask_owner", "Спросить хозяина", group="self")
    async def ask_owner(ctx: ToolContext, question: str, options: list | None = None) -> ToolResult:
        """Задать хозяину вопрос и дождаться ответа (с вариантами-кнопками или свободным текстом). Не злоупотребляй: сначала попробуй разобраться сам.

        question — вопрос
        options — варианты ответа (необязательно)
        """
        if ctx.unattended and ctx.turn.get('agent_letter'):
            # письмо агента, цепочку начал не хозяин: вопрос уходит карточкой, ответ придёт отдельным сообщением
            text = ctx.app.host.agents.correspondence.ask_owner_later(ctx, question, [str(o) for o in (options or [])][:8])
            return ToolResult(text, summary="вопрос хозяину передан")
        if ctx.unattended:
            return ToolResult("Хозяина рядом нет — спросить сейчас нельзя. Отложи вопрос (запиши в дневник) или действуй в рамках полномочий.", ok=False, summary="некого спросить")
        answer = await ctx.app.ask_owner(question, [str(o) for o in (options or [])][:8], session_id=ctx.session_id)
        if answer is None:
            return ToolResult("Хозяин не ответил.", ok=False, summary="без ответа")
        return ToolResult(f"Ответ хозяина: {answer}", summary=f"❓ {question[:40]} → {answer[:40]}")

    @reg.tool("import_openclaw", "Переезд из OpenClaw", group="self")
    async def import_openclaw(ctx: ToolContext, apply: bool = False, path: str = "", agent: str = "", replace_current: bool = False) -> ToolResult:
        """Показать план или перенести OpenClaw в новых агентов. Существующий агент сохраняется.
        Применение всегда подтверждает хозяин; ключи идут в общий сейф без показа модели.

        apply — false: только план; true: перенести (с одобрения хозяина)
        path — папка OpenClaw (по умолчанию ищется сама, обычно ~/.openclaw)
        agent — ID в OpenClaw; пусто — все, каждый в своего агента
        replace_current — явно заменить текущего, вместо создания новых
        """
        import asyncio

        from ..importers import openclaw as oc,flock
        from ..core.pathutil import within
        agent_ctx = ctx.app
        roots=[p for p in oc.candidates(path or None) if p.is_dir() and oc._looks_like_install(p)]
        if not roots:return ToolResult('OpenClaw не найден. Укажи его папку.',ok=False,summary='источник не найден')
        await ctx.check('fs.read',str(roots[0]),'прочитать источник OpenClaw')
        installs=oc.find_installs(path or None,app=agent_ctx)
        for source in installs[0].agents:
            if agent and agent!='*' and source.id!=agent:continue
            for p in (source.workspace,source.agent_dir):
                if p is not None and not within(p,installs[0].root):await ctx.check('fs.read',str(p),'прочитать папку агента OpenClaw')
        destination='current' if replace_current else 'new'
        plan = await asyncio.to_thread(flock.make_plan,agent_ctx,'openclaw',path or None,agent,destination)
        todo=[i for i in plan['items'] if i['action']!='skip' and i['selected']]
        lines=[f"OpenClaw: {plan['source']['path']}. Назначение: "+('текущий агент' if replace_current else 'новые агенты-помощники')]
        lines += [f"{i['title']} — {oc.ACTION_WORDS.get(i['action'],i['action'])}" for i in plan['items']]
        lines += ['! '+w for w in plan['warnings']]
        if not apply:
            return ToolResult("\n".join(lines) + "\n\nЧтобы перенести — вызови с apply=true (спросит хозяина).", summary=f"план переезда: {len(todo)} пунктов")
        if not todo:
            return ToolResult("Переносить нечего — всё уже на месте.", summary="нечего переносить")
        action=Action('self.rules',plan['source']['path'],actor=ctx.actor,tool=ctx.tool,tainted=ctx.tainted,unattended=ctx.unattended,
                      summary='заменить текущего агента' if replace_current else 'создать агентов-помощников из OpenClaw',detail={'preview':'\n'.join(lines)[:4000]})
        decision=await agent_ctx.guard.require_owner(action,'Хозяин выбирает назначение и перенос ключей',session_id=ctx.session_id)
        if not decision.allowed:return ToolResult('Хозяин не подтвердил переезд.',ok=False,summary='не подтверждено')
        rep=await flock.apply(agent_ctx,'openclaw',path or None,agent=agent,destination=destination,expected=plan['hash'],items=[i['id'] for i in todo],confirmed=replace_current,allow_running=True)
        out=[f"Перенесено: {len(rep['done'])}."]+['✗ '+e for e in rep['errors']]+['! '+w for w in rep['warnings']]
        out += [f"Агент {a['name']}: {a['id']}; отчёт {a['dir']}" for a in rep['agents']]
        return ToolResult('\n'.join(out),ok=not rep['errors'],summary='переезд: '+str(len(rep['agents']))+' агентов')
