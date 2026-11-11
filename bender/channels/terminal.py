"""Разговор в терминале: `bender chat`.

Для SSH, Termux без браузера и для проверки. Модель настраивается вопросами прямо здесь
(никаких конфигов), ответ идёт потоком, тулы видны строками, карточки одобрения и вопросы
агента — прямо в строке ввода. Команды: /стоп, /стоп-кран, /продолжить, /выход, /модель, /помощь.
"""

from __future__ import annotations

import asyncio
import getpass
import json

from ..core.errors import HumanError
from ..core.paths import default_data_dir
from ..core.termui import Term

HELP = """Команды:
  /agent [id]  выбрать агента стаи
  /chats       выбрать чат агента
  /стоп        прервать идущий ход (писать дальше можно сразу)
  /стоп-кран   стоп-кран: остановить всё и не давать действовать до /продолжить
  /продолжить  снять стоп
  /модель      подключить провайдера или выбрать модель
  /effort      усилия моделей этого агента; /effort high, /effort default
  /новая       новая страница разговора (память остаётся)
  /промпт      показать, что уходит в модель как системный промпт
  /убрать-навык имя — убрать навык из активных
  /выход       выйти (агент продолжит жить, если запущен основной процесс)
На карточку одобрения отвечай: 1 — один раз, 2 — в этом разговоре, 3 — всегда (программу, папку, узел или сервер), 0 — нет."""


async def ainput(prompt: str = "") -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: input(prompt))


async def pick(term: Term, title: str, items: list[tuple[str, str]], allow_custom: bool = False) -> str | None:
    term.line(term.bold(title))
    for i, (_, label) in enumerate(items, 1):
        term.line(f"  {i}. {label}")
    while True:
        ans = (await ainput("  → ")).strip()
        if not ans:
            return None
        if ans.isdigit() and 1 <= int(ans) <= len(items):
            return items[int(ans) - 1][0]
        if allow_custom:
            return ans
        term.line(term.dim("  Номер из списка, пусто — отмена."))


async def login_chatgpt(app, term: Term, *, browser: bool = False) -> dict | None:
    """Вход через ChatGPT в терминале: код устройства (или браузер), потом провайдер-подписка."""
    from ..router import chatgpt

    try:
        if not browser:
            try:
                with term.step("Прошу у OpenAI код входа"):
                    device = await chatgpt.device_start(app.http)
            except chatgpt.LoginError as e:
                if e.code != "chatgpt_no_device":
                    raise
                term.line(term.dim("  Вход по коду недоступен — открою вход через браузер."))
                browser = True
        if not browser:
            term.line()
            term.line(f"  1. Открой на любом устройстве: {term.amber(device['verification_url'], bold=True)}")
            term.line(f"  2. Войди в ChatGPT и введи код:  {term.amber(device['user_code'], bold=True)}")
            term.line(term.dim("     Код живёт 15 минут. Ctrl+C — отменить."))
            with term.step("Жду, пока подтвердишь вход"):
                tokens = await chatgpt.device_wait(app.http, device)
        else:
            flow = chatgpt.BrowserLogin(app.http)
            await flow.start()
            term.line()
            term.line("  Открой ссылку и войди в ChatGPT:")
            term.line("  " + term.amber(flow.url))
            try:
                import webbrowser

                webbrowser.open(flow.url)
            except Exception:  # noqa: BLE001
                pass
            if not flow.listening:
                term.line(term.dim("  Порт 1455 занят — после входа скопируй адрес из адресной строки браузера и вставь сюда."))
            else:
                term.line(term.dim("  Если браузер на другом устройстве — после входа вставь сюда адрес из его адресной строки."))

            async def paste_loop():
                while not flow.code.done():
                    text = (await ainput("  адрес › ")).strip()
                    if text and not flow.paste(text):
                        term.line(term.red("  Не похоже на адрес возврата из входа OpenAI."))

            paster = asyncio.ensure_future(paste_loop())
            try:
                tokens = await flow.wait()
            finally:
                paster.cancel()
    except HumanError as e:
        term.error(e)
        return None
    p = app.models.connect_chatgpt(tokens)
    app.journal.write("models", f"Вход через ChatGPT: {tokens.get('email') or tokens.get('account_id')}", actor="owner")
    term.line(term.green(f"  ✓ Вход выполнен: {tokens.get('email') or 'ChatGPT'}. Провайдер «{p['name']}»."))
    return p


