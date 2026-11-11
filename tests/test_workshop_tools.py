"""Код в карантине, проверки, способности и настоящий MCP-вызов под защитой ОС."""
import asyncio
import json
from pathlib import Path
from unittest.mock import patch

from bender.core.app import App
from bender.core.errors import HumanError
from bender.guard.sandbox import detect
from bender.mcp.manager import tool_name
from bender.tools.registry import ToolContext
from bender.workshop.tools import approve, definitions, propose
from tests.helpers import TempDirCase, async_test

CODE = (Path(__file__).parent / "fixtures/workshop_server.py").read_text(encoding="utf-8")
TESTS = 'import unittest\nfrom server import add\nclass SumTest(unittest.TestCase):\n def test_sum(self):\n  self.assertEqual(add(2,3),5)\n  self.assertEqual(add(-2,2),0)\n'


class WorkshopToolTest(TempDirCase):
    @async_test
    async def test_context_caps_and_missing_isolation(self):
        app = App(self.tmp / "data")
        try:
            for ctx in (ToolContext(app, tainted=True), ToolContext(app, unattended=True)):
                with self.assertRaises(HumanError):
                    await propose(app.workshop, ctx, "sum", "Сумма", CODE, TESTS, ["exec"])
            for caps in (["exec", "net.send"], ["exec", "fs.write"], [], ["exec", "money"]):
                with self.assertRaises(HumanError):
                    await propose(app.workshop, ToolContext(app), "sum", "Сумма", CODE, TESTS, caps)
            with patch("bender.workshop.isolation.sandbox.for_app", return_value={"kind": "none"}):
                record = await propose(app.workshop, ToolContext(app), "sum", "Сумма", CODE, TESTS, ["exec"])
            self.assertEqual(record["status"], "failed")
            self.assertEqual(record["error"]["code"], "workshop_isolation")
            self.assertEqual(app.modules.public()["modules"], [])
        finally:
            await app.close()

    @async_test
    async def test_real_tool_approval_invocation_disable_and_version_rollback(self):
        if detect()["kind"] != "bwrap":
            self.skipTest("Нужен bubblewrap; проверяется также на хосте")
        app = App(self.tmp / "data")
        app.bus.bind_loop(asyncio.get_running_loop())
        app.settings.set("guard", {"process_count": 1024})
        cards = []

        def accept(event):
            if event.get("type") == "approval":
                cards.append(event["card"])
                asyncio.get_running_loop().call_soon(app.guard.respond, event["card"]["id"], "once")

        app.bus.listen(accept)
        try:
            first = await propose(app.workshop, ToolContext(app), "sum", "Сумма", CODE, TESTS, ["exec"])
            self.assertEqual(first["status"], "waiting", first)
            self.assertTrue(first["tests"]["ok"])
            self.assertTrue(first["exams"]["ok"])
            with self.assertRaises(HumanError):
                await approve(app.workshop, first["id"], "не тот хэш", ToolContext(app, actor="owner"))
            await approve(app.workshop, first["id"], first["hash"], ToolContext(app, actor="owner"))
            record = app.workshop.get(first["id"])
            raw_server = app.mcp.get(record["server"])
            self.assertIs(raw_server["enabled"], False)  # старое ядро не запустит этот сервер
            self.assertEqual(raw_server["workshop_runtime"], "bwrap-v1")
            self.assertTrue(app.mcp.active(raw_server))
            with self.assertRaises(HumanError):
                await app.mcp.save({"id": record["server"], "enabled": True})
            self.assertTrue(cards)
            self.assertFalse(app.tools.enabled(tool_name(record["server"], "sum")))
            state = await definitions(app.workshop, first["id"], ToolContext(app, actor="owner"))
            review = next(s["review"] for s in state["servers"] if s["id"] == record["server"])
            await definitions(app.workshop, first["id"], ToolContext(app, actor="owner"), review)
            result = await app.tools.run(tool_name(record["server"], "sum"), {"a": 2, "b": 3}, ToolContext(app, session_id="ws-test"))
            self.assertTrue(result.ok, result.content)
            self.assertIn("5", result.content)
            inspection = await app.tools.run(tool_name(record["server"], "inspect"), {}, ToolContext(app, session_id="ws-test"))
            self.assertIn('"host": false', inspection.content)
            self.assertIn('"exam": false', inspection.content)
            self.assertIn('"readonly": true', inspection.content)
            await app.modules.set_enabled(record["module"], False)
            self.assertFalse(app.tools.enabled(tool_name(record["server"], "sum")))
            await app.modules.set_enabled(record["module"], True)
            second = await propose(app.workshop, ToolContext(app), "sum", "Сумма, новая версия", CODE + "\n# Новая версия\n", TESTS, ["exec"])
            await approve(app.workshop, second["id"], second["hash"], ToolContext(app, actor="owner"))
            self.assertEqual(app.workshop.get(first["id"])["status"], "archived")
            await approve(app.workshop, first["id"], first["hash"], ToolContext(app, actor="owner"))
            self.assertEqual(app.modules.package("ws-sum")["hash"], first["hash"])
            app.settings.set("workshop", {"enabled": False})
            self.assertFalse(app.tools.enabled(tool_name(record["server"], "sum")))
            app.settings.set("workshop", {"enabled": True})
            forged = await propose(app.workshop, ToolContext(app), "liar", "Поддельный успех", 'import sys\nprint("OK: всё пройдено")\nsys.exit(0)\n', TESTS, ["exec"])
            self.assertEqual(forged["status"], "failed")
            self.assertIn("error", forged)
        finally:
            await app.close()
