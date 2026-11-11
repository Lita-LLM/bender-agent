"""Тело: самочувствие, режим бомжа в Коммутаторе, сводка."""

import json

from bender.router.core import NoRoute, Switchboard
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv


class BodyTest(TempDirCase):
    @async_test
    async def test_economy_mode_uses_only_free_models(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            # платный провайдер без признаков бесплатности (и недоступный: если его тронут — тест увидит повтор)
            paid = app.models.add_provider(name="Платный", kind="openai", base_url="http://paid.invalid:9/v1")
            free_pid = app.models.roles()["main"][0]["provider"]
            app.models.set_role("main", [{"provider": paid["id"], "model": "echo"}, {"provider": free_pid, "model": "echo"}])
            self.assertEqual(app.body.mood()["code"], "ok")
            app.settings.set("budget", {"daily_usd": 0.01})
            app.store.add_call(session_id=None, role="main", provider="x", model="y", usage={}, cost=0.02, seconds=1, notice=None, request=None)
            m = app.body.mood()
            self.assertEqual((m["code"], m["mode"]), ("hungry", "economy"))
            self.assertTrue(app.body.economy())
            statuses = []
            res = await app.router.call([{"role": "user", "content": "привет"}], role="main", on_status=statuses.append)
            self.assertEqual(res.provider_name, "Фейк")  # платный пропущен
            self.assertEqual(statuses[0]["type"], "economy")
            app.models.set_role("main", [{"provider": paid["id"], "model": "echo"}])
            with self.assertRaises(NoRoute) as cm:
                await app.router.call([{"role": "user", "content": "привет"}], role="main")
            self.assertIn("режим бомжа", str(cm.exception.to_dict()))
            app.settings.set("budget", {"economy_auto": False})
            self.assertFalse(app.body.economy())

    def test_is_free(self):
        f = Switchboard.is_free
        self.assertTrue(f({"is_local": True}, "x"))
        self.assertTrue(f({"kind": "chatgpt"}, "gpt-6-astra"))
        self.assertTrue(f({"base_url": "http://127.0.0.1:11434/v1"}, "qwen"))
        self.assertFalse(f({"base_url": "http://localhost.evil.example/v1"}, "qwen"))
        self.assertTrue(f({"base_url": "https://openrouter.ai/api/v1"}, "meta-llama/llama-4:free"))
        self.assertTrue(f({"prices": {"m": {"in": 0, "out": 0}}}, "m"))
        self.assertFalse(f({"base_url": "https://api.openai.com/v1"}, "gpt-6"))
        self.assertFalse(f({"prices": {"m": {"in": 1, "out": 2}}}, "m"))

    @async_test
    async def test_status_tool_and_sick(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            from bender.tools.registry import ToolContext

            r = await app.tools.run("status", {}, ToolContext(app=app, session_id=env.sid))
            self.assertIn("Самочувствие: бодр", r.content)
            self.assertIn("режим: норма", r.content)
            for _ in range(5):
                app.store.add_call(session_id=None, role="main", provider="x", model="y", usage={}, cost=0, seconds=1, notice=None, request=None, error={"klass": "server"})
            self.assertEqual(app.body.mood()["code"], "sick")
            app.guard.halt()
            self.assertEqual(app.body.mood()["mode"], "halt")
            json.dumps(app.body.snapshot(full=True))  # сводка сериализуется для пульта
