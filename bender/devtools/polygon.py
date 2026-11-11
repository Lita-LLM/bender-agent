"""Полигон: потрогать любую часть Бендера отдельно — `bender try …`.

Каждая новая часть Бендера добавляет сюда свою проверку (правило проекта, AGENTS.md).
"""

from __future__ import annotations

import asyncio
import json
import shlex
import sys
import time

from ..core.errors import HumanError
from ..core.paths import default_data_dir
from ..core.termui import Term

ITEMS = {
    'effort':'усилия моделей без сети: try effort [агент]; /effort high — умолчание агента, отдельный выбор — Модели → роль',
    'api':'агенты как модели: try api [агент] — список и пересечение прав без сети/ключей; подключение — Связи → API',
    'matrix':'отдельный модуль: try matrix — состояние без сети; try matrix check — явная проверка сервера/комнаты; настройка — Связи → Matrix',
    'speakers':'колонки с Pulsar: try speakers — состояние без сети; try speakers check АДРЕС[:ПОРТ] — явная проверка связи и модели колонки',
    'manner':'манеры речи: try manner — библиотека и текущая; try manner ИМЯ — раздел системного промпта; переключить — bender style use ИМЯ',
    'media':'зрение/слух моделей без запросов; try media ПРОВАЙДЕР МОДЕЛЬ vision|audio_input — одна явная проверка',
    'lifecycle':'запуск, PID и ожидание ходов; управление — пульт или /restart /shutdown с подтверждением',
    'attachments':'вложения выбранного агента без чтения содержимого; отправка — try tool send_file',
    "doctor": "самопроверка без фоновой жизни и запросов модели",
    "report": "только состав технического отчёта; сохранить — bender report",
    "soak": "сутки событий на фальшивках за час: try soak 60 [--report файл.json]; пользовательские данные не трогает",
    "proxy": "посредник сети тулов-кода и защита прямых соединений; домены — в одобренном пакете",
    "inspector": "происхождение и использования записи: try inspector ID из recall; правки — пульт → Память",
    "voice": "аудиороли; try voice local — установленный пакет и агенты; try voice connect [агент|all]; try tool speak — голосовой ответ хозяину",
    "face": "параметры визора и темы; изменить — Личность или update_self file=avatar",
    "backup": "снимки и проверка: try backup; команды backup now/list/verify/restore",
    "metrics": "исходы, расходы, поправки, откаты, навыки и экзамены — только чтение",
    "workshop": "тулы-код, проверки и история; создание — Полигон → propose_tool",
    "exams": "экзамены и история: try exams; try exams run — прогон отдельной копии",
    "curator": "статистика навыков; try curator run — уборка по срокам, без модели",
    "update": "версии и самопроверка: try update; try update check — новые коммиты своей ветки без установки",
    "rollback": "прежний слот и история отката: try rollback; переключение — в пульте",
    "roles": "свои и встроенные роли команды: try roles; настройка — «Задачи → Роли»",
    "migration": "план переезда без записи: try migration hermes|claude [папка]; применение — в пульте",
    "modules": "пакеты, карантин и каналы: try modules; вызовы — обычные MCP-тулы в try tool",
    "codex": "официальный Codex CLI: try codex [путь]; проверка доступности, без вызова модели",
    "sandbox": "защита ОС и лимиты ресурсов: try sandbox; команда — try tool run '{…}'",
    "search": "поиск через настроенную цепочку: try search \"запрос\"",
    "skills": "навыки и карантин: try skills; try skills read <имя> [references/файл.md]; запуск — try tool skill_run '{…}'",
    "mcp": "MCP: try mcp — подключения и права; try mcp discover|content <имя>; try mcp oauth <имя> — состояние входа; try mcp tasks — свои фоновые задачи без сети. Вызов через try tool, вход/формы — экран MCP.",
    "model": "вызвать модель напрямую: try model [провайдер/модель] \"текст\"",
    "telegram": "настройки живого Telegram без отправки: full/text/off; try telegram reactions/rich/commands/profile/chats/topics — без отправки",
    "mail": "почта: проверить вход и забрать новые письма: try mail",
    "agy": "Gemini Antigravity (agy) на этой машине: найден ли, версия, нужные флаги — без вызова модели: try agy [путь]",
    "claude": "Claude Code на этой машине: найден ли, вошёл ли, какие флаги знает — без вызова модели: try claude [путь]",
    "sessions": "свой архив: try sessions [агент]; try sessions read ID [агент] — страница разговора",
    "agents": "стая и чаты: try agents; управление — пульт → Стая",
    "fuel": "топливо из полученных ответов; try fuel refresh — квоты OmniRoute без запроса модели",
    "models": "провайдеры, роли, здоровье ключей",
    "setup": "подключить провайдера и выбрать Главную модель (вопросами)",
    "catalog": "список моделей у провайдера: try catalog [ID провайдера]",
    "tool": "вызвать тул: try tool read_file '{\"path\": \"x.txt\"}'",
    "tools": "список тулов и их выключатели",
    "guard": "спросить Предохранитель, не выполняя: try guard exec \"rm -rf build\" [--tainted] [--unattended]",
    "journal": "бортжурнал и проверка цепочки: try journal [N]",
    "status": "самочувствие: машина и сам агент",
    "settings": "настройки: try settings [группа] [ключ=значение …]",
    "prompt": "системный промпт, который уйдёт в модель",
    "fetch": "прочитать страницу так, как её увидит агент: try fetch https://…",
    "turn": "один ход агента без чата: try turn \"текст\"",
    "recall": "поиск по памяти глазами агента: try recall \"запрос\" [--all]",
    "stem": "основы слов, как их видит поиск: try stem память памяти памятью",
    "index": "индекс памяти: что проиндексировано; try index rebuild — пересобрать",
    "compact": "сжатие разговора: сколько занимает история, пересказ; try compact now — сжать сейчас, reset — забыть пересказ",
    "review": "разбор после хода вхолостую: что модель запомнила бы из последних ходов: try review [N] [--apply]",
    "cron": "cron-выражение по-человечески и ближайшие запуски: try cron \"0 9 * * 1-5\" [N]",
    "when": "как Бендер понял время: try when \"завтра в 9\"",
    "task": "позвать субагента и дождаться: try task scribe \"перескажи …\" [--context \"…\"]; роли: researcher coder reviewer reader scribe helper",
    "sleep": "уснуть сейчас: try sleep --dry — вхолостую (что запомнил бы), try sleep — по-настоящему",
    "pulse": "удар пульса сейчас: что нашли рефлексы и что решила модель: try pulse [--dry]",
    "intents": "намерения; try intents \"текст\" — что сработало бы на эти слова (вхолостую)",
    "jobs": "задачи по времени: try jobs; try jobs run «имя» — запустить сейчас",
    "membench": "память на большом объёме (синтетический год): try membench [сообщений] [--vectors]",
    "measure": "замеры по таблице целей: память, ЦП и запись в простое, старт: try measure [секунд] [--json]",
}


