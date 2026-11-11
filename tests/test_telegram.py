"""Telegram на поддельном Bot API (fake-llm): привязка кодом, чужие, ответы, кнопки одобрения, команды, уведомления."""

import asyncio
import time
import unittest

from bender.channels.telegram import OWNER_META, TOKEN_SECRET, md_to_html, split
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv, call


class FormatTest(unittest.TestCase):
    def test_markdown_to_telegram_html(self):
        self.assertEqual(md_to_html("**жирно** и `код` <тег> & [сайт](https://x.ru)"),
                         '<b>жирно</b> и <code>код</code> &lt;тег&gt; &amp; <a href="https://x.ru">сайт</a>')
        self.assertEqual(md_to_html("```python\nprint(1 < 2)\n```"), "<pre>print(1 &lt; 2)</pre>")

    def test_split_long(self):
        text = "\n\n".join(f"Абзац {i} " + "слово " * 150 for i in range(10))
        parts = split(text, 2000)
        self.assertTrue(all(len(p) <= 2000 for p in parts))
        self.assertEqual("".join(p.replace("\n", "").replace(" ", "") for p in parts), text.replace("\n", "").replace(" ", ""))

    def test_headings_tables_lists_leave_code_alone(self):
        for level in range(1, 7):
            self.assertEqual(md_to_html('#' * level + ' Заголовок'), '<b>Заголовок</b>')
        result = md_to_html('| Имя | Цена |\n| :--- | ---: |\n| **Бендер** | `0` |\n\n- раз\n* два')
        self.assertEqual(result, '• Имя — Цена\n\n• <b>Бендер</b> — <code>0</code>\n\n• раз\n• два')
        code = '### текст\n| a | b |\n| --- | --- |\n- список\n* ещё'
        self.assertEqual(md_to_html('```text\n' + code + '\n```'), '<pre>' + code + '</pre>')


