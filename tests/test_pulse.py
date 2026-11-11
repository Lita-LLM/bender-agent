"""Сердце, шаг 2: пульс (рефлексы → суждение) и намерения по словам."""

import json
import time
import unittest

from bender.heart import pulse as pulsemod
from bender.heart.intents import matches
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv, call


def model_calls(env):
    return [r for r in env.fake.state.requests if r.get("api") == "openai"]


class PulseTest(TempDirCase):
    def setUp(self):
        super().setUp()
        import bender.body.sensors as sensors

        self._orig = (sensors.temperature, sensors.battery)
        sensors.temperature = lambda: 40.0  # машина в тестах «здорова», что бы ни показывал этот сервер
        sensors.battery = lambda: None

    def tearDown(self):
        import bender.body.sensors as sensors

        sensors.temperature, sensors.battery = self._orig
        super().tearDown()

    def events(self, env, kind):
        return [m["content"] for m in env.app.store.messages(env.app.main_session()) if m.get("kind") == kind]

    async def _env(self, env):
        env.app.settings.set("heart", {"quiet_hours": ""})
        pid = env.app.models.roles()["main"][0]["provider"]
        env.app.models.set_role("fast", [{"provider": pid, "model": "script"}])
        env.app.main_session()

    @async_test
    async def test_calm_pulse_costs_nothing(self):
        async with MindEnv(self.tmp) as env:
            await self._env(env)
            before = env.app.store.db.total_changes
            res = await env.app.heart.beat()
            self.assertEqual((res["action"], res["model"], res["signals"]), ("silent", False, []))
            self.assertEqual(env.app.store.db.total_changes, before)  # ни записи
            self.assertEqual(model_calls(env), [])                     # ни вызова модели
            self.assertIsNotNone(env.app.heart.pulse.next_at)

    @async_test
    async def test_checklist_goes_to_model_not_too_often(self):
        async with MindEnv(self.tmp) as env:
            await self._env(env)
            env.app.selfdir.write("HEARTBEAT.md", "# HEARTBEAT.md\n\n- проверь, не пришло ли письмо от банка\n- [ ] глянь погоду на завтра\n")
            env.script([{"text": json.dumps({"action": "tell", "message": "Письма от банка нет, завтра дождь — возьми зонт."}, ensure_ascii=False)}])
            res = await env.app.heart.beat()
            self.assertTrue(res["model"])
            self.assertEqual(self.events(env, "pulse"), ["💓 Письма от банка нет, завтра дождь — возьми зонт."])
            sent = model_calls(env)[-1]["body"]["messages"][1]["content"]
            self.assertIn("письмо от банка", sent)
            n = len(model_calls(env))
            res = await env.app.heart.beat()  # чек-лист только что смотрели — модель не нужна
            self.assertFalse(res["model"])
            self.assertEqual(len(model_calls(env)), n)
            env.app.selfdir.write("HEARTBEAT.md", "# HEARTBEAT.md\n\n- проверь письмо от банка\n- новое: полей цветы\n")
            env.script([{"text": '{"action": "silent"}'}])
            self.assertTrue((await env.app.heart.beat())["model"])  # чек-лист изменился — смотрим сразу

    @async_test
    async def test_reflex_mode_and_waiting_approvals(self):
        async with MindEnv(self.tmp) as env:
            await self._env(env)
            env.app.settings.set("heart", {"mode": "reflexes"})
            env.app.guard.pending["x"] = {"card": {"id": "x", "summary": "удалить старые логи", "ts": time.time() - 1800}, "future": None, "action": None}
            res = await env.app.heart.beat()
            self.assertFalse(res["model"])
            self.assertEqual(len(self.events(env, "pulse")), 1)
            self.assertIn("удалить старые логи", self.events(env, "pulse")[0])
            await env.app.heart.beat()
            self.assertEqual(len(self.events(env, "pulse")), 1)  # уже сказал — не повторяет
            env.app.guard.pending.clear()

    @async_test
    async def test_alarm_in_quiet_hours_without_model(self):
        async with MindEnv(self.tmp) as env:
            await self._env(env)
            import bender.body.sensors as sensors

            sensors.temperature = lambda: 97.0
            env.app.settings.set("heart", {"quiet_hours": "00:00-23:59"})
            res = await env.app.heart.beat()
            self.assertEqual(res["action"], "alarm")
            self.assertFalse(res["model"])
            self.assertEqual(self.events(env, "alarm"), ["🚨 Машина горячая: 97.0 °C"])
            await env.app.heart.beat()
            self.assertEqual(len(self.events(env, "alarm")), 1)

    @async_test
    async def test_do_runs_unattended_turn(self):
        async with MindEnv(self.tmp) as env:
            await self._env(env)
            env.app.settings.set("heart", {"mode": "always_model"})
            env.script([{"text": '{"action": "do", "task": "Запиши в дневник, что всё спокойно"}'},
                        call("note", text="Пульс: всё спокойно"), {"text": "Записал в дневник."}])
            res = await env.app.heart.beat()
            await env.app.mind.idle()
            self.assertEqual(res["action"], "do")
            self.assertEqual(self.events(env, "job"), ["💓 Сделал сам: Записал в дневник."])
            hb = [s for s in env.app.store.sessions() if s["kind"] == "heartbeat"]
            self.assertEqual(len(hb), 1)
            diary = list(env.app.dirs.diary.glob("*.md"))[0].read_text(encoding="utf-8")
            self.assertIn("Пульс: всё спокойно", diary)

    def test_checklist_parsing(self):
        self.assertEqual(pulsemod.checklist("# H\n\n_Пусто — пульсу нечего проверять._\n"), [])
        self.assertEqual(pulsemod.checklist("- один\n* два <!-- importance: 5 -->\n1. три\n- [x] четыре\nтекст"), ["один", "два", "три", "четыре"])