def _app(args):
    from ..core.app import App

    return App(args.data or default_data_dir())


def main(args) -> int:
    term = Term(animations=False)
    what = args.what
    rest = list(args.args or [])
    if not what or what not in ITEMS:
        if what:
            term.line(term.red(f"  Нет такой проверки: {what}"))
        term.line(term.bold("  Полигон — потрогать часть Бендера отдельно:"))
        for k, v in ITEMS.items():
            term.line(f"    bender try {k:<9} {v}")
        return 0 if not what else 2
    if what in ("doctor", "report"):
        from ..diagnostics.doctor import collect
        from ..diagnostics.report import preview
        print(json.dumps(collect(args.data or default_data_dir()) if what=="doctor" else preview(),ensure_ascii=False,indent=2))
        return 0
    if what == "soak":
        from . import soak

        return soak.main(rest, term, data=args.data)
    if what == "measure":
        from . import measure

        return measure.main(rest, term)
    if what == "membench":
        from . import membench

        return membench.main(rest, term)
    try:
        return asyncio.run(_run(what, rest, args, term))
    except HumanError as e:
        term.error(e)
        return 1
    except KeyboardInterrupt:
        return 130


async def _run(what: str, rest: list[str], args, term: Term) -> int:
    app = _app(args)
    app.bus.bind_loop(asyncio.get_running_loop())
    try:
        return await HANDLERS[what](app, rest, term)
    finally:
        await app.close()


async def try_model(app, rest, term) -> int:
    target = None
    if rest and "/" in rest[0] and not rest[0].startswith("http") and len(rest) > 1:
        pref, model = rest[0].split("/", 1)
        prov = next((p for p in app.models.providers().values() if pref.lower() in (p["id"], p["name"].lower(), str(p.get("preset", "")).lower())), None)
        if not prov:
            raise HumanError("no_provider", f"Нет провайдера «{pref}»", "Список: bender try models")
        target = (prov["id"], model)
        rest = rest[1:]
    text = " ".join(rest) or "Привет! Ответь одной фразой: кто ты?"
    started = time.monotonic()
    first = {"t": None}

    def delta(kind, t):
        if first["t"] is None:
            first["t"] = time.monotonic() - started
        if kind == "text":
            term.write(t)
        elif kind == "reasoning":
            term.write(term.dim(t))

    res = await app.router.call([{"role": "user", "content": text}], target=target, on_delta=delta,
                                on_status=lambda s: term.line(term.dim(f"\n  … {s}")))
    term.line()
    term.line(term.dim(f"  {res.provider_name} · {res.model} · {res.usage.input}→{res.usage.output} токенов · ${res.cost:.6f} · "
                       f"первый кусок {first['t'] or 0:.2f} с, всего {res.seconds:.2f} с · конец: {res.finish}"))
    if res.notice:
        term.line(term.amber("  " + res.notice))
    return 0


async def try_sessions(app,rest,term):
    from ..tools.registry import ToolContext
    reading=rest[:1]==['read']
    if reading and len(rest)<2:raise HumanError('session_read','Нужен id разговора','bender try sessions read ID [агент].')
    selected=rest[2] if reading and len(rest)>2 else rest[0] if not reading and rest else 'main'
    ctx=app.agents.get(app.agents.resolve(selected))
    args={'session_id':rest[1]} if reading else {}
    result=await ctx.tools.run('session_read' if reading else 'sessions',args,ToolContext(ctx,actor='owner',session_id=ctx.main_session()))
    term.line(result.content)
    return 0 if result.ok else 1


async def try_agents(app, rest, term):
    if rest and rest[0]=='chats':
        ctx=app.agents.get(app.agents.resolve(rest[1]) if len(rest)>1 else 'main')
        term.line(json.dumps({'agent':ctx.agent_id,'sessions':[{**row,'read_only':row['kind']=='correspondence'} for row in ctx.store.sessions(limit=300) if row['kind'] in ('main','chat','imported','correspondence')]},ensure_ascii=False,indent=2));return 0
    if rest and rest[0]=='messages':
        term.line(json.dumps(app.agents.correspondence.feed(),ensure_ascii=False,indent=2));return 0
    if rest and rest[0]=='rights':
        ctx=app.agents.get(app.agents.resolve(rest[1]) if len(rest)>1 else 'main')
        term.line(json.dumps(app.agents.rights.public(ctx),ensure_ascii=False,indent=2));return 0
    term.line(json.dumps(app.agents.public(),ensure_ascii=False,indent=2))
    return 0


async def try_attachments(app, rest, term):
    items=app.store.meta_get('attachments:files',{})
    term.line(json.dumps({'enabled':app.settings.get('attachments','enabled'),'files':list(items.values())},ensure_ascii=False,indent=2))
    return 0


