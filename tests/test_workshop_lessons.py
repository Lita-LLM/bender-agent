"""Ошибки и поправки дают уроки; метрики читают наблюдения, не пишут оценки."""
import asyncio
import json
import time
from unittest.mock import patch

from bender.core.app import App
from bender.core.errors import HumanError
from bender.skills.experience import update
from bender.sleep import Night
from bender.workshop.lessons import save
from bender.workshop.metrics import snapshot
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv, call


class LessonsTest(TempDirCase):
    @async_test
    async def test_failed_turn_keeps_cost_of_completed_model_calls(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            app.settings.set("memory", {"review": False})
            original = app.router.call
            count = 0

            async def paid_then_failed(*args, **kwargs):
                nonlocal count
                count += 1
                if count > 1:
                    raise HumanError("test_failure", "Модель прервалась", "Повтори позже.")
                result = await original(*args, **kwargs)
                result.cost = .025
                return result

            env.script([call("read_file", path="missing.txt")])
            with patch.object(app.router, "call", side_effect=paid_then_failed):
                result = await app.mind.say(env.sid, "Прочитай отчёт")
                await app.mind.idle()
            self.assertIsNotNone(result["error"])
            self.assertAlmostEqual(result["cost"], .025)
            self.assertAlmostEqual(snapshot(app)["turns"]["mean_cost"], .025)

    @async_test
    async def test_tool_error_and_owner_correction_reach_review(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            provider = app.models.roles()["main"][0]["provider"]
            app.models.set_role("fast", [{"provider": provider, "model": "script"}])
            env.script([call("read_file", path="нет-файла.txt"), {"text": "Файл не найден."}, {"text": json.dumps({"lessons": ["Перед чтением уточнять точный путь к отсутствующему файлу."]}, ensure_ascii=False)}])
            await app.mind.say(env.sid, "Открой отчёт")
            await app.mind.idle()
            self.assertIn("Урок: Перед чтением", app.selfdir.read("MEMORY.md"))
            env.script([{"text": "Проверю источник и исправлю."}, {"text": json.dumps({"lessons": ["Сверять итоговые суммы с исходной таблицей перед отправкой отчёта."]}, ensure_ascii=False)}])
            await app.mind.say(env.sid, "Не так, суммы неправильные")
            await app.mind.idle()
            self.assertIn("Сверять итоговые суммы", app.selfdir.read("MEMORY.md"))
            report = snapshot(app)
            json.dumps(report, ensure_ascii=False)
            self.assertEqual(report["turns"]["total"], 2)
            self.assertEqual(report["turns"]["successful"], 1)
            self.assertEqual(report["corrections"], 1)

    @async_test
    async def test_cancelled_owner_turn_gets_bounded_lesson_review(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            provider = app.models.roles()["main"][0]["provider"]
            app.models.set_role("main", [{"provider": provider, "model": "slow"}])
            app.models.set_role("fast", [{"provider": provider, "model": "script"}])
            env.script([{"text": json.dumps({"lessons": ["Перед долгой работой уточнять, нужен ли хозяину подробный разбор."]}, ensure_ascii=False)}])
            entered = asyncio.Event()
            original = app.router.call

            async def pending_main(*args, **kwargs):
                if kwargs.get("role", "main") == "main":
                    entered.set()
                    await asyncio.Event().wait()
                return await original(*args, **kwargs)

            with patch.object(app.router, "call", side_effect=pending_main):
                await app.mind.say(env.sid, "Разбери материал подробно", wait=False)
                await entered.wait()
                task = app.mind.turns[env.sid].task
                self.assertEqual(app.mind.stop(env.sid), 1)
                await task
                await app.mind.idle()
            self.assertIn("Перед долгой работой", app.selfdir.read("MEMORY.md"))
            self.assertEqual(snapshot(app)["turns"]["successful"], 0)

    @async_test
    async def test_failed_subagent_schema_enters_sleep_and_memory(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            app.settings.set("memory", {"review": False})
            provider = app.models.roles()["main"][0]["provider"]
            app.models.set_role("fast", [{"provider": provider, "model": "script"}])
            env.script([{"text": "не JSON"}, {"text": "снова не JSON"}])
            task = app.team.spawn("scribe", "Ответь числом по схеме", schema={"type": "object", "properties": {"count": {"type": "integer"}}, "required": ["count"]}, parent_session=env.sid)
            [done] = await app.team.wait([task["id"]], 10)
            self.assertEqual(done["schema_valid"], 0)
            night = Night(since=0)
            app.sleep._sort(night)
            self.assertTrue(any(row["kind"] == "task" and "схему" in row["text"] for row in night.material["new"]))
            night.dream = {"lessons": ["Проверять обязательные поля JSON-ответа перед передачей результата родителю."]}
            await app.sleep._consolidate(night)
            self.assertIn("Проверять обязательные поля", app.selfdir.read("MEMORY.md"))
            self.assertEqual(snapshot(app)["tasks"]["invalid_schema"], 1)

    @async_test
    async def test_taint_switches_and_metrics_read_without_writes(self):
        app = App(self.tmp / "data")
        try:
            text = "Проверять исходную таблицу перед расчётом итогов."
            self.assertEqual(await save(app, [text], ToolContext(app, tainted=True), source="test"), [])
            app.settings.set("workshop", {"lessons": False})
            self.assertEqual(await save(app, [text], ToolContext(app), source="test"), [])
            app.journal.write("turn", "Пример", actor="owner", data={"ok": True, "cost": .02})
            update(app.skills, "example", uses=3, helped=2, failed=1)
            changed = app.store.db.total_changes
            report = snapshot(app)
            self.assertEqual(app.store.db.total_changes, changed)
            self.assertEqual(report["turns"]["rate"], 1)
            self.assertAlmostEqual(report["turns"]["mean_cost"], .02)
            self.assertEqual(report["skill_uses"], 3)
            app.settings.set("workshop", {"enabled": False})
            self.assertFalse(snapshot(app)["enabled"])
        finally:
            await app.close()
