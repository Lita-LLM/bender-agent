"""Бот не глохнет: долгая обработка, пауза Telegram и сбой опроса не отрезают хозяина."""
import asyncio
from unittest.mock import patch

from bender.channels import telegram as tgmod
from bender.channels.telegram import TelegramError
from bender.guard.policy import Action
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv
from tests import test_telegram as original


class StuckTelegramTest(TempDirCase):
    _up = original.TelegramTest._up
    push = original.TelegramTest.push
    sent = original.TelegramTest.sent
    until = original.TelegramTest.until

    def texts(self, env):
        return [s.get("text", "") for s in self.sent(env)]

    async def approve(self, env, app):
        """Карточка одобрения приходит в Telegram, нажатие кнопки доходит до Предохранителя."""
        decision = asyncio.ensure_future(app.guard.require_owner(Action("exec", "echo hi", actor="agent", summary="тестовая команда"), "проверка"))
        await self.until(lambda: any("Предохранитель спрашивает" in t for t in self.texts(env)))
        card = next(s for s in self.sent(env) if "Предохранитель спрашивает" in s.get("text", ""))
        data = card["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        self.push(env, callback_query={"id": "cb1", "from": {"id": 42}, "data": data,
                                       "message": {"message_id": 5, "chat": {"id": 42, "type": "private"}}})
        return await asyncio.wait_for(decision, 5)

    @async_test
    async def test_stuck_command_does_not_block_buttons_commands_or_next_message(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            await self._up(env, owner=True)
            tg = app.telegram
            started, release = asyncio.Event(), asyncio.Event()
            execute = tg.commands.execute

            async def slow(ctx, chat, sid, command, arg):
                if command == "backup":
                    started.set()
                    await release.wait()  # бекап, вызов модели или пауза Telegram, которые долго не кончаются
                    return
                return await execute(ctx, chat, sid, command, arg)

            with patch.object(tg.commands, "execute", slow), patch.object(tgmod, "ORDER_WAIT", 0.3):
                self.push(env, "/backup")
                await asyncio.wait_for(started.wait(), 5)
                self.push(env, "/status")  # срочная команда — мимо очереди чата
                await self.until(lambda: any("Самочувствие" in t for t in self.texts(env)))
                self.assertTrue((await self.approve(env, app)).allowed)
                env.script([{"text": "Слышу тебя."}])
                self.push(env, "ты тут?")  # обычное сообщение того же чата ждёт недолго и идёт само
                await self.until(lambda: "Слышу тебя." in self.texts(env))
                release.set()
                await self.until(lambda: not tg._bg)

    @async_test
    async def test_long_telegram_pause_is_visible_and_polling_continues(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            await self._up(env, owner=True)
            tg = app.telegram
            api = tg.api
            flooded = []

            async def limited(method, payload=None, **kw):
                if method == "sendMessage" and not flooded:
                    flooded.append(payload)
                    raise TelegramError("tg_api", "Telegram: Too Many Requests", method, data={"code": 429, "retry_after": 3600})
                return await api(method, payload, **kw)

            with patch.object(tg, "api", limited):
                self.push(env, "/help")
                await self.until(lambda: flooded)
                await self.until(lambda: tg.public()["pause"] > 3500)
                self.assertTrue(any("паузу 3600 с" in r["summary"] for r in app.journal.recent(kind="channel")))
                # Ответ ждёт паузу, но опрос жив: нажатие кнопки одобрения доходит.
                tg._retry_until.clear()  # карточке нужно уйти, чтобы было что нажать
                self.assertTrue((await self.approve(env, app)).allowed)

    @async_test
    async def test_answer_survives_poll_error_and_network_blip(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            await self._up(env, owner=True)
            tg = app.telegram
            api = tg.api
            failed = []

            async def blip(method, payload=None, **kw):
                if method == "sendMessage" and not failed:
                    failed.append(method)
                    raise TelegramError("tg_net", "Нет связи с Telegram", "оборвалось")
                return await api(method, payload, **kw)

            tg.state = "error"  # опрос только что споткнулся о сеть и ещё не переподключился
            env.script([{"text": "Ответ дошёл."}])
            with patch.object(tg, "api", blip), patch.object(tgmod, "ANSWER_RETRY", (0.05, 0.05, 0.05)):
                await app.mind.say(env.sid, "привет", channel="telegram")
                await self.until(lambda: "Ответ дошёл." in self.texts(env))
            self.assertEqual(failed, ["sendMessage"])
