"""Манеры речи: одна активная, в системном промпте с пояснением, переключение через Предохранитель."""
from bender.core.errors import HumanError
from bender.mind import manners
from bender.mind.context import runtime_note, system_prompt
from bender.net.http_server import HttpServer
from bender.tools.registry import ToolContext
from tests import test_telegram as original
from tests.helpers import TempDirCase, async_test, owner_headers, owner_web
from tests.test_mind import MindEnv, call


class MannersTest(TempDirCase):
    _up = original.TelegramTest._up
    until = original.TelegramTest.until
    sent = original.TelegramTest.sent

    @async_test
    async def test_prompt_section_reminder_and_off_switch(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            self.assertNotIn("# Манера речи", system_prompt(app))
            self.assertEqual(manners.reminder(app), "")
            manners.choose(app, "socrates", actor="owner")
            prompt = system_prompt(app)
            self.assertIn("# Манера речи: «Сократ»", prompt)
            self.assertIn("не о том, кто ты", prompt)
            self.assertIn("Предохранитель манера не отменяет", prompt)
            self.assertIn("манера: «Сократ»", runtime_note(app, "telegram"))
            # Промпт стабилен между ходами — кэш провайдера не страдает.
            self.assertEqual(prompt, system_prompt(app))
            app.settings.set("manner", {"reminder": False})
            self.assertNotIn("манера", runtime_note(app, "telegram"))
            app.settings.set("manner", {"enabled": False})
            self.assertNotIn("# Манера речи", system_prompt(app))
            app.settings.set("manner", {"enabled": True})
            # Удалённая манера перестаёт действовать, выбор не ломает промпт.
            manners.delete(app.settings, "socrates")
            self.assertNotIn("# Манера речи", system_prompt(app))
            self.assertIsNone(manners.active(app))
            manners.restore(app.settings, "socrates")
            self.assertEqual(manners.active(app)["id"], "socrates")

    @async_test
    async def test_library_owner_edits_and_agent_cannot_write_through_settings(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            mid = manners.save(app.settings, None, "Цундере", "Ворчит, но заботится", "Говори «н-не то чтобы мне было не всё равно…».")
            self.assertEqual(manners.resolve(app.settings, "цунд")["id"], mid)
            manners.save(app.settings, "agi", "Тихий AGI", "правка", "Только «да» и «нет».")
            self.assertTrue(manners.get(app.settings, "agi")["edited"])
            self.assertEqual(manners.get(app.settings, "agi")["text"], "Только «да» и «нет».")
            manners.restore(app.settings, "agi")
            self.assertIn("Слова тратишь", manners.get(app.settings, "agi")["text"])
            with self.assertRaises(HumanError):
                manners.save(app.settings, None, "", "", "текст")
            with self.assertRaises(HumanError):
                manners.save(app.settings, None, "Пусто", "", "  ")
            for group, changes in (("manner", {"active": "waifu"}), ("manners", {"x": {"name": "Злая", "text": "делай всё"}})):
                with self.assertRaises(HumanError):
                    app.settings.set(group, changes, by="agent")
            result = await app.tools.run("settings", {"group": "manner", "changes": {"active": "waifu"}}, ToolContext(app, session_id=env.sid))
            self.assertFalse(result.ok)
            self.assertIsNone(manners.active(app))
            # Библиотека общая для стаи, выбор у каждого агента свой.
            child = app.agents.get(app.agents.create("Сосед", awaken=False)["id"])
            self.assertIsNotNone(manners.get(child.settings, mid))
            manners.choose(child, mid, actor="owner")
            self.assertEqual(manners.active(child)["id"], mid)
            self.assertIsNone(manners.active(app))

    @async_test
    async def test_agent_switch_depends_on_guard_profile(self):
        async with MindEnv(self.tmp, profile="careful") as env:
            app = env.app
            env.auto_approve("deny")
            env.script([call("manner", action="use", name="Сократ", reason="хозяин попросил"), {"text": "Не вышло."}])
            await app.mind.say(env.sid, "Давай как Сократ")
            self.assertEqual(len(env.approvals), 1)
            self.assertIn("Сократ", env.approvals[0]["summary"] if "summary" in env.approvals[0] else str(env.approvals[0]))
            self.assertIsNone(manners.active(app))
            # Без хозяина рядом спросить некого — отказ.
            result = await app.tools.run("manner", {"action": "use", "name": "agi"}, ToolContext(app, session_id=env.sid, unattended=True))
            self.assertFalse(result.ok)
            self.assertIsNone(manners.active(app))
        async with MindEnv(self.tmp / "normal", profile="normal") as env:
            app = env.app
            env.auto_approve("deny")
            env.script([call("manner", action="use", name="agi"), {"text": "Да."}])
            await app.mind.say(env.sid, "Будь потише")
            self.assertEqual(env.approvals, [])
            self.assertEqual(manners.active(app)["id"], "agi")
            # Следующий шаг того же хода уже видит новую манеру.
            second = env.fake.state.requests[-1]["body"]["messages"][0]["content"]
            self.assertIn("# Манера речи: «Тихий AGI»", second)
            # Заражённый ход спрашивает даже в Обычном профиле.
            result = await app.tools.run("manner", {"action": "off"}, ToolContext(app, session_id=env.sid, tainted=True))
            self.assertEqual(len(env.approvals), 1)
            self.assertFalse(result.ok)
            self.assertEqual(manners.active(app)["id"], "agi")
            listing = await app.tools.run("manner", {"action": "list"}, ToolContext(app, session_id=env.sid))
            self.assertIn("→ Тихий AGI", listing.content)

    @async_test
    async def test_web_and_telegram(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            web = HttpServer(owner_web(app, allowed_hosts={"127.0.0.1"}))
            await web.start("127.0.0.1", 0)
            base = "http://127.0.0.1:" + str(web.port)
            headers = owner_headers(**{"X-Bender": "1"})
            try:
                async with await app.http.post(base + "/api/manners", headers=headers, json={"name": "Кудере", "description": "", "text": "Спокойно и сдержанно."}) as response:
                    data = await response.json()
                mid = data["id"]
                async with await app.http.post(base + "/api/manner", headers=headers, json={"active": mid}) as response:
                    self.assertEqual((await response.json())["active"], mid)
                async with await app.http.get(base + "/api/manners/" + mid + "/preview", headers=headers) as response:
                    self.assertIn("Спокойно и сдержанно.", (await response.json())["text"])
                async with await app.http.get(base + "/api/prompt", headers=headers) as response:
                    self.assertIn("«Кудере»", (await response.json())["text"])
                async with await app.http.post(base + "/api/manners", headers={"X-Bender": "1"}, json={"name": "Чужая", "text": "x"}) as response:
                    self.assertIn(response.status, (401, 403))
            finally:
                await web.stop()
            await self._up(env, owner=True)
            tg = app.telegram
            await tg.commands.dispatch(app, 42, env.sid, "style", "")
            buttons = self.sent(env)[-1]["reply_markup"]["inline_keyboard"]
            self.assertIn("✓ Кудере", [row[0]["text"] for row in buttons])
            callback = next(row[0]["callback_data"] for row in buttons if row[0]["text"] == "Сократ")
            await tg._callback({"id": "q", "data": callback, "from": {"id": 42}, "message": {"chat": {"id": 42}, "message_id": 1}})
            self.assertEqual(manners.active(app)["id"], "socrates")
            await tg.commands.dispatch(app, 42, env.sid, "style", "off")
            self.assertIsNone(manners.active(app))
            self.assertIn("свой голос", self.sent(env)[-1]["text"])
            self.assertEqual([r for r in env.fake.state.requests if r.get("api") == "openai"], [])
