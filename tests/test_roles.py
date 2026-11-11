"""Свои роли: подтверждение хозяина, границы полномочий и вложенная очередь."""
import asyncio
import json
from unittest.mock import patch
from bender.mind.loop import Mind

from bender.core.app import App
from bender.core.errors import HumanError
from bender.core.settings import NeedsOwner
from bender.guard.policy import Action
from bender.tools.registry import Denied, ToolContext
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv, call
from tests.test_team import child_requests, SPAM


class RolesTest(TempDirCase):
    @async_test
    async def test_custom_prompt_model_default_schema_and_real_turn(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            pid = app.models.roles()["main"][0]["provider"]
            app.models.set_role("fast", [{"provider":pid,"model":"script"}])
            app.team.save_role("auditor", {"title":"Мой чтец", "prompt":"Сверь признаки спама.", "capabilities":[], "model":"fast", "schema":SPAM})
            env.script([call("write_file", path="нет.txt", content="чужое"), {"text":'{"spam":false}'}])
            task = app.team.spawn("auditor", "Проверь письмо", parent_session=app.main_session(), tainted=True)
            [done] = await app.team.wait([task["id"]], 5)
            self.assertEqual(done["schema_valid"], 1)
            self.assertEqual(done["data"], {"spam":False})
            self.assertEqual(done["result_tainted"], 0)
            self.assertFalse((app.dirs.work/"нет.txt").exists())
            self.assertIn("Сверь признаки спама", child_requests(env)[0]["messages"][0]["content"])
            self.assertNotIn("write_file", json.dumps(child_requests(env)[0].get("tools", [])))
            app.team.remove_role("auditor")
            self.assertEqual(app.team.title(done), "Мой чтец")
            with self.assertRaises(HumanError):
                app.team.remove_role("reader")

    @async_test
    async def test_agent_proposal_requires_owner_and_preserves_unknown_fields(self):
        app = App(self.tmp/"data")
        try:
            ctx = ToolContext(app, session_id=app.main_session())
            proposal = {"mine":{"title":"Своя", "prompt":"Читай", "capabilities":["fs.read"], "model":"fast", "future":{"value":1}}}
            with self.assertRaises(NeedsOwner):
                app.settings.set("team_roles", proposal, by="agent")
            app.bus.listen(lambda e: app.guard.respond(e["card"]["id"], "deny") if e.get("type")=="approval" else None)
            result = await app.tools.run("settings", {"group":"team_roles", "changes":proposal}, ctx)
            self.assertFalse(result.ok)
            self.assertNotIn("mine", app.team.roles())
            # Новый экземпляр шины не нужен: первый ответ уже завершает карточку.
            app.bus._listeners.clear()
            app.bus.listen(lambda e: app.guard.respond(e["card"]["id"], "once") if e.get("type")=="approval" else None)
            result = await app.tools.run("settings", {"group":"team_roles", "changes":proposal}, ctx)
            self.assertTrue(result.ok, result.content)
            app.team.save_role("mine", {"title":"Исправлено"})
            self.assertEqual(app.settings.group("team_roles")["mine"]["future"], {"value":1})
        finally:
            await app.close()

    @async_test
    async def test_nested_intersection_depth_taint_and_single_lane(self):
        app = App(self.tmp/"data")
        app.settings.set("team", {"parallel":1})
        app.team.save_role("lead", {"title":"Ведущий", "prompt":"Веди", "capabilities":["fs.read", "team.spawn"], "model":"main"})
        children = []
        async def say(mind, sid, first, **kw):
            worker=mind.app
            ctx = ToolContext(worker, session_id=sid, tainted=True)
            if "Ведущий" in kw["system"]:
                result = await worker.tools.run("spawn_task", {"role":"coder","goal":"Проверь код"}, ctx)
                self.assertTrue(result.ok, result.content)
                children.append(result.data["task"])
                result = await worker.tools.run("wait_tasks", {"ids":children,"timeout":3}, ctx)
                return {"text":result.content,"tainted":result.tainted}
            task = app.team.get(app.store.session(sid)["meta"]["task"])
            self.assertEqual(task["depth"], 2)
            self.assertNotIn("fs.write", task["budget"]["authority"]["capabilities"])
            self.assertNotIn("spawn_task", kw["tool_names"])
            self.assertFalse((await worker.tools.run("write_file", {"path":"нет","content":"нет"}, ctx)).ok)
            app.secrets.set("role-key", "secret-value", allow={"tools":["run"]})
            with self.assertRaises(HumanError):
                app.guard.expand_secrets("{{secret:role-key}}", tool="run", session_id=sid)
            app.guard.grant(sid, [("fs.write","*")])
            app.settings.set("guard", {"profile":"off"})
            self.assertFalse((await app.guard.check(Action("fs.write",str(app.dirs.work/"нет")),session_id=sid)).allowed)
            with self.assertRaises(HumanError):
                app.team.spawn("reader", "Третий уровень", parent_session=sid)
            return {"text":"Проверено", "tainted":True}
        mock=patch.object(Mind,"say",say);mock.start();self.addCleanup(mock.stop)
        try:
            parent = app.team.spawn("lead", "Разбери задачу", parent_session=app.main_session())
            [done] = await app.team.wait([parent["id"]], 5)
            self.assertEqual(done["status"], "done", done["error"])
            self.assertIn("Проверено", done["result"])
            self.assertEqual(done["result_tainted"], 1)
            self.assertEqual(app.team.get(children[0])["status"], "done")
            self.assertEqual(app.team._holders, {})
        finally:
            await app.close()

    @async_test
    async def test_revocation_during_owner_approval_and_no_rights_growth(self):
        app = App(self.tmp/"data")
        app.team.save_role("writer", {"capabilities":["fs.write"], "prompt":"Запиши", "model":"main"})
        async def say(mind, sid, first, **kw):
            worker=mind.app
            ctx = ToolContext(worker, session_id=sid)
            worker.settings.set('agent_rights',{'work_folders':[str(self.tmp)]})
            app.team.save_role("writer", {"capabilities":["fs.write", "net.send"]})
            with self.assertRaises(Denied):
                await ctx.check("net.send", "https://example.invalid")
            def revoke(e):
                if e.get("type") == "approval":
                    app.team.save_role("writer", {"capabilities":[]})
                    app.guard.respond(e["card"]["id"], "once")
            app.bus.listen(revoke)
            result = await worker.tools.run("write_file", {"path":str(self.tmp/"outside.txt"),"content":"нет"}, ctx)
            self.assertFalse(result.ok)
            self.assertFalse((self.tmp/"outside.txt").exists())
            self.assertEqual(app.team.roles()['writer'].capabilities,())
            return {"text":"Отказано после отзыва"}
        mock=patch.object(Mind,"say",say);mock.start();self.addCleanup(mock.stop)
        try:
            parent = app.team.spawn("writer", "Пиши", parent_session=app.main_session())
            [done] = await app.team.wait([parent["id"]], 5)
            self.assertEqual(done["status"], "done", done["error"])
        finally:
            await app.close()

    @async_test
    async def test_disable_stops_queue_and_running_and_validation(self):
        app = App(self.tmp/"data")
        app.settings.set("team", {"parallel":1})
        app.team.save_role("slow", {"capabilities":[], "prompt":"Жди", "model":"main"})
        started = asyncio.Event()
        async def say(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()
        mock=patch.object(Mind,"say",say);mock.start();self.addCleanup(mock.stop)
        try:
            a = app.team.spawn("slow", "Первая")
            b = app.team.spawn("slow", "Вторая")
            await started.wait()
            app.team.save_role("slow", {"enabled":False})
            [first, second] = await app.team.wait([a["id"], b["id"]], 3)
            self.assertEqual(first["status"], "cancelled")
            self.assertEqual(second["status"], "cancelled")
            with self.assertRaises(HumanError):
                app.team.spawn("slow", "Нет")
            for value in ({"capabilities":["root"]}, {"capabilities":"fs.read"}, {"schema":[]}, {"model":"unknown"}, {"tools":["magic"]}, {"tools":None}):
                with self.assertRaises(HumanError):
                    app.team.save_role("invalid", value)
        finally:
            await app.close()

    @async_test
    async def test_task_scope_and_restricted_parent_turn(self):
        app = App(self.tmp/"data")
        async def say(*args, **kwargs):
            return {"text":"Готово"}
        mock=patch.object(Mind,"say",say);mock.start();self.addCleanup(mock.stop)
        try:
            parent = app.main_session()
            other = app.store.new_session("chat")
            task = app.team.spawn("reader", "Читай", parent_session=other)
            for name,args in (("wait_tasks",{"ids":[task["id"]]}), ("tasks",{"action":"cancel","id":task["id"]})):
                result = await app.tools.run(name,args,ToolContext(app,session_id=parent))
                self.assertFalse(result.ok)
            from bender.mind.loop import Turn
            app.mind.turns[parent] = Turn(parent, "pult", tool_names=("read_file", "spawn_task"))
            task = app.team.spawn("coder", "Без записи", parent_session=parent)
            self.assertEqual(task["budget"]["authority"]["tools"], ["read_file"])
            await app.team.wait([task["id"]], 3)
        finally:
            await app.close()
