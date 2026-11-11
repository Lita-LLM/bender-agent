"""Слово хозяина, пришедшее в конце хода (лимит шагов), не остаётся без ответа; после /stop ход сам не начинается."""
import asyncio

from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv, call


class TurnTailTest(TempDirCase):
    async def _owner_mid_turn(self, env, text):
        for _ in range(200):
            if env.app.mind.running(env.sid):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.1)
        await env.app.mind.say(env.sid, text, wait=False)

    @async_test
    async def test_message_after_step_limit_gets_new_turn(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            app.settings.set("memory", {"review": False, "compact": False})
            app.settings.set("agent", {"max_steps": 1})
            env.script([call("list_dir", path="."), {"text": "Отвечаю на второе."}])
            tools = app.tools.run

            async def slow(name, args, ctx):  # хозяин пишет, пока идёт тул
                if name == "list_dir":
                    await asyncio.sleep(0.6)
                return await tools(name, args, ctx)

            app.tools.run = slow
            first = asyncio.ensure_future(app.mind.say(env.sid, "Посмотри папку"))
            await self._owner_mid_turn(env, "А ещё скажи время")
            result = await first
            self.assertEqual(result["steps"], 1)
            self.assertTrue(any(m.get("kind") == "limit" for m in app.store.messages(env.sid)), "ход упёрся в лимит")
            for _ in range(200):
                if any(m["role"] == "assistant" and m.get("content") == "Отвечаю на второе." for m in app.store.messages(env.sid)):
                    break
                await asyncio.sleep(0.05)
            texts = [m.get("content") for m in app.store.messages(env.sid) if m["role"] in ("user", "assistant")]
            self.assertIn("Отвечаю на второе.", texts, "второе сообщение получило свой ход")
            last_request = str(env.fake.state.requests[-1]["body"]["messages"])
            self.assertIn("А ещё скажи время", last_request)
            for _ in range(100):
                if not app.mind.running(env.sid):
                    break
                await asyncio.sleep(0.02)
            self.assertEqual(len([m for m in app.store.messages(env.sid) if m["role"] == "user"]), 2, "сообщение не продублировано")

    @async_test
    async def test_stop_does_not_restart(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            app.settings.set("memory", {"review": False, "compact": False})
            env.script([{"text": "долго", "delay": 1.0}, {"text": "лишний ход"}])
            first = asyncio.ensure_future(app.mind.say(env.sid, "Начни"))
            await self._owner_mid_turn(env, "Подожди")
            app.mind.stop(env.sid)
            await first
            await asyncio.sleep(0.3)
            self.assertFalse(app.mind.running(env.sid))
            self.assertNotIn("лишний ход", [m.get("content") for m in app.store.messages(env.sid)])
