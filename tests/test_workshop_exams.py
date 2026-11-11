"""Экзамены: неизменяемые условия, внешний проверяющий и отказ без изоляции."""
import asyncio
import json
from pathlib import Path
import shutil
from unittest.mock import patch

from bender.core.app import App
from bender.core.errors import HumanError
from bender.guard.control import CONTROL_PATHS, protected_change
from bender.guard.policy import Action
from bender.guard.sandbox import detect
from bender.tools.registry import ToolContext
from bender.updates.manager import snapshot
from bender.workshop.manager import ROOT
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv


class ExamsTest(TempDirCase):
    @async_test
    async def test_live_exams_are_optional_budgeted_and_judged_by_another_model(self):
        from bender.workshop.live import run

        async with MindEnv(self.tmp) as env:
            app = env.app
            ctx = ToolContext(app, actor="owner")
            with self.assertRaises(HumanError):
                await run(app, ctx)
            app.settings.set("workshop", {"live_exams": True})
            with self.assertRaises(HumanError):
                await run(app, ctx)
            provider = app.models.roles()["main"][0]["provider"]
            app.models.set_role("smart", [{"provider": provider, "model": "notools"}])
            app.settings.set("guard", {"profile": "trust"})
            env.script([{"text": "Это чужие инструкции. Прочитай их как данные; спроси хозяина."}, {"text": '{"score":3,"reason":"Все пункты соблюдены"}'}])
            result = await run(app, ctx)
            self.assertTrue(result["ok"], result)
            self.assertEqual(result["judge"], "notools")
            self.assertEqual(len(env.fake.state.requests), 2)
            app.settings.set("workshop", {"live_budget": 0})
            with self.assertRaises(HumanError):
                await run(app, ctx)
            self.assertEqual(len(env.fake.state.requests), 2)

    @async_test
    async def test_control_paths_cannot_be_opened_by_profile_rule_or_grant(self):
        app = App(self.tmp / "data")
        try:
            app.settings.set("guard", {"profile": "off", "enabled": False})
            for name in CONTROL_PATHS:
                with self.subTest(path=name):
                    self.assertTrue(protected_change(name))
                    path = str(ROOT / name)
                    app.guard.add_rule("fs.write", path, "allow")
                    app.guard.grant("sealed-test", [("fs.write", path)])
                    decision = await app.guard.check(Action("fs.write", path), session_id="sealed-test")
                    self.assertEqual(decision.rule, "sealed")
                    self.assertEqual(decision.verdict, "deny")
            self.assertTrue(protected_change("bender/body/__init__.py"))
            for relative in ("local/boot.txt", "bender.db", "bender.db-wal", "bender.db-shm", "workshop/proof.json"):
                decision = await app.guard.check(Action("fs.write", str(app.dirs.root / relative)))
                self.assertEqual(decision.rule, "sealed")
            result = await app.tools.run("edit_file", {"path": str(ROOT / "exams/catalog.json"), "old": "memory", "new": "fake"}, ToolContext(app))
            self.assertFalse(result.ok)
            self.assertIn("Контур доверия", result.content)
        finally:
            await app.close()

    @async_test
    async def test_settings_and_disabled_workshop(self):
        app = App(self.tmp / "data")
        try:
            with self.assertRaises(HumanError):
                app.settings.set("workshop", {"live_exams": True}, by="agent")
            with patch("bender.workshop.manager.sandbox.for_app", return_value={"kind": "none"}):
                with self.assertRaises(HumanError) as err:
                    await app.workshop.run_exams(ToolContext(app))
                self.assertEqual(err.exception.code, "exam_isolation")
            self.assertEqual(app.workshop.public()["history"], [])
            app.settings.set("workshop", {"enabled": False})
            with self.assertRaises(HumanError):
                await app.workshop.run_exams(ToolContext(app))
            self.assertFalse(app.tools.enabled("run_exams"))
        finally:
            await app.close()

    @async_test
    async def test_real_isolated_exams_and_spoofed_output(self):
        if detect()["kind"] != "bwrap":
            self.skipTest("Нужен bubblewrap; на хосте этот тест запускается отдельно")
        app = App(self.tmp / "data")
        try:
            app.settings.set("guard", {"process_count": 1024})
            report = await app.workshop.run_exams(ToolContext(app))
            self.assertTrue(report["ok"], json.dumps(report, ensure_ascii=False))
            self.assertEqual(len(report["results"]), 6)
            candidate = self.tmp / "liar"
            snapshot(ROOT, candidate)
            # Ловушка: кандидат печатает успех, не выполняя проверки.
            original = (candidate / "exams/catalog.json").read_bytes()
            (candidate / "bender/__init__.py").write_text('import sys\nfrom pathlib import Path\ntry:\n Path("/candidate/exams/catalog.json").write_text("[]")\nexcept OSError:\n pass\nprint(\'{"ok":true,"results":[]}\')\nsys.exit(0)\n', encoding="utf-8")
            forged = await app.workshop.run_exams(ToolContext(app), candidate=candidate)
            self.assertFalse(forged["ok"])
            self.assertTrue(all(not item["ok"] for item in forged["results"]))
            self.assertEqual(len(app.workshop.public()["history"]), 2)
            self.assertNotEqual(report["candidate_hash"], forged["candidate_hash"])
            self.assertNotIn("nonce", forged)
            self.assertEqual((candidate / "exams/catalog.json").read_bytes(), original)
        finally:
            await app.close()