async def try_telegram(app, rest, term):
    if rest==['events']:
        term.line(json.dumps({'mode':app.settings.get('telegram','history_events'),'known_messages':len(app.store.meta_get('telegram:refs',{})),'note':'Без отправки и чтения текста. /status и кнопки оставляют служебные факты; правка известного сообщения дополняет текущий ход. Пульт: Ответить — цитата.'},ensure_ascii=False,indent=2));return 0
    if rest==['topics']:
        term.line(json.dumps({'enabled':app.settings.get('telegram','topics'),'topics':list(app.telegram.topics.rows().values()),'note':'Без создания тем. Включи темы у @BotFather и используй /new.'},ensure_ascii=False,indent=2));return 0
    if rest==['chats']:
        term.line(json.dumps({'chats':app.telegram.groups.rows(app),'note':'Без отправки. Пульт → Связи → Где находится бот.'},ensure_ascii=False,indent=2));return 0
    if rest==['profile']:
        term.line(json.dumps({'profile':app.telegram.profile.defaults(),'note':'Без публикации. Пульт → Связи → Публичное лицо бота.'},ensure_ascii=False,indent=2));return 0
    if rest and rest[0]=='commands':
        from ..channels.telegram_commands import menu
        term.line(json.dumps({'private':menu(app),'group':menu(app,True),'languages':len(app.telegram.commands.languages()),'note':'Без отправки. /help — разделы; меню обновится при подключении бота.'},ensure_ascii=False,indent=2));return 0
    if rest and rest[0]=='rich':
        from ..channels.telegram_rich import chunks
        sample=' '.join(rest[1:]) or '# Пример\n\n| Имя | Цена |\n| --- | --- |\n| Бендер | $5 |'
        term.line(json.dumps({'rich':app.telegram.rich_enabled(),'parts':chunks(sample),'note':'Без отправки. Настройки в Связях → Telegram.'},ensure_ascii=False,indent=2));return 0
    if rest == ['reactions']:
        from ..channels.reactions import EMOJI
        term.line(json.dumps({'emoji':EMOJI,'count':len(EMOJI),'wake':app.settings.get('telegram','reaction_wake'),'note':'Без отправки; настройка в Связях → Telegram.'},ensure_ascii=False));return 0
    term.line(json.dumps({"live": app.settings.get("telegram", "live"), "enabled": app.telegram.enabled(), "bound": bool(app.telegram.owner()), "note": "Настройки без отправки; напиши боту для живого хода."}, ensure_ascii=False))
    return 0


async def try_fuel(app, rest, term):
    data = await app.fuel.refresh() if rest == ["refresh"] else app.fuel.snapshot()
    term.line(json.dumps(data, ensure_ascii=False, indent=2))
    return 0


async def try_models(app, rest, term) -> int:
    from ..router.config import ROLES

    provs = app.models.public_providers()
    if not provs:
        term.line("  Провайдеров нет. bender try setup — подключить.")
    for p in provs:
        term.line(f"  {term.bold(p['name'])} [{p['id']}] {p['kind']} {p['base_url']}" + ("" if p.get("enabled", True) else term.red(" (выключен)")) +
                  (f" · ключей: {len(p['keys'])}" if p["keys"] else " · без ключа") + (f" · моделей в каталоге: {len(p.get('models') or [])}" if p.get("models") else ""))
    roles = app.models.roles()
    for r, (title, _) in ROLES.items():
        chain = roles.get(r) or []
        term.line(f"  {title:<14} " + (" → ".join(f"{app.models.providers().get(e['provider'], {}).get('name', '?')} · {e['model']}" for e in chain) or term.dim("как Главная" if r != "main" else "не выбрана")))
    h = app.router.health.snapshot()
    if h["keys"] or h["models"]:
        term.line(term.dim("  Здоровье: " + json.dumps(h, ensure_ascii=False)))
    return 0


async def try_claude(app, rest, term) -> int:
    """Claude Code на этой машине: найден ли, версия, вошёл ли, какие флаги знает — без вызова модели."""
    from ..router import cli

    prov = {"command": list(rest)} if rest else next((p for p in app.models.providers().values() if p.get("kind") == "claude_code"), {})
    st = await cli.check(prov)
    if not st["found"]:
        term.line(term.amber(f"  claude не найден ({' '.join(st['command'])}). Поставь: npm install -g @anthropic-ai/claude-code"))
        return 1
    term.line(f"  программа: {st['path']}")
    term.line(f"  версия:    {st.get('version') or '?'}")
    term.line("  вход:      " + (term.green("вошёл " + ("ключом API (ходы платные)" if st["billing"] == "api" else "подпиской")) if st["logged_in"]
                               else term.amber("не вошёл — claude, потом /login")) + (f" · {st['auth_method']}" if st.get("auth_method") else ""))
    known = await cli.flags(cli.command(prov))
    missing = [f for f in cli.OPTIONAL if f not in known and f != "--effort"]
    term.line("  флаги:     " + ("все нужные есть" if not missing else "старая версия, обойдусь без " + ", ".join(missing)))
    if "--effort" not in known:
        term.line(term.amber("  усилия:    нет --effort — ход с выбранным усилием не пойдёт; обнови Claude Code или выбери default"))
    return 0 if st["logged_in"] else 1