async def connect_claude_code(app, term: Term) -> dict | None:
    """Claude Code на этой машине: проверить (найден, вошёл — без вызова модели), предупредить, подключить."""
    from ..router import cli

    term.line(term.dim("  Бендер запускает официальный Claude Code, в который ты сам вошёл. Свои тулы Claude Code выключены —"))
    term.line(term.dim("  руки только Бендера, через Предохранитель. Правила Anthropic про такое использование подписки"))
    term.line(term.dim("  в 2026 году менялись; по справке Anthropic (октябрь 2026) claude -p по подписке расходует её лимиты."))
    term.line(term.dim("  Вход — только твой собственный, не давай его другим. Держи в цепочке запасную модель."))
    cmd = ""
    while True:
        with term.step("Ищу claude"):
            st = await cli.check({"command": cmd} if cmd else {})
        if not st["found"]:
            term.line(term.amber("  Не нашёл claude. Поставь: npm install -g @anthropic-ai/claude-code (или установщик с claude.com)."))
            cmd = (await ainput("  Путь к claude (пусто — отмена): ")).strip()
            if not cmd:
                return None
            continue
        term.line(f"  Нашёл: {st['path']} · {st.get('version') or 'версия ?'}")
        if not st["logged_in"]:
            term.line(term.amber("  Claude Code не вошёл. В другом терминале: claude, потом /login."))
            if (await ainput("  Войду и нажму Enter (или «нет» — отмена): ")).strip().lower() in ("нет", "no", "n"):
                return None
            continue
        term.line(term.green("  ✓ Вошёл " + ("ключом API — ходы платные" if st["billing"] == "api" else "подпиской")))
        break
    extra = {"billing": st["billing"]} if st.get("billing") else {}
    if cmd:
        extra["command"] = cmd
    return app.models.add_provider(preset="claude_code", extra=extra)


async def setup_model(app, term: Term) -> bool:
    """Подключить провайдера и выбрать модель для Главной роли — вопросами."""
    from ..router.catalog import list_models
    from ..router.config import PRESETS

    providers = app.models.providers()
    items = [(pid, f"{p['name']} ({p['base_url'] or 'claude'})") for pid, p in providers.items()]
    items += [(f"new:{k}", f"+ {v['name']}") for k, v in PRESETS.items()] + [("new:custom", "+ свой адрес (OpenAI-совместимый)")]
    choice = await pick(term, "Откуда брать модель?", items)
    if not choice:
        return False
    if choice == "new:chatgpt":
        p = await login_chatgpt(app, term)
        if not p:
            return False
        from ..router import chatgpt

        model = await pick(term, "Какую модель взять Главной?", [(m["id"], m["id"] + (f" — {m['note']}" if m.get("note") else "")) for m in chatgpt.MODELS], allow_custom=True)
        app.models.set_role("main", [{"provider": p["id"], "model": model or chatgpt.DEFAULT_MODEL}])
        term.line(term.green(f"  ✓ Главная модель: {p['name']} · {model or chatgpt.DEFAULT_MODEL}"))
        return True
    if choice == "new:claude_code":
        p = await connect_claude_code(app, term)
        if not p:
            return False
        pid = p["id"]
    elif choice.startswith("new:"):
        preset = choice[4:]
        base_url = ""
        kind = "openai"
        if preset == "custom":
            base_url = (await ainput("  Адрес API (например http://127.0.0.1:20128/v1): ")).strip()
            preset = None
        elif PRESETS[preset].get("is_local"):
            url = (await ainput(f"  Адрес [{PRESETS[preset]['base_url']}]: ")).strip()
            base_url = url or ""
        key = ""
        if preset is None or not PRESETS.get(preset, {}).get("free"):
            try:
                key = getpass.getpass("  API-ключ (пусто — без ключа): ").strip()
            except (EOFError, KeyboardInterrupt):
                key = ""
        try:
            p = app.models.add_provider(preset=preset, base_url=base_url, api_key=key or None, kind=kind)
        except HumanError as e:
            term.error(e)
            return False
        pid = p["id"]
    else:
        pid = choice
    p = app.models.providers()[pid]
    keys = [app.secrets.get(n) for n in p.get("keys") or []]
    models = []
    try:
        with term.step(f"Спрашиваю у {p['name']} список моделей"):
            models = await list_models(app.http, p, keys[0] if keys else None)
        app.models.apply_catalog(pid,models)
    except HumanError as e:
        term.error(e)
    model = None
    if models:
        q = (await ainput(f"  Моделей: {len(models)}. Поиск (часть названия, «free» — бесплатные; пусто — все): ")).strip().lower()
        shown = [m for m in models if not q or q in m["id"].lower() or (q == "free" and m.get("free"))][:40]
        model = await pick(term, "Какую модель взять Главной?", [(m["id"], m["id"] + (" · бесплатно" if m.get("free") else "") + (f" · контекст {m['context']}" if m.get("context") else "")) for m in shown], allow_custom=True)
    if not model:
        model = (await ainput("  Название модели как у провайдера: ")).strip()
    if not model:
        return False
    app.models.set_role("main", [{"provider": pid, "model": model}])
    term.line(term.green(f"  ✓ Главная модель: {p['name']} · {model}"))
    return True