class TelegramTest(TempDirCase):
    async def _up(self, env, owner=False):
        app = env.app
        # live_via=draft: эти тесты считают отправленные сообщения; статус-сообщение по умолчанию проверяется отдельно (test_telegram_live).
        app.settings.set("telegram", {"api_base": env.base + "/tg", "command_languages":["ru","en"],'agent_label':False,'live_via':'draft'})
        app.secrets.set(TOKEN_SECRET, env.fake.state.tg_token)
        app.settings.set("telegram", {"enabled": True})
        if owner:
            app.store.meta_set(OWNER_META, {"user_id": 42, "chat_id": 42, "name": "Хозяин"})
        app.telegram.start()
        await self.until(lambda: app.telegram.state == "ok")

    def push(self, env, text="", uid=42, **kw):
        st = env.fake.state
        upd = {"update_id": st.tg_next_id, **kw} if kw else {"update_id": st.tg_next_id, "message": {
            "message_id": 1000 + st.tg_next_id, "from": {"id": uid, "is_bot": False, "first_name": "Хозяин" if uid == 42 else "Чужой"},
            "chat": {"id": uid, "type": "private"}, "date": int(time.time()), "text": text}}
        st.tg_next_id += 1
        st.tg_updates.append(upd)
        if st.tg_event:
            st.tg_event.set()

    def sent(self, env, method="sendMessage"):
        return [s for s in env.fake.state.tg_sent if s["method"] == method]

    async def until(self, cond, timeout=8.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if cond():
                return True
            await asyncio.sleep(0.03)
        raise AssertionError("не дождался")

    @async_test
    async def test_bind_with_code_and_strangers(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            await self._up(env)
            self.push(env, "привет", uid=99)
            await self.until(lambda: self.sent(env))
            self.assertIn("пришли код", self.sent(env)[0]["text"])
            code = app.telegram.new_code()
            self.push(env, f"/start {code.lower()}")
            await self.until(lambda: app.telegram.owner())
            self.assertEqual(app.telegram.owner()["user_id"], 42)
            await self.until(lambda: any("Привязал" in s["text"] for s in self.sent(env)))
            n = len(self.sent(env))
            self.push(env, "я тоже хозяин", uid=99)  # чужой после привязки: агенту не идёт; отвечали меньше часа назад — молчим
            self.push(env, "/status")
            await self.until(lambda: any("Самочувствие" in s["text"] for s in self.sent(env)))
            self.assertEqual(len(self.sent(env)), n + 1)
            self.assertEqual([r for r in env.fake.state.requests if r.get("api") == "openai"], [])  # модель не звали
            self.assertTrue(app.journal.verify()["ok"])

    @async_test
    async def test_owner_message_goes_to_main_session_and_back(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            await self._up(env, owner=True)
            env.script([{"text": "**Привет**, хозяин. `ok`"}])
            self.push(env, "как дела?")
            await self.until(lambda: self.sent(env))
            m = self.sent(env)[0]
            self.assertEqual(m["text"], "<b>Привет</b>, хозяин. <code>ok</code>")
            self.assertEqual(m["parse_mode"], "HTML")
            self.assertNotIn("reply_parameters", m)  # обычный ответ; явный номер проверяется отдельно
            msgs = app.store.messages(app.main_session())
            self.assertEqual(msgs[0]["content"], "как дела?")
            self.assertEqual((msgs[0]["meta"]["channel"], msgs[0]["meta"]["actor"]), ("telegram", "owner"))
            req = [r for r in env.fake.state.requests if r.get("api") == "openai"][0]["body"]["messages"][-1]["content"]
            self.assertIn("канал: telegram", req)
            self.assertTrue(self.sent(env, "sendChatAction"))  # «печатает…»

    @async_test
    async def test_task_answer_follows_home_once_success_and_error(self):
        for home in ("telegram", "pult"):
            for failed in (False, True):
                async with MindEnv(self.tmp / (home + str(failed))) as env:
                    app = env.app
                    await self._up(env, owner=True)
                    pid = app.models.roles()["main"][0]["provider"]
                    app.models.set_role("fast", [{"provider": pid, "model": "script"}])
                    env.script([{"text": "Начинаю."}])
                    await app.mind.say(env.sid, "поручаю", channel=home)
                    await self.until(lambda: not app.telegram._bg)
                    env.fake.state.tg_sent.clear()
                    env.script([{"error": 400, "message": "Unsupported format"} if failed else {"text": "Нашёл."}, {"text": "Вот итог работы."}])
                    task = app.team.spawn("scribe", "Проверь", parent_session=env.sid)
                    await self.until(lambda: app.team.get(task["id"])["delivered"])
                    await self.until(lambda: not app.mind.running(env.sid))
                    await self.until(lambda: not app.telegram._bg)
                    sent = self.sent(env)
                    self.assertEqual(len(sent), 1 if home == "telegram" else 0)
                    if sent:
                        self.assertIn("📋 Задача #", sent[0]["text"])
                        self.assertIn("Вот итог работы.", sent[0]["text"])
                        self.assertNotIn("[Задача", sent[0]["text"])
                    self.assertEqual(app.store.session(env.sid)["meta"]["home_channel"], home)

    @async_test
    async def test_pult_changes_telegram_home(self):
        async with MindEnv(self.tmp) as env:
            await self._up(env, owner=True)
            env.script([{"text": "Первый."}, {"text": "Второй."}, {"text": "Внутренний."}])
            await env.app.mind.say(env.sid, "раз", channel="telegram")
            await self.until(lambda: len(self.sent(env)) == 1)
            await env.app.mind.say(env.sid, "два", channel="pult")
            await env.app.mind.say(env.sid, "внутреннее", channel="team", actor="team")
            await self.until(lambda: not env.app.telegram._bg)
            self.assertEqual(len(self.sent(env)), 1)

    @async_test
    async def test_approval_by_button(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            await self._up(env, owner=True)
            outside = self.tmp / "вне" / "файл.txt"
            outside.parent.mkdir()
            env.script([call("write_file", path=str(outside), content="из телеграма"), {"text": "Записал."}])
            self.push(env, "запиши файл")
            await self.until(lambda: any(s.get("reply_markup") for s in self.sent(env)))
            card = next(s for s in self.sent(env) if s.get("reply_markup"))
            self.assertIn("Предохранитель спрашивает", card["text"])
            data = card["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
            self.assertTrue(data.startswith("ap:") and data.endswith(":once"))
            self.push(env, callback_query={"id": "cb1", "from": {"id": 99}, "data": data, "message": {"message_id": card["message_id"], "chat": {"id": 42}}})
            await self.until(lambda: self.sent(env, "answerCallbackQuery"))
            self.assertFalse(outside.exists())  # чужая кнопка не считается
            self.push(env, callback_query={"id": "cb2", "from": {"id": 42}, "data": data, "message": {"message_id": card["message_id"], "chat": {"id": 42}}})
            await self.until(lambda: any(s["text"] == "Записал." for s in self.sent(env)))
            self.assertEqual(outside.read_text(encoding="utf-8"), "из телеграма")

    @async_test
    async def test_stop_resume_and_notifications(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            await self._up(env, owner=True)
            # /stop прерывает идущий ход и не блокирует: следующее сообщение агент берёт сразу
            entered = asyncio.Event()
            @app.tools.tool('event_wait', 'Подождать', group='base')
            async def waiting(ctx):
                entered.set(); await asyncio.Event().wait()
            env.script([call('event_wait'), {'text': 'После стопа слышу.'}])
            self.push(env, "долгая задача")
            await asyncio.wait_for(entered.wait(), 5)
            self.push(env, "/stop")
            await self.until(lambda: any('Остановил' in s["text"] for s in self.sent(env)))
            self.assertFalse(app.guard.halted)
            self.push(env, "ты тут?")
            await self.until(lambda: any(s["text"] == "После стопа слышу." for s in self.sent(env)))
            self.push(env, "/estop")
            await self.until(lambda: app.guard.halted)
            self.push(env, "/resume")
            await self.until(lambda: not app.guard.halted)
            job = app.heart.add("вода", kind="remind", text="Выпей воды", at="через 20 минут")
            await app.heart.fire(app.heart.jobs.get(job.id), manual=True)
            await self.until(lambda: any(s["text"] == "⏰ Выпей воды" for s in self.sent(env)))
            app.settings.set("telegram", {"notify": False})
            await self.until(lambda: app.telegram.state == "ok")
            n = len(self.sent(env))
            await app.heart.fire(app.heart.jobs.get(app.heart.add("ещё", kind="remind", text="Снова", at="через 20 минут").id), manual=True)
            await asyncio.sleep(0.3)
            self.assertEqual(len(self.sent(env)), n)  # уведомления выключены

    @async_test
    async def test_bad_token(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            app.settings.set("telegram", {"api_base": env.base + "/tg", "enabled": True})
            app.secrets.set(TOKEN_SECRET, "999:WRONG")
            app.telegram.start()
            await self.until(lambda: app.telegram.state == "error")
            self.assertIn("не принял токен", app.telegram.last_error)


if __name__ == "__main__":
    unittest.main()