async def try_agy(app, rest, term) -> int:
    """Antigravity (agy): только факты — найден ли, версия, какие флаги знает. Модель не вызывается, вход не проверяется.
    Предупреждение: Google не поддерживает обёртку agy в сторонних приложениях на потребительских планах (docs/research/ANTIGRAVITY.md)."""
    import shutil

    command = rest[0] if rest else shutil.which("agy")
    if not command:
        term.line(term.amber("  agy не найден. Поставь Antigravity CLI по инструкции Google и повтори; путь можно передать: try agy ПУТЬ"))
        return 1

    async def ask(*args):
        try:
            proc = await asyncio.create_subprocess_exec(command, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            out, _ = await asyncio.wait_for(proc.communicate(), 15)
            return out.decode("utf-8", "replace")
        except (OSError, asyncio.TimeoutError):
            return ""

    term.line(f"  программа: {command}")
    term.line(f"  версия:    {(await ask('--version')).strip()[:80] or '?'}")
    helptext = await ask("--help")
    import re
    known = set(re.findall(r'--[a-zA-Z][a-zA-Z0-9-]*', helptext))
    flags = {name: name in known for name in ('--print', '--input-format', '--output-format', '--mode', '--model', '--effort')}
    term.line("  флаги:     " + ", ".join(f"{k} {'есть' if v else 'нет'}" for k, v in flags.items()))
    can_wrap = all(flags[name] for name in ('--print', '--input-format', '--output-format', '--mode'))
    term.line("  движок:    " + (term.green("неинтерактивный режим с потоком событий есть") if can_wrap else term.amber("потока событий не видно — встроить нельзя")))
    term.line(term.amber("  ⚠ Подключение требует принятия риска: Google может ограничить аккаунт."))
    term.line(term.dim("  Режим plan и инструкция не гарантируют запрета встроенных тулов agy. Тулы Бендера проходят через Предохранитель."))
    return 0 if can_wrap else 1


async def try_mail(app, rest, term) -> int:
    """Почта: проверить вход (IMAP и SMTP) и забрать новые письма один раз — без Чтеца, если он не нужен."""
    from ..core.errors import HumanError

    m = app.mail
    if not m.configured():
        term.line(term.amber("  Почта не подключена: «Связь → Почта» в пульте (адрес, пароль приложения, серверы)."))
        return 1
    try:
        with term.step("Вхожу в ящик"):
            st = await m.check()
        term.line(f"  IMAP: писем в папке {st['imap'].get('messages')}" + (" · SMTP: вход есть" if st.get("smtp") else " · SMTP не задан"))
        with term.step("Забираю новые"):
            got = await m.poll()
    except HumanError as e:
        term.error(e)
        return 1
    term.line(f"  новых: {len(got)}" + (" (первый взгляд — без разбора)" if got and got[0]["status"] == "old" else ""))
    for L in m.recent(10):
        term.line(f"  {L['id'][-6:]}  {(L.get('from_name') or L.get('from_addr') or '?')[:24]:<24} {(L.get('subject') or '')[:50]}")
    if m._bg:
        with term.step("Чтец разбирает"):
            await asyncio.gather(*list(m._bg), return_exceptions=True)
    return 0


async def try_setup(app, rest, term) -> int:
    from ..channels.terminal import setup_model

    return 0 if await setup_model(app, term) else 1


async def try_catalog(app, rest, term) -> int:
    from ..router.catalog import list_models

    provs = app.models.providers()
    p = provs.get(rest[0]) if rest else next(iter(provs.values()), None)
    if not p:
        raise HumanError("no_provider", "Нет провайдеров", "bender try setup")
    keys = [app.secrets.get(n) for n in p.get("keys") or []]
    models = await list_models(app.http, p, keys[0] if keys else None)
    for m in models:
        price = m.get("price")
        term.line(f"  {m['id']}" + (term.green(" · бесплатно") if m.get("free") else (f" · ${price['in']:.2f}/${price['out']:.2f} за 1M" if price else "")) +
                  (f" · {m['context']}" if m.get("context") else "") + (" · тулы" if m.get("tools") else ""))
    term.line(term.dim(f"  Всего: {len(models)}"))
    return 0


async def try_tool(app, rest, term) -> int:
    from ..channels.terminal import TerminalChat
    from ..tools.registry import ToolContext

    if not rest:
        raise HumanError("no_tool", "Какой тул?", "bender try tools — список")
    name = rest[0]
    args = json.loads(rest[1]) if len(rest) > 1 else {}
    chat = TerminalChat(app, term)
    app.bus.listen(chat.on_event)

    async def answers():
        from ..channels.terminal import ainput

        while True:
            if chat.pending:
                await chat.answer_pending(await ainput("  ответ › "))
            await asyncio.sleep(0.1)

    helper = asyncio.ensure_future(answers())
    try:
        res = await app.tools.run(name, args, ToolContext(app=app, session_id="polygon", actor="agent"))
    finally:
        helper.cancel()
    term.line((term.green("✓ ") if res.ok else term.red("✗ ")) + res.summary + (term.red("  [недоверенное]") if res.tainted else ""))
    term.line(res.content)
    return 0 if res.ok else 1


async def try_tools(app, rest, term) -> int:
    for t in app.tools.describe():
        term.line(f"  {'●' if t['enabled'] else '○'} {t['name']:<12} {t['title']} — {t['description'][:90]}")
    return 0


async def try_guard(app, rest, term) -> int:
    from ..guard.policy import CAPS, Action

    if len(rest) < 2:
        term.line("  Способности: " + ", ".join(CAPS))
        raise HumanError("args", "Нужно: способность и цель", 'например: bender try guard exec "rm -rf build"')
    flags = {r for r in rest if r.startswith("--")}
    pos = [r for r in rest if not r.startswith("--")]
    a = Action(capability=pos[0], target=" ".join(pos[1:]), tainted="--tainted" in flags, unattended="--unattended" in flags, to_owner="--owner" in flags)
    if a.capability.startswith("fs."):
        a.target = str(app.guard.policy.resolve(a.target))
    d = app.guard.policy.decide(a)
    color = {"allow": term.green, "ask": term.amber, "deny": term.red}[d.verdict]
    term.line(color({"allow": "  РАЗРЕШИТЬ", "ask": "  СПРОСИТЬ ХОЗЯИНА", "deny": "  ЗАПРЕТИТЬ"}[d.verdict], bold=True) + f"  {d.reason}")
    term.line(term.dim(f"  правило: {d.rule} · профиль: {app.settings.get('guard', 'profile')}"))
    return 0


async def try_journal(app, rest, term) -> int:
    n = int(rest[0]) if rest and rest[0].isdigit() else 30
    for e in reversed(app.journal.recent(n)):
        term.line(f"  {time.strftime('%m-%d %H:%M:%S', time.localtime(e['ts']))} {e['kind']:<9} {e['actor']:<8} {e['summary'][:120]}")
    v = app.journal.verify()
    if v.get("truncated"):
        term.line(term.red(f"  ✗ хвост обрезан: якорь помнит запись {v['anchor']}, а последняя — {v['last']}") + term.dim(f" (записей: {v['checked']})"))
    else:
        term.line((term.green("  ✓ цепочка цела") if v["ok"] else term.red(f"  ✗ цепочка разорвана на записи {v['broken_at']}")) + term.dim(f" (записей: {v['checked']})"))
    return 0 if v["ok"] else 1


async def try_status(app, rest, term) -> int:
    snap = app.body.snapshot(full=True)
    term.line(app.body.describe(snap))
    if snap.get("top"):
        term.line("Больше всех памяти едят: " + ", ".join(f"{p['name']} {p['rss_mb']} МБ" for p in snap["top"][:5]))
    return 0


async def try_settings(app, rest, term) -> int:
    if not rest:
        for g in app.settings.describe():
            term.line(term.bold(f"  {g['name']} — {g['title']}"))
            for f in g["fields"]:
                term.line(f"    {f['key']} = {json.dumps(f['value'], ensure_ascii=False)}" + term.dim(f"  {f['title']}"))
        return 0
    group = rest[0]
    changes = {}
    for kv in rest[1:]:
        k, _, v = kv.partition("=")
        try:
            changes[k] = json.loads(v)
        except ValueError:
            changes[k] = v
    if changes:
        diff = app.settings.set(group, changes, by="owner")
        app.journal.write("settings", f"Настройки «{group}» из Полигона: {diff}", actor="owner")
        term.line(term.green(f"  ✓ {diff or 'без изменений'}"))
    for k, v in app.settings.group(group).items():
        term.line(f"  {k} = {json.dumps(v, ensure_ascii=False)}")
    return 0


async def try_prompt(app, rest, term) -> int:
    from ..mind.context import system_prompt

    text = system_prompt(app)
    term.line(text)
    from ..mind.context import estimate_tokens

    term.line(term.dim(f"\n  ≈{estimate_tokens(text)} токенов, {len(text)} символов"))
    return 0


async def try_fetch(app, rest, term) -> int:
    rest = rest or ["https://example.com"]
    return await try_tool(app, ["web_fetch", json.dumps({"url": rest[0]})], term)


async def try_turn(app, rest, term) -> int:
    from ..channels.terminal import TerminalChat

    chat = TerminalChat(app, term)
    app.bus.listen(chat.on_event)
    text = " ".join(rest) or "Привет"
    task = asyncio.ensure_future(app.mind.say(chat.sid, text, channel="полигон"))
    from ..channels.terminal import ainput

    while not task.done():
        if chat.pending:
            await chat.answer_pending(await ainput("  ответ › "))
        else:
            await asyncio.sleep(0.1)
    res = task.result()
    await app.mind.idle()  # разбор и сжатие после хода — их итог тоже покажем
    term.line(term.dim(f"\n  шагов: {res.get('steps')} · ${res.get('cost', 0):.6f}"))
    return 0 if not res.get("error") else 1


async def try_recall(app, rest, term) -> int:
    from ..memory.index import KIND_TITLES, PROVENANCE, age_words

    flags = {r for r in rest if r.startswith("--")}
    q = " ".join(r for r in rest if not r.startswith("--"))
    if not q:
        raise HumanError("args", "Что вспомнить?", 'например: bender try recall "релиз кометы"')
    t0 = time.monotonic()
    kinds = ("core", "diary", "session", "fact", "note") if "--all" in flags else None
    qvec = await app.vectors.embed_query(q)
    hits = await asyncio.to_thread(app.index.search, q, limit=10, kinds=kinds, qvec=qvec)
    ms = (time.monotonic() - t0) * 1000
    for h in hits:
        term.line(term.amber(f"  {h.score:.4f}") + term.dim(f"  {KIND_TITLES.get(h.kind, h.kind)} · {h.source} · {age_words(h.ts)} · {PROVENANCE.get(h.provenance, h.provenance)} · важность {h.importance} · {', '.join(h.why)}"))
        term.line("    " + h.text.replace("\n", "\n    ")[:600])
    term.line(term.dim(f"  найдено: {len(hits)} за {ms:.0f} мс"))
    return 0


async def try_stem(app, rest, term) -> int:
    from ..memory.stem import stem, words

    for w in words(" ".join(rest) or "память памяти памятью воспоминания вспомнил"):
        term.line(f"  {w:<20} → {stem(w)}")
    return 0


async def try_index(app, rest, term) -> int:
    t0 = time.monotonic()
    n = await asyncio.to_thread(app.index.rebuild if rest[:1] == ["rebuild"] else app.index.sync)
    if app.vectors.available():
        with term.step("Считаю векторы для новых кусков"):
            v = await app.vectors.fill(max_items=5000)
        term.line(f"  векторов посчитано: {v}" + (term.red(f" · ошибка: {app.vectors.last_error}") if app.vectors.last_error else ""))
    else:
        term.line(term.dim("  поиск по смыслу выключен: нет роли «Эмбеддинги» (Модели → роли)"))
    st = app.index.stats()
    term.line(f"  добавлено кусков: {n} за {(time.monotonic() - t0) * 1000:.0f} мс")
    term.line(f"  всего: {st['chunks']} ({', '.join(f'{k}: {v}' for k, v in st['by_kind'].items())}) · с векторами: {st['with_vectors']} · FTS5: {'есть' if st['fts'] else 'нет (запасной поиск)'}")
    return 0


async def try_compact(app, rest, term) -> int:
    from ..mind import compact
    from ..mind import context as ctxmod

    sid = app.main_session()
    if rest[:1] == ["reset"]:
        compact.reset(app, sid)
        term.line("  пересказ забыт; сообщения на месте, история снова идёт окном")
        return 0
    budget = ctxmod.history_budget(app, ctxmod.estimate_tokens(ctxmod.system_prompt(app)))
    if rest[:1] == ["now"]:
        with term.step("Сжимаю (роль «Быстрая»)"):
            await compact.manual(app,sid)
    w = compact.window(app, sid, budget)
    st = compact.state(app, sid)
    term.line(f"  бюджет истории: {budget} ток. · хвост после границы: {w.tail_tokens} ток. · в запросе: {w.tokens} ток., {len(w.messages)} сообщ.")
    term.line(f"  сжатие: {'включено' if compact.enabled(app) else 'выключено'} · сожмётся, когда хвост > {int(budget * compact.TRIGGER)} ток.")
    if w.dropped:
        term.line(term.amber(f"  не влезло и не пересказано: {w.dropped} сообщ."))
    if st.get("summary"):
        term.line(term.dim(f"  пересказано сообщений: {st.get('count')} · частей: {st.get('parts')} · {time.strftime('%Y-%m-%d %H:%M', time.localtime(st.get('ts', 0)))}"))
        term.line("    " + st["summary"].replace("\n", "\n    "))
    elif st.get("error"):
        term.line(term.red(f"  последняя ошибка: {st['error']}"))
    else:
        term.line(term.dim("  пересказа пока нет"))
    return 0


async def try_review(app, rest, term) -> int:
    from ..mind import review

    n = int(next((r for r in rest if r.isdigit()), "1"))
    sid = app.main_session()
    ranges = review.last_turns(app, sid, n)
    if not ranges:
        term.line(term.dim("  в главной сессии ещё нет ходов хозяина"))
        return 0
    with term.step(f"Разбираю {review.plural_turns(len(ranges))} (роль «Быстрая»)"):
        data = await review.propose(app, sid, ranges)
    if data is None:
        term.line(term.dim("  не о чем думать: только «ок/спасибо»"))
        return 0
    term.line("  модель предлагает: " + (json.dumps(data, ensure_ascii=False, indent=2).replace("\n", "\n  ") if data else "ничего"))
    if "--apply" in rest and data:
        saved = review.apply(app, data, session_id=sid)
        term.line(term.green("  сохранено: " + (review.summary(saved, verbose=True) or "ничего нового")))
    elif data:
        term.line(term.dim("  вхолостую — ничего не сохранено; --apply сохранит"))
    return 0


async def try_cron(app, rest, term) -> int:
    from ..heart import cron as cronmod
    from ..heart import when as whenmod

    expr = " ".join(r for r in rest if not (r.isdigit() and len(rest) > 1 and r == rest[-1])) or "0 9 * * 1-5"
    n = int(rest[-1]) if len(rest) > 1 and rest[-1].isdigit() else 5
    c = cronmod.parse(expr)
    term.line(f"  {expr} — {c.describe()}")
    t = time.time()
    for _ in range(n):
        t = c.next_after(t)
        if t is None:
            term.line(term.dim("  больше не наступит"))
            break
        term.line(f"  {time.strftime('%a %d.%m.%Y %H:%M', time.localtime(t))} · {whenmod.human(t)}")
    return 0


async def try_when(app, rest, term) -> int:
    from ..heart import when as whenmod

    text = " ".join(rest) or "завтра в 9"
    t = whenmod.parse(text)
    term.line(f"  «{text}» → {time.strftime('%a %d.%m.%Y %H:%M', time.localtime(t))} ({whenmod.human(t)})")
    return 0


async def try_jobs(app, rest, term) -> int:
    from ..tools.heart import _line

    if rest[:1] == ["run"] and len(rest) > 1:
        j = app.heart.jobs.find(" ".join(rest[1:]))
        if not j:
            raise HumanError("not_found", "Нет такой задачи", " ".join(rest[1:]))
        with term.step(f"Запускаю «{j.name}»"):
            res = await app.heart.fire(j, manual=True)
        await app.mind.idle()
        term.line(f"  {res.get('status')}: {res.get('summary') or res.get('error') or ''}")
        return 0 if res.get("status") == "ok" else 1
    jobs = app.heart.jobs.all()
    if not jobs:
        term.line(term.dim("  задач нет — попроси Бендера «напомни завтра в 9 …» или «каждое утро присылай сводку»"))
    for j in jobs:
        term.line("  " + _line(j)[2:])
    if not app.heart.enabled():
        term.line(term.amber("  сердце выключено в настройках — задачи не запускаются"))
    return 0


async def try_pulse(app, rest, term) -> int:
    from ..heart import pulse as pulsemod

    st = app.heart.pulse
    if "--dry" in rest:
        sig = pulsemod.gather(app, st, time.time())
        for s in sig:
            term.line(("  🚨 " if s.urgent else "  · ") + s.text.replace("\n", "\n    "))
        term.line(term.dim(f"  сигналов: {len(sig)} · модель {'позвали бы' if sig or app.settings.get('heart', 'mode') == 'always_model' else 'не нужна'}"
                           + (" · сейчас тихие часы" if pulsemod.in_quiet(app) else "")))
        return 0
    with term.step("Пульс"):
        res = await app.heart.beat(force=True)
    await app.mind.idle()
    for s in res.get("signals") or []:
        term.line("  · " + s.replace("\n", "\n    "))
    term.line(f"  итог: {res.get('action')}" + (" (думала модель)" if res.get("model") else " (без модели)") + (f" · задача: {res['task']}" if res.get("task") else ""))
    return 0


async def try_intents(app, rest, term) -> int:
    from ..heart.intents import matches

    text = " ".join(rest)
    rows = app.heart.intents.all(active_only=True)
    if not rows:
        term.line(term.dim("  намерений нет — попроси Бендера: «когда зайдёт речь о релизе, напомни про changelog»"))
    for r in rows:
        hit = matches(r["keywords"], text) if text else None
        term.line(("  ✓ " if hit else "  · ") + f"{r['text']} — на слова: {', '.join(r['keywords'])} ({r['fired']}/{r['budget']})" + (f" ← «{hit}»" if hit else ""))
    return 0


async def try_sleep(app, rest, term) -> int:
    dry = "--dry" in rest
    with term.step("Сплю" + (" вхолостую" if dry else "")):
        res = await app.sleep.run(dry=dry, force=True)
    m = res.get("material") or {}
    term.line(f"  новое с прошлого сна: {m.get('new', 0)} · пригождалось при поиске: {m.get('useful', 0)} · модель: роль «{app.sleep.role()}» · ${res.get('cost', 0):.4f}")
    d = res.get("dream") or {}
    if d.get("story"):
        term.line("  " + term.dim(str(d["story"])))
    for p in res.get("promoted") or []:
        term.line(term.green(f"  + {p['text']} → {p['to']}"))
    for p in res.get("skipped") or []:
        term.line(term.dim(f"  · {p['text']} ({p['why']})"))
    if res.get("rewrote"):
        term.line(f"  ядро: {res['rewrote']}")
    for e in res.get("errors") or []:
        term.line(term.red(f"  ✗ {e}"))
    if dry:
        term.line(term.dim("  вхолостую — ничего не записано"))
    return 0


async def try_task(app, rest, term) -> int:
    from ..team import render

    roles = app.team.roles()
    if not rest or rest[0] not in roles:
        for k, v in roles.items():
            term.line(f"  {k:<11} {v.title} · модель: {v.model} · тулы: {', '.join(v.tools) or 'нет'}")
        return 0 if not rest else 2
    ctx = ""
    if "--context" in rest:
        i = rest.index("--context")
        ctx = " ".join(rest[i + 1:])
        rest = rest[:i]
    role, goal = rest[0], " ".join(rest[1:])
    t = app.team.spawn(role, goal, context=ctx, parent_session=None)
    with term.step(f"{roles[role].title} работает над #{t['id'][-6:]}"):
        [done] = await app.team.wait([t["id"]], 1800)
    term.line(f"  {done['status']} · шагов {done['steps']} · ${done['cost']:.4f}")
    term.line("  " + render(done).replace("\n", "\n  "))
    return 0 if done["status"] == "done" else 1


HANDLERS = {"sessions":try_sessions,"agents": try_agents, "telegram": try_telegram, "fuel": try_fuel, "task": try_task, "sleep": try_sleep, "pulse": try_pulse, "intents": try_intents, "cron": try_cron, "when": try_when, "jobs": try_jobs, "compact": try_compact, "review": try_review, "recall": try_recall, "stem": try_stem, "index": try_index, "model": try_model, "models": try_models, "setup": try_setup, "claude": try_claude, "agy": try_agy, "mail": try_mail, "catalog": try_catalog, "tool": try_tool, "tools": try_tools,
            "guard": try_guard, "journal": try_journal, "status": try_status, "settings": try_settings, "prompt": try_prompt,
            "fetch": try_fetch, "turn": try_turn}

async def try_mcp(app, rest, term) -> int:
    if rest==['tasks']:
        from ..tools.registry import ToolContext
        term.line(json.dumps(app.mcp.tasks.public(ToolContext(app,actor='owner')),ensure_ascii=False,indent=2))
        return 0
    from ..tools.registry import ToolContext

    if rest:
        if len(rest) != 2 or rest[0] not in ('discover','content','oauth'):
            raise HumanError("mcp_args", "Нужно: try mcp discover|content|oauth <имя>", "Подключение и одобрение — в пульте, раздел «MCP».")
        if rest[0]=='oauth':
            term.line(json.dumps({'enabled':app.settings.get('mcp','oauth_enabled',False),**app.mcp.oauth.status(rest[1])},ensure_ascii=False,indent=2))
            return 0
        discover=app.mcp.discover if rest[0]=='discover' else app.mcp.catalog.discover
        await discover(rest[1], ToolContext(app=app, actor="owner", session_id="polygon"))
    term.line(json.dumps(app.mcp.public(), ensure_ascii=False, indent=2))
    return 0


HANDLERS["mcp"] = try_mcp


async def try_skills(app, rest, term) -> int:
    if not rest:
        term.line(json.dumps(app.skills.public(), ensure_ascii=False, indent=2))
        return 0
    if len(rest) in (2, 3) and rest[0] == "read":
        data = app.skills.read(rest[1], rest[2] if len(rest) == 3 else "SKILL.md", manual=True)
        term.line(data["text"])
        if data["next_offset"] is not None:
            term.line(f"Продолжение: skill_read offset={data['next_offset']}")
        return 0
    raise HumanError("skill_args", "Нужно: try skills или try skills read <имя> [файл]", "Импорт и просмотр — в пульте, раздел «Навыки».")


HANDLERS["skills"] = try_skills


async def try_search(app, rest, term):
    from ..tools.registry import ToolContext

    result = await app.tools.run("web_search", {"query": " ".join(rest)}, ToolContext(app, actor="owner", session_id="polygon"))
    term.line(result.content)
    return 0 if result.ok else 1


HANDLERS["search"] = try_search


async def try_sandbox(app, rest, term):
    from ..guard.sandbox import for_app

    term.line(json.dumps(for_app(app), ensure_ascii=False, indent=2))
    return 0


HANDLERS["sandbox"] = try_sandbox


async def try_lifecycle(app,rest,term):
    from ..core.run_lock import current
    result=app.host.lifecycle.state();result['copy']=current(app.host.dirs.root)
    term.line(json.dumps(result,ensure_ascii=False,indent=2))
    return 0


HANDLERS['lifecycle']=try_lifecycle


async def try_modules(app, rest, term):
    term.line(json.dumps(app.modules.public(), ensure_ascii=False, indent=2))
    return 0


HANDLERS["modules"] = try_modules


async def try_cli(app, rest, term, kind):
    from ..router.codex_probe import check
    from ..router.cli_common import command
    from ..tools.registry import ToolContext

    provider = {"command": rest or [kind]}
    await ToolContext(app, actor="owner", session_id="polygon").check("exec", shlex.join(command(provider)), f"проверить {kind}")
    result = await check(provider)
    term.line(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["found"] else 1


HANDLERS["codex"] = lambda app, rest, term: try_cli(app, rest, term, "codex")

_ = (shlex, sys)


async def try_migration(app, rest, term):
    from ..importers import portable, flock
    from ..tools.registry import ToolContext

    if not rest or rest[0] not in portable.KINDS:
        raise HumanError("migration_args", "Нужно: try migration hermes|claude [папка]", "Перенос выбранного плана — в пульте.")
    root = portable.source(app, rest[0], rest[1] if len(rest) > 1 else None)
    await ToolContext(app, actor="owner", session_id="polygon").check("fs.read", str(root), "прочитать источник переезда")
    plan = await asyncio.to_thread(flock.make_plan, app, rest[0], str(root))
    term.line(json.dumps(plan, ensure_ascii=False, indent=2))
    return 0


HANDLERS["migration"] = try_migration


async def try_roles(app, rest, term):
    from ..team.roles import public

    term.line(json.dumps({k: public(v) for k,v in app.team.roles().items()}, ensure_ascii=False, indent=2))
    return 0


HANDLERS["roles"] = try_roles


async def try_update(app, rest, term):
    term.line(json.dumps(await app.updater.check() if rest == ['check'] else app.versions.public(), ensure_ascii=False, indent=2))
    return 0


HANDLERS["update"] = try_update
HANDLERS["rollback"] = try_update


async def try_exams(app, rest, term):
    from ..tools.registry import ToolContext

    result = await app.workshop.run_exams(ToolContext(app, actor="owner")) if rest == ["run"] else app.workshop.public()
    term.line(json.dumps(result, ensure_ascii=False, indent=2))
    return 1 if result.get("ok") is False else 0


HANDLERS["exams"] = try_exams
HANDLERS["workshop"] = try_exams


async def try_curator(app, rest, term):
    from ..skills.experience import curate

    result = curate(app.skills) if rest == ["run"] else app.skills.public()
    term.line(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


HANDLERS["curator"] = try_curator


async def try_metrics(app, rest, term):
    from ..workshop.metrics import snapshot

    term.line(json.dumps(snapshot(app), ensure_ascii=False, indent=2))
    return 0


HANDLERS["metrics"] = try_metrics


async def try_backup(app, rest, term):
    term.line(json.dumps(app.backups.status(), ensure_ascii=False, indent=2))
    return 0


HANDLERS["backup"] = try_backup


async def try_face(app, rest, term):
    from ..memory.avatar import read
    term.line(json.dumps({"avatar":read(app.dirs.self_dir), "appearance":app.settings.group("appearance")}, ensure_ascii=False, indent=2))
    return 0


HANDLERS["face"] = try_face


async def try_voice(app, rest, term):
    if rest==['local']:
        from ..voice.cli import status
        result=status(app)
    elif rest and rest[0]=='connect':
        from ..voice.connect import connect
        from ..tools.registry import ToolContext
        result=await connect(ToolContext(app,actor='owner'),rest[1] if len(rest)>1 else app.agent_id)
    elif rest and rest[0]=="speak":
        result=await app.voice.speak(" ".join(rest[1:]),session_id=app.main_session())
        path=app.dirs.work/"voice-preview.bin"
        from ..core.fsutil import atomic_write
        from ..tools.registry import ToolContext
        await ToolContext(app,actor="owner").check("fs.write",str(path),"сохранить пробное аудио")
        atomic_write(path,result.pop("audio"))
        result["file"]=str(path)
    else:
        result={"settings":app.settings.group("voice"),"roles":{k:app.models.roles().get(k,[]) for k in ("ears","voice")}}
    term.line(json.dumps(result,ensure_ascii=False,indent=2))
    return 0


HANDLERS["voice"]=try_voice


async def try_media(agent_ctx,rest,term):
    if len(rest)==3:
        from ..router.probes import run
        from ..tools.registry import ToolContext
        result=await run(ToolContext(agent_ctx,actor='owner'),rest[0],rest[1],rest[2])
    elif not rest:
        from ..router.capabilities import description,KEYS
        result={'roles':agent_ctx.models.roles(),'providers':{pid:{'name':p.get('name'),
            'models':{entry['model']:{key:description(p,entry['model'],key) for key in KEYS} for chain in agent_ctx.models.roles().values()
                      for entry in chain if isinstance(entry,dict) and entry.get('provider')==pid and isinstance(entry.get('model'),str)}} for pid,p in agent_ctx.models.providers().items()}}
    else:raise HumanError('model_probe','Нужны провайдер, модель и vision/audio_input','Без аргументов bender try media показывает текущие роли без запросов.')
    term.line(json.dumps(result,ensure_ascii=False,indent=2));return 0


HANDLERS['media']=try_media


async def try_inspector(app,rest,term):
    result=app.inspector.detail(rest[0]) if rest else {"settings":app.settings.group("inspectors"),"usage":app.store.one("SELECT COUNT(*) FROM mem_usage")[0]}
    term.line(json.dumps(result,ensure_ascii=False,indent=2))
    return 0


HANDLERS["inspector"]=try_inspector


async def try_proxy(app, rest, term):
    from ..workshop.proxy import status, hostname
    result = status(app)
    if rest:
        result['host'] = hostname(rest[0])
        result['hint'] += ' Проверка имени не выполняет запрос и не заменяет проверку DNS при соединении.'
    term.line(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


HANDLERS['proxy'] = try_proxy

HANDLERS['attachments']=try_attachments


async def try_manner(app, rest, term) -> int:
    from ..mind import manners
    from ..mind.context import estimate_tokens

    if rest:
        text = manners.section(manners.resolve(app.settings, " ".join(rest)))
        term.line(text)
        term.line(term.dim(f"\n  ≈{estimate_tokens(text)} токенов — столько манера добавит к каждому запросу"))
        return 0
    cur = manners.active(app)
    term.line("  Сейчас: " + (f"«{cur['name']}»" if cur else "свой голос (SOUL.md)") + ("" if app.settings.get("manner", "enabled", True) else " · манеры выключены"))
    for m in manners.library(app.settings):
        term.line(("  → " if cur and cur["id"] == m["id"] else "    ") + f"{m['id']:<12} {m['name']} — {m['description']}")
    return 0


HANDLERS["manner"] = try_manner


async def try_matrix(app,rest,term):
    matrix=app.host.matrix
    if rest:
        if rest!=['check']:raise HumanError('matrix_args','Нужно: try matrix [check]','Подключение и код хозяина — Связи → Matrix.')
        from ..tools.registry import ToolContext
        stamp=matrix.transport.stamp()
        await ToolContext(app.host,actor='owner').check('net.read',matrix.settings()['homeserver'],'проверить Matrix')
        matrix.transport.validate(stamp,enabled=False)
        info=await matrix.transport.call('status',enabled=False);matrix.require_room(info,paired=bool(matrix.owner()))
        term.line(json.dumps(info,ensure_ascii=False,indent=2))
    else:term.line(json.dumps(matrix.public(),ensure_ascii=False,indent=2))
    return 0


HANDLERS['matrix']=try_matrix


async def try_speakers(app,rest,term):
    speakers=app.host.speakers
    if not rest:
        term.line(json.dumps(speakers.public(),ensure_ascii=False,indent=2));return 0
    if len(rest)!=2 or rest[0]!='check':raise HumanError('speakers_args','Нужно: try speakers [check АДРЕС[:ПОРТ]]','Например: bender try speakers check 192.0.2.40')
    from ..speakers.esphome import Link
    from ..tools.registry import ToolContext
    host,_,port=rest[1].partition(':')
    try:port=int(port or 6053)
    except ValueError:raise HumanError('speakers_args','Порт должен быть числом','Например: 192.0.2.40:6053') from None
    await ToolContext(app.host,actor='owner').check('net.read',host,'проверить связь с колонкой')
    link=Link(host,port)
    try:term.line(json.dumps(await link.open(),ensure_ascii=False,indent=2))
    finally:await link.close()
    return 0


HANDLERS['speakers']=try_speakers


async def try_api(app,rest,term):
    from ..channels.api_context import ceiling
    from ..channels.openai_api import catalog
    if len(rest)>1:raise HumanError('api_args','Нужно: try api [агент]','Передай короткий id агента или оставь пустым.')
    ctx=app.host.agents.get(rest[0]) if rest else app
    term.line(json.dumps({'enabled':app.host.settings.get('api','enabled',False),'models':catalog(app.host),'agent':ctx.agent_id,
                          'api_rights':ctx.settings.group('api_rights'),'effective':ceiling(ctx)},ensure_ascii=False,indent=2))
    return 0


HANDLERS['api']=try_api


async def try_effort(app,rest,term):
    from ..router.effort import command
    ctx=app.agents.get(app.agents.resolve(rest[0] if rest else 'main'))
    term.line(await command(ctx,ctx.main_session()))
    return 0


HANDLERS['effort']=try_effort
