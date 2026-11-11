"""Claude Code как движок (фальшивый `claude`): поток, продолжение разговора, тулы Бендера через петлевой MCP,
Предохранитель и заражение, ошибки и запасная модель."""

import json
import os
import sys
from pathlib import Path

from bender.router import cli as clipkg
from bender.router.cli import _classify, prompt_for
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv

FAKE = Path(__file__).parent / "fixtures" / "fake_claude.py"


class ClaudeCodeTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.log = self.tmp / "claude.log"
        os.environ["FAKE_CLAUDE_LOG"] = str(self.log)
        os.environ.pop("FAKE_CLAUDE", None)

    def tearDown(self):
        for k in ("FAKE_CLAUDE_LOG", "FAKE_CLAUDE", "FAKE_CLAUDE_OLD"):
            os.environ.pop(k, None)
        clipkg._FLAGS.clear()
        super().tearDown()

    def plan(self, **kw):
        os.environ["FAKE_CLAUDE"] = json.dumps(kw, ensure_ascii=False)

    def runs(self):
        return [json.loads(x) for x in self.log.read_text(encoding="utf-8").splitlines()] if self.log.exists() else []

    def cc(self, env, command=None, fallback=True):
        app = env.app
        p = app.models.add_provider(preset="claude_code", extra={"command": command or [sys.executable, str(FAKE)]})
        chain = [{"provider": p["id"], "model": "default"}]
        if fallback:
            chain.append({"provider": app.models.roles()["main"][0]["provider"], "model": "echo"})
        app.models.set_role("main", chain)
        return p

    @async_test
    async def test_chosen_effort_is_not_overridden_by_environment(self):
        from unittest import mock
        async with MindEnv(self.tmp) as env:
            self.cc(env,fallback=False)
            with mock.patch.dict(os.environ,{'CLAUDE_CODE_EFFORT_LEVEL':'low'}):
                env.app.settings.set('reasoning',{'effort':'max'})
                self.plan(text='Выбор Бендера.')
                await env.app.router.call([{'role':'user','content':'Проверка'}])
                run=self.runs()[-1]
                self.assertEqual(run['argv'][run['argv'].index('--effort')+1],'max')
                self.assertNotIn('CLAUDE_CODE_EFFORT_LEVEL',run['env'])  # у Claude Code переменная главнее флага
                env.app.settings.set('reasoning',{'effort':'default'})
                self.plan(text='Выбор хозяина Claude Code.')
                await env.app.router.call([{'role':'user','content':'Проверка'}])
                run=self.runs()[-1]
                self.assertNotIn('--effort',run['argv'])
                self.assertEqual(run['env'].get('CLAUDE_CODE_EFFORT_LEVEL'),'low')

    def test_try_claude_reports_version_login_and_flags(self):
        import shutil
        import subprocess
        root=Path(__file__).resolve().parents[1]
        env={**os.environ,'PYTHONPATH':str(root)}
        env.pop('FAKE_CLAUDE_OLD',None)
        fake=self.tmp/'папка с пробелом'/'fake_claude.py'  # путь к программе — списком, без склейки строкой
        fake.parent.mkdir();shutil.copy(FAKE,fake)
        cmd=[sys.executable,'-m','bender','--data',str(self.tmp/'data'),'--no-anim','try','claude',sys.executable,str(fake)]
        done=subprocess.run(cmd,cwd=root,env=env,capture_output=True,text=True,timeout=60)
        self.assertEqual(done.returncode,0,done.stdout+done.stderr)
        self.assertIn('9.9.9',done.stdout);self.assertIn('все нужные есть',done.stdout)
        env['FAKE_CLAUDE_OLD']='1'
        done=subprocess.run(cmd,cwd=root,env=env,capture_output=True,text=True,timeout=60)
        self.assertEqual(done.returncode,0,done.stdout+done.stderr)
        self.assertIn('обойдусь без',done.stdout);self.assertIn('ход с выбранным усилием не пойдёт',done.stdout)

    @async_test
    async def test_native_effort_reaches_cli_and_old_cli_fails_before_turn(self):
        async with MindEnv(self.tmp) as env:
            provider=self.cc(env,fallback=False)
            env.app.settings.set('reasoning',{'effort':'high'})
            self.plan(text='С усилием.')
            res=await env.app.router.call([{'role':'user','content':'Проверка'}])
            self.assertEqual(res.text,'С усилием.')
            argv=self.runs()[-1]['argv']
            self.assertEqual(argv[argv.index('--effort')+1],'high')
            self.assertEqual(argv[argv.index('--tools')+1],'')
            self.assertIn('--strict-mcp-config',argv)
            count=len(self.runs());os.environ['FAKE_CLAUDE_OLD']='1';clipkg._FLAGS.clear()
            from bender.router.errors import ProviderError
            with self.assertRaises(ProviderError) as error:
                await env.app.router.call([{'role':'user','content':'Не запускать'}])
            self.assertIn('--effort',error.exception.message)
            self.assertEqual(len(self.runs()),count)
            self.assertEqual(env.app.router.cli_bridge.bindings,{})

    @async_test
    async def test_turn_stream_and_resume(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            p = self.cc(env)
            deltas = []
            app.bus.listen(lambda e: deltas.append(e["text"]) if e.get("type") == "delta" and e.get("kind") == "text" else None)
            self.plan(text="Привет, я через Claude Code.")
            res = await app.mind.say(env.sid, "привет")
            self.assertIsNone(res["error"])
            self.assertEqual(res["text"], "Привет, я через Claude Code.")
            self.assertEqual("".join(deltas), "Привет, я через Claude Code.")  # поток по кусочкам
            r1 = self.runs()[0]
            argv = r1["argv"]
            self.assertEqual(argv[argv.index("--tools") + 1], "")  # свои тулы Claude Code выключены
            self.assertIn("--strict-mcp-config", argv)
            self.assertIn("--session-id", argv)
            self.assertEqual(argv[argv.index("--system-prompt-snapshot") + 1], "off")
            self.assertIn("--restricted", argv)  # без хуков и настроек хозяина
            self.assertIn("Как ты устроен", argv[argv.index("--system-prompt") + 1])  # личность и правила Бендера
            # как у API-моделей: время, канал и метка реплики — без них Claude лез за временем в шелл
            self.assertRegex(r1["prompt"], r"^\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}, \w+ · канал: [^\]]+\]\n\[(?:пн|вт|ср|чт|пт|сб|вс) \d{2}\.\d{2} \d{2}:\d{2}\]\nпривет$")
            self.assertIn("mcp__bender__recall", argv[argv.index("--system-prompt") + 1])  # имена тулов, как их видит Claude Code
            self.assertEqual(r1["env"].get("CLAUDE_CODE_DISABLE_CLAUDE_MDS"), "1")  # чужие CLAUDE.md не подмешиваются
            self.assertEqual(r1["server"]["name"], "bender")
            self.assertIn("write_file", r1["tools"])
            # второй ход — продолжение той же сессии и только новое сообщение
            self.plan(text="Помню.")
            await app.mind.say(env.sid, "помнишь меня?")
            r2 = self.runs()[1]
            self.assertTrue(r2["resume"])
            self.assertEqual(r2["session"], r1["session"])
            self.assertTrue(r2["prompt"].endswith("]\nпомнишь меня?"))
            self.assertNotIn("привет", r2["prompt"])  # только новое
            self.assertEqual(app.store.session(env.sid)["meta"][f"cc:{p['id']}"]["session"], r1["session"])
            call = app.store.q("SELECT * FROM calls ORDER BY created_at DESC LIMIT 1")[0]
            self.assertEqual(call["cost"], 0)  # подписка — ход денег не стоит

    @async_test
    async def test_tools_through_mcp_and_guard(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            self.cc(env)
            env.auto_approve("deny")
            outside = self.tmp / "вне.txt"
            self.plan(tools=[{"name": "write_file", "arguments": {"path": "cc.txt", "content": "из Claude Code"}},
                             {"name": "write_file", "arguments": {"path": str(outside), "content": "нельзя"}},
                             {"name": "web_fetch", "arguments": {"url": env.base + "/page"}}], text="Сделал, что смог.")
            res = await app.mind.say(env.sid, "запиши файлы и прочитай страницу")
            self.assertEqual(res["text"], "Сделал, что смог.")
            self.assertEqual((app.dirs.work / "cc.txt").read_text(encoding="utf-8"), "из Claude Code")
            self.assertFalse(outside.exists())  # Предохранитель спросил, хозяин отказал
            self.assertEqual(env.approvals[0]["capability"], "fs.write")
            results = self.runs()[0]["results"]
            self.assertEqual([r["isError"] for r in results], [False, True, False])
            self.assertIn("НЕДОВЕРЕННОЕ", results[2]["text"])
            self.assertTrue(res["tainted"])  # прочитал сеть через MCP — ход заражён
            events = [m["content"] for m in app.store.messages(env.sid) if m.get("kind") == "tool"]
            self.assertEqual(len(events), 3)
            self.assertTrue(events[0].startswith("⚙ write_file"))
            self.assertEqual(app.router.cli_bridge.bindings, {})  # токен хода закрыт

    @async_test
    async def test_errors_fall_back(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            self.cc(env)
            self.plan(error="Not logged in · Please run /login")
            res = await app.mind.say(env.sid, "ау")
            self.assertIsNone(res["error"])
            self.assertTrue(res["text"].startswith("Эхо:"))  # ответила запасная
            notices = [m["content"] for m in app.store.messages(env.sid) if m.get("kind") == "notice"]
            self.assertTrue(any("запасная" in n for n in notices))
        async with MindEnv(self.tmp / "2") as env:
            self.cc(env, command=["нет-такого-claude"], fallback=False)
            res = await env.app.mind.say(env.sid, "ау")
            self.assertIn("Не нашёл", json.dumps(res["error"], ensure_ascii=False))

    @async_test
    async def test_claude_code_sees_images(self):
        import base64
        import struct
        import zlib

        from bender.router.capabilities import description

        def png():
            def chunk(t, d):
                return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
            return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0))
                    + chunk(b"IDAT", zlib.compress(b"\0" + b"\xff\0\0" * 2 + b"\0" + b"\xff\0\0" * 2)) + chunk(b"IEND", b""))

        raw = png()
        data = base64.b64encode(raw).decode()
        self.assertTrue(description({"kind": "claude_code"}, "default", "vision")["value"])
        self.assertFalse(description({"kind": "claude_code"}, "default", "audio_input")["value"])
        self.assertFalse(description({"kind": "codex_cli"}, "default", "vision")["value"])
        async with MindEnv(self.tmp) as env:
            app = env.app
            self.cc(env)
            (app.dirs.work / "кадр.png").write_bytes(raw)
            self.plan(tools=[{"name": "read_file", "arguments": {"path": "кадр.png"}}], text="Красный квадрат.")
            content = [{"type": "text", "text": "что на фото?"}, {"type": "image", "media_type": "image/png", "data": data, "path": "/inbox/фото.png"}]
            res = await app.mind.say(env.sid, "что на фото?", content=content)
            self.assertEqual(res["text"], "Красный квадрат.")
            run = self.runs()[0]
            self.assertEqual(run["images"], ["image/png"])  # фото хозяина — изображением, не текстом
            self.assertIn("--input-format", run["argv"])
            self.assertIn("Картинка 1 приложена ниже. Файл: /inbox/фото.png", run["prompt"])
            self.assertNotIn(data, run["prompt"])
            self.assertEqual(run["results"][0]["images"], ["image/png"])  # read_file картинки — изображением через MCP
            self.assertNotIn(data, run["results"][0]["text"])
            res = await app.mind.say(env.sid, "а теперь просто текст")
            run = self.runs()[1]
            self.assertEqual(run["images"], [])
            self.assertNotIn("--input-format", run["argv"])  # без картинок — обычный текст, как раньше
        clipkg._FLAGS.clear()
        os.environ["FAKE_CLAUDE_OLD"] = "1"
        async with MindEnv(self.tmp / "старый") as env:
            self.cc(env)
            self.plan(text="Ок")
            await env.app.mind.say(env.sid, "фото", content=[{"type": "text", "text": "фото"}, {"type": "image", "media_type": "image/png", "data": data}])
            run = self.runs()[-1]
            self.assertNotIn("--input-format", run["argv"])
            self.assertIn("обнови CLI", run["prompt"])
            self.assertNotIn(data, run["prompt"])

    @async_test
    async def test_without_tools_is_one_shot(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            p = self.cc(env, fallback=False)
            self.plan(text="Да")
            res = await app.router.call([{"role": "system", "content": "Будь краток"}, {"role": "user", "content": "Работаешь?"}], role="main")
            self.assertEqual(res.text, "Да")
            r = self.runs()[0]
            self.assertIn("--no-session-persistence", r["argv"])
            self.assertNotIn("--mcp-config", r["argv"])
            self.assertEqual(r["prompt"], "Работаешь?")
            self.assertEqual(r["tools"], [])
            self.assertNotIn(f"cc:{p['id']}", (app.store.session(env.sid) or {}).get("meta", {}))

    @async_test
    async def test_check_and_connect_from_pult(self):
        import shlex

        from bender.router import cli

        cmd = shlex.join([sys.executable, str(FAKE)])
        st = await cli.check({"command": cmd})
        self.assertTrue(st["found"])
        self.assertEqual(st["version"], "9.9.9 (Claude Code)")
        self.assertTrue(st["logged_in"])
        self.assertEqual(st["billing"], "subscription")
        self.plan(auth="api_key")
        self.assertEqual((await cli.check({"command": cmd}))["billing"], "api")
        self.plan(auth="")
        self.assertFalse((await cli.check({"command": cmd}))["logged_in"])
        self.assertFalse((await cli.check({"command": "нет-такого-claude"}))["found"])
        async with MindEnv(self.tmp) as env:
            app = env.app
            p = app.models.add_provider(preset="claude_code", extra={"command": cmd, "billing": "api"})
            self.assertFalse(p["subscription"])
            self.assertFalse(app.router.is_free(p, "opus"))  # ключ API — режим бомжа его не берёт
            p2 = app.models.add_provider(preset="claude_code")
            self.assertTrue(app.router.is_free(p2, "opus"))
            # каталог из пульта: список алиасов, адрес не нужен
            from bender.router.catalog import apply_catalog, list_models

            ms = await list_models(app.http, p2, None)
            self.assertEqual([m["id"] for m in ms], ["default", "opus", "sonnet", "fable", "haiku"])
            self.assertTrue(all(m["subscription"] for m in ms))
            app.models.update_provider(p2["id"], apply_catalog(p2, ms))
            self.assertFalse(any(m["subscription"] for m in await list_models(app.http, p, None)))

    @async_test
    async def test_terminal_setup(self):
        import io
        import shlex
        from unittest import mock

        from bender.channels import terminal
        from bender.core.termui import Term

        cmd = shlex.join([sys.executable, str(FAKE)])
        answers = iter([cmd])  # сначала claude не найден — даём путь
        out = io.StringIO()
        async with MindEnv(self.tmp) as env:
            app = env.app
            with mock.patch.object(terminal, "ainput", side_effect=lambda *_: next(answers)), \
                 mock.patch.object(clipkg, "command",
                                   side_effect=lambda p: shlex.split(p.get("command") or "нет-такого-claude")):
                p = await terminal.connect_claude_code(app, Term(out))
            self.assertEqual(p["kind"], "claude_code")
            self.assertEqual(p["command"], cmd)
            self.assertIn("Вошёл подпиской", out.getvalue())
            self.assertIn("Правила Anthropic", out.getvalue())

    @async_test
    async def test_old_claude_without_new_flags(self):
        os.environ["FAKE_CLAUDE_OLD"] = "1"
        clipkg._FLAGS.clear()
        async with MindEnv(self.tmp) as env:
            self.cc(env, fallback=False)
            self.plan(text="Старый, но рабочий.")
            res = await env.app.mind.say(env.sid, "привет")
            self.assertIsNone(res["error"])
            self.assertEqual(res["text"], "Старый, но рабочий.")
            argv = self.runs()[0]["argv"]
            self.assertNotIn("--system-prompt-snapshot", argv)
            self.assertIn("--mcp-config", argv)

    def test_classify_and_prompt(self):
        self.assertEqual(_classify("Claude AI usage limit reached|1792700000"), "rate_limit")
        self.assertEqual(_classify("Invalid API key · Please run /login"), "auth")
        self.assertEqual(_classify("Prompt is too long"), "context")

    @async_test
    async def test_first_prompt_carries_earlier_history(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            app.store.add_message(env.sid, {"role": "user", "content": "меня зовут Миша"})
            app.store.add_message(env.sid, {"role": "assistant", "content": "Приятно познакомиться"})
            app.store.add_message(env.sid, {"role": "user", "content": "как меня зовут?"})
            text, last = prompt_for(app, env.sid, {})
            self.assertIn("шло без тебя", text)
            self.assertIn("меня зовут Миша", text)
            self.assertTrue(text.endswith("как меня зовут?"))

    @async_test
    async def test_first_prompt_starts_at_new_page(self):
        from bender.mind import compact

        async with MindEnv(self.tmp) as env:
            app = env.app
            app.store.add_message(env.sid, {"role": "user", "content": "СТАРОЕ: меня зовут Миша"})
            app.store.add_message(env.sid, {"role": "assistant", "content": "Приятно познакомиться"})
            compact.new_page(app, env.sid)
            app.store.add_message(env.sid, {"role": "user", "content": "привет"})
            text, _ = prompt_for(app, env.sid, {})
            self.assertRegex(text, r"^\[\w\w \d\d\.\d\d \d\d:\d\d\]\nпривет$")
            app.store.update_session(env.sid, meta={"compact": {**compact.state(app, env.sid), "summary": "Хозяин — Миша."}})
            text, _ = prompt_for(app, env.sid, {})
            self.assertTrue(text.startswith("[Пересказ начала разговора:\nХозяин — Миша.]"))
            self.assertNotIn("СТАРОЕ", text)
