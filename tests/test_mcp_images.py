"""Картинки из результатов MCP-тулов: файлом в inbox и отдельным сообщением модели, без base64 в тексте."""
import base64
import json
import os
import sys
from pathlib import Path

from bender.core.app import App
from bender.mcp.manager import tool_name
from bender.mcp.parts import render
from bender.router import cli as clipkg
from bender.router.probes import png
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv, call
from tests import test_chatgpt as subscription
from tests import test_mcp_background as background

FAKE = Path(__file__).parent / "fixtures" / "fake_mcp.py"
CLAUDE = Path(__file__).parent / "fixtures" / "fake_claude.py"
RAW = png((255, 0, 0))
DATA = base64.b64encode(RAW).decode()
AUDIO = base64.b64encode(b"RIFF" + b"\0" * 300).decode()
ALIAS = tool_name("local", "echo")


class McpImagesTest(TempDirCase):
    def setUp(self):
        super().setUp()
        folder = self.tmp / "server"
        folder.mkdir()
        self.state = folder / "mcp-state.json"
        self.state.write_text(json.dumps({"result_content": [
            {"type": "text", "text": "Снимок экрана:"},
            {"type": "image", "data": DATA, "mimeType": "image/png"},
            {"type": "audio", "data": AUDIO, "mimeType": "audio/wav"},
            {"type": "resource", "resource": {"uri": "file:///x.bin", "mimeType": "application/octet-stream", "blob": AUDIO}}]}), encoding="utf-8")

    async def attach(self, app):
        app.guard.add_rule("mcp.call", "local/*", "allow", only_clean=False)
        app.guard.add_rule("mcp.connect", "local/*", "allow", only_clean=False)
        await app.mcp.save({"id": "local", "transport": "stdio", "enabled": True, "cwd": str(self.state.parent),
                            "command": [sys.executable, str(FAKE), str(self.state)]})
        panel = await app.mcp.discover("local", ToolContext(app, actor="owner"))
        app.mcp.approve("local", panel["servers"][0]["review"])

    def check_tool_text(self, app, sid):
        tool = next(m for m in app.store.messages(sid) if m["role"] == "tool")
        self.assertTrue(tool["meta"]["ok"], tool["content"])
        self.assertNotIn(DATA, tool["content"])
        self.assertNotIn(AUDIO, tool["content"])
        self.assertIn("[картинка 1 приложена отдельным сообщением; файл: ", tool["content"])
        self.assertIn("[двоичные данные audio/wav, 304 байт опущены]", tool["content"])
        self.assertIn("[двоичные данные application/octet-stream, 304 байт опущены]", tool["content"])
        files = list((app.dirs.work / "inbox").rglob("*.png"))
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].read_bytes(), RAW)
        self.assertIn(str(files[0]), tool["content"])
        item = next(i for i in app.store.meta_get("attachments:files", {}).values() if i["path"] == str(files[0]))
        self.assertEqual((item["source"], item["kind"]), ("mcp", "photo"))
        media = next(m for m in app.store.messages(sid) if m["meta"].get("tool_media"))
        self.assertTrue(media["meta"]["untrusted"])
        self.assertEqual(media["meta"]["source"], "mcp")
        image = next(p for p in media["content"] if p["type"] == "image")
        self.assertEqual((image["path"], image["source"], image["data"]), (str(files[0]), "mcp", DATA))
        return files[0]

    @async_test
    async def test_openai_and_anthropic_get_mcp_image_after_tool_results(self):
        for kind in ("openai", "anthropic"):
            with self.subTest(kind=kind):
                async with MindEnv(self.tmp / kind) as env:
                    app = env.app
                    app.models.update_provider(app.models.roles()["main"][0]["provider"], {"kind": kind})
                    await self.attach(app)
                    env.script([{"tool_calls": [{"name": ALIAS, "arguments": {"text": "снимок"}}, {"name": "list_dir", "arguments": {}}]},
                                {"text": "Красный экран."}])
                    result = await app.mind.say(env.sid, "Сделай снимок")
                    self.assertIsNone(result["error"], result)
                    self.assertTrue(result["tainted"])
                    self.assertEqual([m["role"] for m in app.store.messages(env.sid)], ["user", "assistant", "tool", "tool", "user", "assistant"])
                    self.check_tool_text(app, env.sid)
                    request = [r for r in env.fake.state.requests if r.get("api") == kind][-1]["body"]["messages"]
                    if kind == "openai":
                        self.assertEqual([m["role"] for m in request[-3:]], ["tool", "tool", "user"])
                        self.assertNotIn(DATA, json.dumps(request[-3:-1]))
                        part = next(p for p in request[-1]["content"] if p["type"] == "image_url")
                        self.assertEqual(base64.b64decode(part["image_url"]["url"].split(",")[1]), RAW)
                    else:
                        blocks = request[-1]["content"]
                        types = [p["type"] for p in blocks]
                        self.assertEqual(types[:2], ["tool_result", "tool_result"])
                        self.assertGreater(types.index("image"), 1)
                        self.assertEqual(next(p for p in blocks if p["type"] == "image")["source"]["data"], DATA)
                    self.assertIn("недоверенные данные", json.dumps(request, ensure_ascii=False))

    @async_test
    async def test_chatgpt_responses_gets_mcp_image_after_function_outputs(self):
        async with subscription.Env(self.tmp) as env:
            await env.login()
            env.use("script")
            app = env.app
            sid = app.main_session()
            await self.attach(app)
            env.fake.state.script = [call(ALIAS, text="снимок"), {"text": "Красный."}]
            result = await app.mind.say(sid, "Снимок")
            self.assertIsNone(result["error"], result)
            self.check_tool_text(app, sid)
            items = [r for r in env.fake.state.requests if r.get("api") == "chatgpt"][-1]["body"]["input"]
            output = max(i for i, item in enumerate(items) if item.get("type") == "function_call_output")
            image = next(i for i, item in enumerate(items) if any(p.get("type") == "input_image" for p in item.get("content", []) if isinstance(p, dict)))
            self.assertGreater(image, output)
            self.assertNotIn(DATA, json.dumps(items[output]))

    @async_test
    async def test_old_mcp_images_become_paths(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            app.settings.set("attachments", {"history_turns": 1})
            await self.attach(app)
            env.script([call(ALIAS, text="снимок"), {"text": "Вижу."}])
            await app.mind.say(env.sid, "Снимок")
            path = self.check_tool_text(app, env.sid)
            await app.mind.say(env.sid, "Следующий ход")
            sent = json.dumps(env.fake.state.requests[-1]["body"]["messages"], ensure_ascii=False)
            self.assertNotIn("image_url", sent)
            self.assertNotIn(DATA, sent)
            self.assertIn(str(path), sent)

    @async_test
    async def test_turn_limit_is_shared_with_read_file(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            app.settings.set("attachments", {"read_files_per_turn": 1})
            await self.attach(app)
            (app.dirs.work / "мой.png").write_bytes(png((0, 0, 255)))
            env.script([call("read_file", path="мой.png"), call(ALIAS, text="снимок"), {"text": "Хватит."}])
            result = await app.mind.say(env.sid, "Открой и сними")
            self.assertIsNone(result["error"], result)
            tools = [m for m in app.store.messages(env.sid) if m["role"] == "tool"]
            self.assertTrue(tools[1]["meta"]["ok"])
            self.assertIn("[картинка 1 не передана: лимит медиафайлов за ход]", tools[1]["content"])
            self.assertNotIn(DATA, tools[1]["content"])
            self.assertFalse(list((app.dirs.work / "inbox").rglob("*.png")))
            media = [m for m in app.store.messages(env.sid) if m["meta"].get("tool_media")]
            self.assertEqual(len(media), 1)
            self.assertEqual(media[0]["meta"]["source"], "read_file")
            # следующий ход — счётчик заново
            env.script([call(ALIAS, text="снимок"), {"text": "Есть."}])
            await app.mind.say(env.sid, "Ещё раз")
            self.assertIn("приложена отдельным сообщением", [m for m in app.store.messages(env.sid) if m["role"] == "tool"][-1]["content"])

    @async_test
    async def test_switched_off_broken_and_unsupported_never_send_bytes(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            ctx = ToolContext(app, session_id=env.sid)
            app.settings.set("attachments", {"vision": False})
            text, parts = await render(ctx, [{"type": "image", "data": DATA, "mimeType": "image/png"}], origin="local/echo")
            self.assertEqual((text, parts), ("[картинка 1 не передана: передача картинок модели выключена в настройках]", []))
            app.settings.set("attachments", {"vision": True, "enabled": False})
            text, parts = await render(ctx, [{"type": "image", "data": DATA, "mimeType": "image/png"}], origin="local/echo")
            self.assertEqual((text, parts), ("[картинка 1 не передана: вложения выключены в настройках]", []))
            app.settings.set("attachments", {"enabled": True})
            text, parts = await render(ctx, [{"type": "image", "data": "не base64!", "mimeType": "image/png"},
                                             {"type": "image", "data": base64.b64encode(b"just text").decode(), "mimeType": "image/png"},
                                             {"type": "image", "data": DATA, "mimeType": "image/svg+xml"}], origin="local/echo")
            self.assertEqual(parts, [])
            self.assertEqual(text.splitlines()[:2], ["[картинка 1 не передана: данные повреждены]", "[картинка 2 не передана: данные повреждены]"])
            self.assertIn("[картинка 3 не передана: формат image/svg+xml не поддерживается", text)
            self.assertNotIn(DATA, text)
            self.assertFalse(ctx.turn.get("read_media", {}).get("files"))
            # ошибка тула: картинка только меткой
            text, parts = await render(ctx, [{"type": "image", "data": DATA, "mimeType": "image/png"}], origin="local/echo", attach=False)
            self.assertEqual((text, parts), (f"[двоичные данные image/png, {len(RAW)} байт опущены]", []))
            self.assertFalse((app.dirs.work / "inbox").exists())
            # через настоящий сервер с выключенной передачей — в модель не уходит ни картинка, ни base64
            app.settings.set("attachments", {"vision": False})
            await self.attach(app)
            env.script([call(ALIAS, text="снимок"), {"text": "Не вижу."}])
            result = await app.mind.say(env.sid, "Снимок")
            self.assertTrue(result["tainted"])
            sent = json.dumps(env.fake.state.requests[-1]["body"]["messages"], ensure_ascii=False)
            self.assertNotIn(DATA, sent)
            self.assertNotIn("image_url", sent)
            self.assertIn("передача картинок модели выключена", sent)
            self.assertFalse([m for m in app.store.messages(env.sid) if m["meta"].get("tool_media")])

    @async_test
    async def test_claude_code_engine_gets_mcp_image_block(self):
        log = self.tmp / "claude.log"
        os.environ["FAKE_CLAUDE_LOG"] = str(log)
        try:
            async with MindEnv(self.tmp / "cc") as env:
                app = env.app
                await self.attach(app)
                p = app.models.add_provider(preset="claude_code", extra={"command": [sys.executable, str(CLAUDE)]})
                app.models.set_role("main", [{"provider": p["id"], "model": "default"}])
                os.environ["FAKE_CLAUDE"] = json.dumps({"tools": [{"name": ALIAS, "arguments": {"text": "снимок"}}], "text": "Красный."}, ensure_ascii=False)
                result = await app.mind.say(env.sid, "Снимок")
                self.assertEqual(result["text"], "Красный.", result)
                run = json.loads(log.read_text(encoding="utf-8").splitlines()[-1])
                self.assertIn(ALIAS, run["tools"])
                self.assertEqual(run["results"][0]["images"], ["image/png"])
                self.assertNotIn(DATA, run["results"][0]["text"])
                self.assertTrue(result["tainted"])
        finally:
            for key in ("FAKE_CLAUDE_LOG", "FAKE_CLAUDE"):
                os.environ.pop(key, None)
            clipkg._FLAGS.clear()


class McpTaskImagesTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.app = App(self.tmp / "data")
        folder = self.tmp / "server"
        folder.mkdir()
        self.state = folder / "mcp-state.json"
        self.state.write_text("{}")
        self.app.settings.set("mcp", {"tasks_enabled": True, "client_enabled": True}, by="owner")
        self.app.guard.add_rule("mcp.call", "local/*", "allow", only_clean=False)
        self.app.guard.add_rule("mcp.connect", "local/*", "allow", only_clean=False)

    context = background.McpBackgroundTest.context
    attach = background.McpBackgroundTest.attach
    change = background.McpBackgroundTest.change
    ready = background.McpBackgroundTest.ready

    @async_test
    async def test_completed_task_image_goes_same_path_and_card_has_no_base64(self):
        try:
            await self.ready()
            await self.app.mcp.call("local", "echo", {"text": "снимок"}, self.context(actor="agent"))
            entry = next(reversed(self.app.mcp.tasks.records().values()))
            self.change(task_state={**background.TASK, "resultType": "complete", "status": "completed",
                                    "result": {"content": [{"type": "text", "text": "ГОТОВО"}, {"type": "image", "data": DATA, "mimeType": "image/png"}]}})
            ctx = self.context(actor="agent")
            result = await self.app.mcp.tasks.request("status", entry["id"], ctx)
            self.assertTrue(result.ok, result.content)
            self.assertTrue(result.tainted)
            self.assertIn("ГОТОВО", result.content)
            self.assertNotIn(DATA, result.content)
            self.assertIn("[картинка 1 приложена отдельным сообщением; файл: ", result.content)
            image = next(p for p in result.model_parts if p["type"] == "image")
            self.assertEqual((image["source"], Path(image["path"]).read_bytes()), ("mcp", RAW))
            self.assertEqual(ctx.turn["read_media"]["files"], 1)
            self.assertNotIn(DATA, json.dumps(self.app.mcp.tasks.public(self.context(actor="owner"))))
            listed = await self.app.mcp.tasks.request("list", None, self.context(actor="agent"))
            self.assertNotIn(DATA, listed.content)
            self.assertFalse(listed.model_parts)
        finally:
            await self.app.close()


if __name__ == "__main__":
    import unittest
    unittest.main()