class TerminalChat:
    def __init__(self, app, term: Term):
        self.app = app
        self.term = term
        self.sid = app.main_session()
        self.pending: list[dict] = []  # карточки одобрения и вопросы, ждущие ответа
        self.streaming = False
        self.done = asyncio.Event()

    def on_event(self, ev: dict) -> None:
        if ev.get("agent_id", "main") not in (self.app.agent_id,'*'): return
        t = self.term
        typ = ev.get("type")
        if ev.get("session") not in (None, self.sid) and typ not in ("approval", "question"):
            return
        if typ == "delta":
            if ev.get("kind") == "text":
                if not self.streaming:
                    t.write("\n" + t.amber("◉ ") )
                    self.streaming = True
                t.write(ev.get("text", ""))
            elif ev.get("kind") == "reasoning" and not self.streaming:
                pass
        elif typ == "tool_start":
            self._end_stream()
            args = json.dumps(ev.get("arguments") or {}, ensure_ascii=False)
            t.line(t.dim(f"  ⚙ {ev.get('name')} {args[:160]}"))
        elif typ == "tool_end":
            mark = t.green("✓") if ev.get("ok") else t.red("✗")
            t.line(f"  {mark} " + t.dim(ev.get("summary") or ""))
        elif typ == "approval":
            self._end_stream()
            c = ev["card"]
            self.pending.append({"kind": "approval", "card": c})
            t.line()
            t.line(t.amber("  ⚠ Предохранитель спрашивает: ", bold=True) + (c.get("summary") or c.get("what")))
            t.line(t.dim(f"    {c.get('reason')}"))
            if c.get("tainted"):
                t.line(t.red("    В этом ходе агент читал недоверенное."))
            t.line("    1 — один раз · 2 — в этом разговоре · " + (f"3 — всегда: {c.get('remember') or 'такое'} · " if c.get("can_remember") else "") + "0 — нет")
        elif typ == "question":
            self._end_stream()
            c = ev["card"]
            self.pending.append({"kind": "question", "card": c})
            t.line()
            t.line(t.amber("  ❓ ", bold=True) + c["question"])
            for i, o in enumerate(c.get("options") or [], 1):
                t.line(f"    {i}. {o}")
        elif typ in ("approval_done", "question_done"):
            self.pending = [p for p in self.pending if p["card"]["id"] != ev.get("id")]
        elif typ == "router":
            if ev.get("kind") == "retry":
                t.line(t.dim(f"  … повторяю ({ev.get('candidate')}, {ev.get('class')}, через {ev.get('wait')} с)"))
            elif ev.get("kind") == "fallback":
                t.line(t.dim(f"  … переключаюсь на {ev.get('to')}"))
        elif typ == "message":
            m = ev.get("message") or {}
            if m.get("role") == "event":
                self._end_stream()
                t.line(t.dim("  · " + str(m.get("content"))))
                if m.get("kind")=="skill" and (m.get("data") or {}).get("name"):
                    t.line(t.dim("    Убрать: /убрать-навык "+m["data"]["name"]))
        elif typ == "turn_end":
            self._end_stream()
            err = (ev.get("result") or {}).get("error")
            if err and err.get("code") not in ("stopped",):
                t.error(HumanError(err.get("code", "error"), err.get("title", "Ошибка"), err.get("hint", ""), detail=err.get("detail")))
            self.done.set()
        elif typ == "memory" and ev.get("action") != "review":  # итог разбора придёт строкой «💾 Запомнил»
            t.line(t.dim(f"  💾 {ev.get('file')}: {ev.get('text', '')[:80]}"))

    def _end_stream(self) -> None:
        if self.streaming:
            self.term.write("\n")
            self.streaming = False

    async def answer_pending(self, text: str) -> bool:
        if not self.pending:
            return False
        item = self.pending.pop(0)
        c = item["card"]
        if item["kind"] == "approval":
            choice = {"1": "once", "2": "task", "3": "always", "0": "deny", "н": "deny", "нет": "deny", "да": "once"}.get(text.strip().lower(), "deny")
            self.app.guard.respond(c["id"], choice)
        else:
            opts = c.get("options") or []
            if text.strip().isdigit() and 1 <= int(text) <= len(opts):
                text = opts[int(text) - 1]
            self.app.answer(c["id"], text)
        return True

    async def run(self) -> int:
        t = self.term
        app = self.app
        app.bus.bind_loop(asyncio.get_running_loop())
        app.host.bus.listen(self.on_event)
        if not app.models.roles().get("main"):
            t.line("  Сначала подключим модель — без неё агенту нечем думать.")
            if not await setup_model(app, t):
                t.line("  Без модели никак. Запусти ещё раз, когда будет ключ или локальная модель (или `bender fake-llm` для проверки).")
                return 1
        ident = app.selfdir.identity()
        who = ident.get("name") or "агентом (у него ещё нет имени)"
        t.line(t.dim(f"  Разговор с {who}. /помощь — команды. Ctrl+D — выход."))
        if not app.selfdir.born():
            t.line(t.dim("  Агент только что проснулся. Напиши ему что-нибудь — он начнёт знакомство."))
        while True:
            try:
                prompt = "  ответ › " if self.pending else "\n› "
                line = await ainput(prompt)
            except (EOFError, KeyboardInterrupt):
                t.line()
                break
            text = line.strip()
            if not text:
                continue
            if text.startswith("/убрать-навык "):
                try:
                    result=await app.mind.say(self.sid,text,channel="терминал",actor="owner")
                    t.line(t.dim(result.get("text", "")))
                except HumanError as exc:t.error(exc)
                continue
            if await self.answer_pending(text):
                continue
            if text == '/agent' or text.startswith('/agent '):
                choices=[(a['id'],a['name']) for a in app.agents.public() if not a['archived']]
                value=text.partition(' ')[2].strip() or await pick(t,'Выбрать агента',choices)
                if value:
                    try:
                        app=self.app=app.host.agents.get(app.agents.resolve(value));self.sid=app.main_session();self.pending.clear()
                        t.line('  Агент: '+(app.selfdir.identity().get('name') or app.agent_id))
                    except HumanError as exc:t.error(exc)
                continue
            if text == '/chats':
                choices=[(s['id'],s['title']) for s in app.store.sessions() if s['kind'] in ('main','chat') and not s['meta'].get('archived')]
                value=await pick(t,'Выбрать чат',choices)
                if value:self.sid=value
                continue
            if text in ("/выход", "/exit", "/quit"):
                break
            if text in ("/помощь", "/help"):
                t.line(HELP)
                continue
            if text in ("/стоп", "/stop"):
                n = app.mind.stop()
                t.line(t.amber(f"  Остановил ходов: {n}. Можно писать дальше.") if n else "  Сейчас ничего не идёт.")
                continue
            if text in ("/estop", "/стоп-кран"):
                app.halt()
                t.line(t.red("  Стоп-кран: всё остановлено, агенты не действуют. /продолжить — снять."))
                continue
            if text in ("/продолжить", "/resume"):
                app.guard.resume()
                t.line(t.green("  Стоп снят."))
                continue
            if text in ("/новая", "/new"):
                from ..mind import compact

                ok = compact.new_page(app, app.main_session(), "хозяин попросил в терминале")
                t.line(t.green("  📄 Новая страница: разговор с моделью начнётся заново, память остаётся.") if ok else t.dim("  Страница и так чистая."))
                continue
            if text in ("/модель", "/model"):
                await setup_model(app, t)
                continue
            if text.split(maxsplit=1)[0:1]==['/effort']:
                from ..router.effort import command
                try:t.line(await command(app,self.sid,text.partition(' ')[2].strip()))
                except HumanError as exc:t.error(exc)
                continue
            if text in ("/промпт", "/prompt"):
                from ..mind.context import system_prompt

                t.line(system_prompt(app))
                continue
            self.done.clear()
            await app.mind.say(self.sid, text, channel="терминал", wait=False)
            while not self.done.is_set():
                if self.pending:
                    try:
                        ans = await ainput("  ответ › ")
                    except (EOFError, KeyboardInterrupt):
                        app.halt()
                        break
                    await self.answer_pending(ans)
                else:
                    try:
                        await asyncio.wait_for(self.done.wait(), 0.2)
                    except asyncio.TimeoutError:
                        pass
        return 0


def main(args) -> int:
    from .. import __version__
    from ..core.app import App

    term = Term(animations=not getattr(args, "no_anim", False))
    term.banner(__version__)

    async def run():
        app = App(args.data or default_data_dir())
        try:
            ctx=app.agents.get(app.agents.resolve(getattr(args,"agent","main")))
            return await TerminalChat(ctx, term).run()
        finally:
            await app.close()

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        return 0


def login_main(args) -> int:
    from .. import __version__
    from ..core.app import App

    term = Term(animations=not getattr(args, "no_anim", False))
    term.banner(__version__)

    async def run():
        app = App(args.data or default_data_dir())
        app.bus.bind_loop(asyncio.get_running_loop())
        try:
            p = await login_chatgpt(app, term, browser=args.browser)
            if p:
                main = app.models.roles().get("main") or []
                term.line(f"  Главная модель: {main[0]['model'] if main else '—'} · сменить — в пульте («Модели») или bender try setup")
            return 0 if p else 1
        finally:
            await app.close()

    try:
        return asyncio.run(run())
    except KeyboardInterrupt:
        term.line()
        return 130