class IntentTest(TempDirCase):
    def test_matching(self):
        self.assertEqual(matches(["релиз"], "Когда будет релиз кометы?"), "релиз")
        self.assertEqual(matches(["выпуск версии"], "готовим выпуск новой версии"), "выпуск версии")
        self.assertIsNone(matches(["выпуск версии"], "готовим выпуск"))
        self.assertEqual(matches(["релиз"], "про релизы"), "релиз")

    @async_test
    async def test_intent_fires_on_owner_words(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            ctx = ToolContext(app=app, session_id=env.sid, actor="agent")
            r = await app.tools.run("intent", {"action": "add", "text": "Спроси про changelog", "keywords": "релиз, выпуск версии", "times": 2}, ctx)
            self.assertTrue(r.ok, r.content)
            env.script([{"text": "Ок."}, {"text": "Ок."}, {"text": "Ок."}])
            await app.mind.say(env.sid, "Завтра релиз кометы")
            last = [r for r in env.fake.state.requests if r["body"].get("tools")][-1]["body"]["messages"][-1]["content"]
            self.assertIn("Ты сам себе напомнил", last)
            self.assertIn("Спроси про changelog", last)
            await app.mind.say(env.sid, "а релиз точно завтра?")  # пауза 24 ч — не повторяется
            last = [r for r in env.fake.state.requests if r["body"].get("tools")][-1]["body"]["messages"][-1]["content"]
            self.assertNotIn("Ты сам себе напомнил", last)
            it = app.heart.intents.all()[0]
            self.assertEqual((it["fired"], it["status"]), (1, "armed"))
            # чужие слова (заражённый ход) намерения не будят
            app.store.x("UPDATE intents SET last_fired = NULL")
            await app.mind.say(env.sid, "релиз, релиз!", actor="stranger")
            self.assertEqual(app.heart.intents.all()[0]["fired"], 1)
            # срок
            app.store.x("UPDATE intents SET expires = ?", (time.time() - 1,))
            self.assertEqual(app.heart.intents.expire(), 1)
            self.assertEqual(app.heart.intents.all()[0]["status"], "expired")


if __name__ == "__main__":
    unittest.main()
