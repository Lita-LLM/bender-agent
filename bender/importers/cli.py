"""`bender import openclaw` — переезд из OpenClaw в терминале: найти, показать план, перенести."""

from __future__ import annotations

import asyncio

from ..core.errors import HumanError
from ..core.paths import default_data_dir
from ..core.termui import Term
from . import openclaw as oc, flock

KIND_TITLES = [
    ("self", "Личность и память"), ("diary", "Дневник"), ("skill", "Навыки"), ("avatar", "Аватары"), ("work", "Файлы из рабочей папки"),
    ("provider", "Модели: провайдеры"), ("role", "Модели: роли"), ("setting", "Настройки"), ("session", "История разговоров"),
    ("secret", "Секреты"), ("archive", "В архив (заберут будущие вехи)"), ("ritual", "Переселение"),
]


def ask(term: Term, question: str, default: bool = True) -> bool:
    hint = "[Д/н]" if default else "[д/Н]"
    try:
        ans = input(f"  {question} {hint} ").strip().lower()
    except EOFError:
        term.line()
        return False
    if not ans:
        return default
    return ans[0] in ("д", "y", "l")  # «l» — «д» в английской раскладке


def choose(term: Term, title: str, options: list[tuple[str, str]]) -> str | None:
    term.line("  " + term.bold(title))
    for i, (_, label) in enumerate(options, 1):
        term.line(f"    {i}. {label}")
    while True:
        try:
            ans = input("    → ").strip()
        except EOFError:
            return None
        if not ans:
            return options[0][0]
        if ans.isdigit() and 1 <= int(ans) <= len(options):
            return options[int(ans) - 1][0]
        term.line(term.dim("    Номер из списка (пусто — первый)."))


def show_plan(term: Term, plan: oc.Plan, secrets: bool) -> None:
    src = plan.install
    term.line()
    term.line("  " + term.steel("Откуда: ", bold=True) + f"{src.root}" + term.dim(f"  (OpenClaw {src.version})" if src.version else ""))
    term.line("  " + term.steel("Агент:  ", bold=True) + f"{plan.agent.name}" + term.dim(f"  ({plan.agent.id})"))
    for kind, title in KIND_TITLES:
        items = [i for i in plan.items if i.kind == kind]
        if not items:
            continue
        term.line()
        term.line("  " + term.amber(title, bold=True))
        for it in items:
            off = it.action == "skip" or not it.selected or (it.kind == "secret" and not secrets)
            mark = term.dim("·") if off else term.green("✓")
            key = " 🔑" if it.secret and secrets else ""
            word = oc.ACTION_WORDS.get(it.action, it.action)
            line = f"    {mark} {it.title}{key}" + term.dim(f" — {word}")
            term.line(line if not off else term.dim(f"    · {it.title} — {word}"))
            if it.detail:
                term.line(term.dim(f"        {it.detail}"))
            if it.note:
                term.line(term.dim(f"        ↳ {it.note}"))
    if plan.warnings:
        term.line()
        term.line("  " + term.amber("Предупреждения", bold=True))
        for w in plan.warnings:
            term.line(f"    ! {w}")
    term.line()


def main(args) -> int:
    from .. import __version__
    from ..core.app import App

    term = Term(animations=not getattr(args, "no_anim", False))
    term.banner(__version__)
    try:
        with term.step("Ищу OpenClaw"):
            installs = oc.find_installs(args.from_path)
        if not installs:
            raise HumanError("no_openclaw", "OpenClaw не найден", "Обычно он живёт в ~/.openclaw. Если в другом месте — укажи: bender import openclaw --from <папка>")
        inst = installs[0]
        if len(installs) > 1 and not args.yes:
            picked = choose(term, "Нашлось несколько установок OpenClaw — какую переносить?", [(str(i.root), f"{i.root}  {i.version}") for i in installs])
            if picked is None:
                return 1
            inst = next(i for i in installs if str(i.root) == picked)
        agent = args.agent or ""
        destination = 'current' if args.replace_current else 'new'
        if destination == 'current' and not agent:
            agent = inst.agent().id
        app = App(args.data or default_data_dir())
        try:
            with term.step("Читаю OpenClaw — только чтение, там ничего не меняется"):
                profiles={a.id:args.profile for a in inst.agents} if getattr(args,'profile',None) else None
                plan = flock.make_plan(app, 'openclaw', str(inst.root), agent, destination, profiles)
            secrets = not args.no_secrets
            term.line("  Назначение: " + ("новые агенты" if destination == 'new' else "замена текущего агента"))
            for entry in plan['destinations']:
                term.line("  → " + entry['name']+' · шаблон '+entry['profile'])
            for item in plan['items']:
                term.line("    " + item['title'] + " — " + oc.ACTION_WORDS.get(item['action'], item['action']))
            for warning in plan['warnings']:
                term.line("  ! " + warning)
            todo = [i for i in plan['items'] if i['action'] != 'skip' and i['selected']]
            if not todo:
                term.line("  Переносить нечего — всё уже на месте.")
                return 0
            if args.dry_run:
                term.line(term.dim("  Это только план (--dry-run). Чтобы перенести — запусти без него."))
                return 0
            if not args.yes:
                if not ask(term, "Переносим?"):
                    term.line("  Отменено. Ничего не изменилось.")
                    return 1
                if secrets and any(i['secret'] for i in todo) and not ask(term, "Ключи и токены (🔑) тоже переносим в сейф Бендера?"):
                    secrets = False
            with term.step("Переселяю"):
                selected = [i['id'] for i in todo if secrets or i['kind'] != 'secret']
                rep = asyncio.run(flock.apply(app, 'openclaw', str(inst.root), agent=agent,
                    destination=destination, expected=plan['hash'], items=selected,
                    secrets=secrets, confirmed=args.replace_current, profiles=profiles))
            term.line("  " + term.green(f"Перенесено: {len(rep['done'])}", bold=True))
            for error in rep['errors']:
                term.line("  " + term.red("✗ " + error))
            for warning in rep['warnings']:
                term.line("  ! " + warning)
            for result in rep['agents']:
                term.line(f"  {result['name']} · {result['id']} · отчёт: {result['dir']}")
            term.ready("bender", ["Выбери нового агента в пульте или через /agent в терминале."], title="Запусти", stop_hint=False)
            return 0 if not rep['errors'] else 1
        finally:
            asyncio.run(app.close())
    except HumanError as e:
        term.error(e)
        return 1
    except KeyboardInterrupt:
        term.line()
        return 130
