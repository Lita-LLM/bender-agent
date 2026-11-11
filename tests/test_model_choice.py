"""Модели, введённые вручную, сохраняются; модель можно применить ко всем агентам на роль."""
import asyncio

from bender.core.errors import HumanError
from bender.net.http_server import HttpServer
from bender.router.catalog import apply_catalog
from tests.helpers import TempDirCase, async_test
from tests.helpers import owner_web as create_web
from tests.test_hooks import _call
from tests.test_mind import MindEnv


class ModelChoiceTest(TempDirCase):
    @async_test
    async def test_manual_model_kept_and_forgotten(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            pid = app.models.roles()["main"][0]["provider"]
            app.models.update_provider(pid, {"models": [{"id": "echo"}]})
            app.models.set_role("main", [{"provider": pid, "model": "my-private-model"}])
            ids = [m["id"] for m in app.models.providers()[pid]["models"]]
            self.assertEqual(ids, ["echo", "my-private-model"])
            self.assertTrue(app.models.providers()[pid]["models"][-1]["manual"])
            # повторный выбор не плодит записи
            app.models.set_role("fast", [{"provider": pid, "model": "my-private-model"}])
            self.assertEqual(len(app.models.providers()[pid]["models"]), 2)
            # обновление каталога не стирает введённую руками
            p = apply_catalog(app.models.providers()[pid], [{"id": "echo"}, {"id": "slow"}])
            self.assertEqual([m["id"] for m in p["models"]], ["echo", "slow", "my-private-model"])
            # каталог узнал модель — пометка «вручную» больше не нужна
            p = apply_catalog(app.models.providers()[pid], [{"id": "my-private-model"}])
            self.assertEqual(p["models"], [{"id": "my-private-model"}])
            # забыть можно только введённую руками
            app.models.forget_model(pid, "my-private-model")
            self.assertEqual([m["id"] for m in app.models.providers()[pid]["models"]], ["echo"])
            with self.assertRaises(HumanError):
                app.models.forget_model(pid, "echo")
            self.assertEqual(app.models.roles()["main"][0]["model"], "my-private-model", "роль не трогается")

    @async_test
    async def test_apply_role_to_all_agents(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            pid = app.models.roles()["main"][0]["provider"]
            second = app.agents.get(app.agents.create("Второй", agent_id="second", awaken=False)["id"])
            archived = app.agents.get(app.agents.create("Спящий", agent_id="sleeper", awaken=False)["id"])
            second.models.set_role("fast", [{"provider": pid, "model": "old"}])
            archived.models.set_role("fast", [{"provider": pid, "model": "old"}])
            await app.agents.update("sleeper", archived=True)
            web = HttpServer(create_web(app, allowed_hosts={"127.0.0.1"}))
            await web.start("127.0.0.1", 0)
            try:
                chain = [{"provider": pid, "model": "echo"}, {"provider": pid, "model": "slow"}]
                code, out = await asyncio.to_thread(_call, web.port, "/api/roles/fast/all", {"entries": chain}, {"X-Bender": "1"})
                self.assertEqual(code, 200, out)
                self.assertEqual(sorted(out["agents"]), ["main", "second"])
                self.assertEqual([e["model"] for e in app.models.roles()["fast"]], ["echo", "slow"])
                self.assertEqual([e["model"] for e in second.models.roles()["fast"]], ["echo", "slow"])
                self.assertEqual([e["model"] for e in archived.models.roles()["fast"]], ["old"], "архив не трогается")
                # потом каждого можно поправить отдельно
                second.models.set_role("fast", [{"provider": pid, "model": "echo"}])
                self.assertEqual(len(app.models.roles()["fast"]), 2)
                code, out = await asyncio.to_thread(_call, web.port, "/api/roles/nope/all", {"entries": chain}, {"X-Bender": "1"})
                self.assertEqual(code, 404)
            finally:
                await web.stop()
