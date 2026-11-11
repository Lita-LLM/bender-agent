"""Сжатие разговора и разбор после хода (веха 2, шаг 4)."""

import json
import unittest

from bender.mind import compact, review, side
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv, call


def roles(env, main="script", fast="script"):
    pid = env.app.models.roles()["main"][0]["provider"]
    env.app.models.set_role("main", [{"provider": pid, "model": main}])
    env.app.models.set_role("fast", [{"provider": pid, "model": fast}])


def fast_calls(env):
    return [r for r in env.fake.state.requests if r.get("api") == "openai" and not r["body"].get("tools")]


class SideTest(unittest.TestCase):
    def test_parse_json_tolerates_wrapping(self):
        self.assertEqual(side.parse_json('Вот: ```json\n{"a": 1,}\n```'), {"a": 1})
        self.assertEqual(side.parse_json('думаю… {"summary": "x {y}", "diary": ""} конец'), {"summary": "x {y}", "diary": ""})
        self.assertEqual(side.parse_json("{}"), {})
        self.assertIsNone(side.parse_json("ничего"))

    def test_transcript_skips_recall_and_cuts_tools(self):
        rows = [{"role": "user", "content": "что я любил?"},
                {"role": "assistant", "content": "", "tool_calls": [{"id": "1", "name": "recall", "arguments": {"query": "любил"}}]},
                {"role": "tool", "name": "recall", "content": "Хозяин любит чай с бергамотом"},
                {"role": "tool", "name": "read_file", "content": "x" * 1000},
                {"role": "assistant", "content": "Чай."}]
        t = side.transcript(rows, skip_tools=("recall",))
        self.assertNotIn("бергамот", t)
        self.assertIn("recall(", t)
        self.assertIn("Хозяин: что я любил?", t)
        self.assertLess(len(t), 700)

    def test_similar(self):
        self.assertTrue(review.similar("Хозяин живёт в Казани", "хозяин живет в казани с 2025 года"))
        self.assertTrue(review.similar("Любит короткие ответы", "Хозяин любит короткие ответы без воды"))
        self.assertFalse(review.similar("Любит чай", "Работает программистом в Яндексе"))

    def test_plan_puts_boundary_before_owner_message(self):
        rows = []
        for i in range(10):
            rows += [{"id": f"u{i}", "role": "user"}, {"id": f"a{i}", "role": "assistant"}, {"id": f"t{i}", "role": "tool"}]
        sizes = [100] * len(rows)
        k = compact.plan(rows, sizes, 2000)  # 3000 > 0.85·2000 — пора; оставить ≈1000
        self.assertIsNotNone(k)
        self.assertEqual(rows[k]["role"], "user")
        self.assertLessEqual(sum(sizes[k:]), 1100)
        self.assertIsNone(compact.plan(rows, sizes, 10000))  # влезает — не трогать
        # Хозяин попросил сам — сжать и короткий разговор, оставив последнюю четверть.
        k = compact.plan(rows, sizes, 100000, force=True)
        self.assertEqual(rows[k]["role"], "user")
        self.assertLessEqual(sum(sizes[k:]), 900)
        self.assertEqual(compact.plan(rows[:6], sizes[:6], 100000, force=True), 3)
        self.assertIsNone(compact.plan(rows[:3], sizes[:3], 100000, force=True))  # одна реплика — пересказывать нечего


class ReviewTest(TempDirCase):
    @async_test
    async def test_review_saves_memory_user_facts_diary(self):
        async with MindEnv(self.tmp) as env:
            roles(env)
            env.script([{"text": "Понял, буду короче."},
                        {"text": json.dumps({"user": ["Всегда отвечай коротко"], "memory": ["Хозяин переехал в Казань"],
                                             "facts": [{"subject": "хозяин", "relation": "город", "object": "Казань", "single": True}],
                                             "diary": "Хозяин рассказал о переезде"}, ensure_ascii=False)}])
            res = await env.app.mind.say(env.sid, "Я переехал в Казань. И отвечай короче, пожалуйста.")
            self.assertIsNone(res["error"])
            await env.app.mind.idle()
            self.assertIn("Всегда отвечай коротко", env.app.selfdir.read("USER.md"))
            self.assertIn("<!-- observed:", env.app.selfdir.read("USER.md"))
            self.assertIn("Хозяин переехал в Казань <!-- source: review -->", env.app.selfdir.read("MEMORY.md"))
            self.assertEqual([f["object"] for f in env.app.facts.active("хозяин", "город")], ["Казань"])
            diary = list(env.app.dirs.diary.glob("*.md"))[0].read_text(encoding="utf-8")
            self.assertIn("Хозяин рассказал о переезде", diary)
            ev = [m for m in env.app.store.messages(env.sid) if m.get("kind") == "memory"]
            self.assertEqual(len(ev), 1)
            self.assertTrue(ev[0]["content"].startswith("💾 Запомнил:"))
            self.assertIn("Казань", ev[0]["content"])
            # в разбор ушли стенограмма хода и текущая память, без тулов
            req = fast_calls(env)[-1]["body"]
            self.assertIn("Хозяин: Я переехал в Казань", req["messages"][1]["content"])
            self.assertIn("MEMORY.md сейчас", req["messages"][1]["content"])
            self.assertTrue(env.app.journal.verify()["ok"])
            calls = env.app.store.q("SELECT notice FROM calls WHERE role = 'fast'")
            self.assertEqual([c["notice"] for c in calls], ["разбор после хода"])

    @async_test
    async def test_review_dedups_filters_secrets_and_overflows(self):
        async with MindEnv(self.tmp) as env:
            roles(env)
            sd = env.app.selfdir
            filler = "".join(f"- Старая запись номер {i} про всякое\n" for i in range(15))
            sd.write("MEMORY.md", sd.read("MEMORY.md").rstrip() + "\n\n" + filler + "- Хозяин живёт в Казани\n")
            env.app.settings.set("memory", {"core_limit": len(sd.read("MEMORY.md")) + 60})
            env.script([{"text": "Ок."},
                        {"text": json.dumps({"memory": ["хозяин живет в Казани", "Ключ от сервера: sk-abcdefghijklmnopqrstuv",
                                                        "Хозяин держит кота Барсика и двух попугаев, а ещё мечтает о собаке породы корги"],
                                             "user": ["пароль: hunter2hunter2"]}, ensure_ascii=False)}])
            await env.app.mind.say(env.sid, "Кстати, у меня кот Барсик, а ключ от сервера sk-abcdefghijklmnopqrstuv")
            await env.app.mind.idle()
            mem = sd.read("MEMORY.md")
            self.assertEqual(mem.count("Казан"), 1)  # похожее не дублируется
            self.assertNotIn("sk-", mem)
            self.assertNotIn("hunter2", sd.read("USER.md"))
            self.assertNotIn("Барсик", mem)  # не влезло в ядро…
            diary = list(env.app.dirs.diary.glob("*.md"))[0].read_text(encoding="utf-8")
            self.assertIn("не влезло в MEMORY.md", diary)  # …и ушло в дневник
            ev = [m for m in env.app.store.messages(env.sid) if m.get("kind") == "memory"][0]
            self.assertIn("ядро памяти полно", ev["content"])

    @async_test
    async def test_no_review_after_tainted_turn_or_ack_or_when_off(self):
        async with MindEnv(self.tmp) as env:
            roles(env)
            env.auto_approve("deny")
            env.script([call("web_fetch", url=env.base + "/page"), {"text": "Прочитал."}])
            await env.app.mind.say(env.sid, "прочитай страницу и запомни главное")
            await env.app.mind.idle()
            self.assertEqual(fast_calls(env), [])  # заражённый ход — без разбора
            env.script([{"text": "Пожалуйста!"}])
            await env.app.mind.say(env.sid, "спасибо!")
            await env.app.mind.idle()
            self.assertEqual(fast_calls(env), [])  # «спасибо» — не о чем думать
            env.app.settings.set("memory", {"review": False})
            env.script([{"text": "Ясно."}])
            await env.app.mind.say(env.sid, "Я теперь работаю в школе учителем физики")
            await env.app.mind.idle()
            self.assertEqual(fast_calls(env), [])

    @async_test
    async def test_notices_off_still_saves(self):
        async with MindEnv(self.tmp) as env:
            roles(env)
            env.app.settings.set("agent", {"memory_notices": "off"})
            env.script([{"text": "Ок."}, {"text": '{"memory": ["Хозяин не ест мясо"]}'}])
            await env.app.mind.say(env.sid, "Я вегетарианец, учти")
            await env.app.mind.idle()
            self.assertIn("не ест мясо", env.app.selfdir.read("MEMORY.md"))
            self.assertEqual([m for m in env.app.store.messages(env.sid) if m.get("kind") == "memory"], [])


    @async_test
    async def test_without_fast_role_review_goes_in_batches(self):
        async with MindEnv(self.tmp) as env:
            env.app.settings.set("memory", {"review_batch": 2})  # «Быстрой» нет — разбор на Главной, пачкой
            env.script([{"text": "Записал."}])
            await env.app.mind.say(env.sid, "У меня есть сестра Маша")
            await env.app.mind.idle()
            self.assertEqual(env.app.store.q("SELECT COUNT(*) AS n FROM calls WHERE role = 'fast'")[0]["n"], 0)
            env.script([{"text": "Понял."}, {"text": '{"facts": [{"subject": "хозяин", "relation": "сестра", "object": "Маша"}]}'}])
            await env.app.mind.say(env.sid, "А брата у меня нет")
            await env.app.mind.idle()
            req = [r for r in env.fake.state.requests if not r["body"].get("tools")][-1]["body"]["messages"][1]["content"]
            self.assertIn("Последние ходы (2)", req)
            self.assertIn("сестра Маша", req)
            self.assertIn("брата у меня нет", req)
            self.assertEqual([f["object"] for f in env.app.facts.active("хозяин")], ["Маша"])
            self.assertEqual(env.app.store.session(env.sid)["meta"]["review"]["pending"], [])


class CompactTest(TempDirCase):
    def _fill(self, env, n):
        for i in range(n):
            env.app.store.add_message(env.sid, {"role": "user", "content": f"вопрос номер {i}: " + "расскажи подробнее про проект " * 6})
            env.app.store.add_message(env.sid, {"role": "assistant", "content": f"ответ номер {i}: " + "вот подробности о проекте " * 6})

    @async_test
    async def test_compaction_replaces_old_part_with_summary(self):
        async with MindEnv(self.tmp) as env:
            roles(env, main="echo")
            env.app.settings.set("memory", {"review": False})
            env.app.settings.set("agent", {"history_tokens": 1200})
            self._fill(env, 20)  # ≈ 2400 токенов — больше бюджета
            env.script([{"text": json.dumps({"summary": "Хозяин расспрашивал про проект; я рассказал подробности. ПЕРЕСКАЗ-1",
                                             "diary": "Долго обсуждали проект"}, ensure_ascii=False)}])
            res = await env.app.mind.say(env.sid, "ну и что дальше?")
            self.assertIsNone(res["error"])
            # первый ход шёл старым окном: старое не показано
            first = [r for r in env.fake.state.requests if r["body"].get("tools")][-1]["body"]["messages"]
            self.assertIn("не показаны", first[1]["content"])
            await env.app.mind.idle()
            st = compact.state(env.app, env.sid)
            self.assertIn("ПЕРЕСКАЗ-1", st["summary"])
            self.assertGreater(st["count"], 10)
            self.assertEqual(env.app.store.one("SELECT data FROM messages WHERE id > ? AND role IN ('user','assistant','tool') ORDER BY id LIMIT 1",
                                               (st["upto"],))["data"][:20].count('"user"'), 1)  # граница — перед сообщением хозяина
            diary = list(env.app.dirs.diary.glob("*.md"))[0].read_text(encoding="utf-8")
            self.assertIn("(разговор) Долго обсуждали проект", diary)
            # следующие ходы: пересказ вместо старого, начало запроса не меняется (кэш)
            await env.app.mind.say(env.sid, "а ещё?")
            await env.app.mind.say(env.sid, "и всё?")
            reqs = [r["body"]["messages"] for r in env.fake.state.requests if r["body"].get("tools")][-2:]
            self.assertIn("ПЕРЕСКАЗ-1", reqs[0][1]["content"])
            self.assertEqual(reqs[0][:3], reqs[1][:3])
            self.assertNotIn("вопрос номер 0:", json.dumps(reqs[1], ensure_ascii=False))
            # всё старое — на месте в базе и находится поиском
            self.assertTrue(env.app.index.search("вопрос номер 0", limit=5))
            self.assertTrue(env.app.journal.verify()["ok"])

    @async_test
    async def test_compaction_failure_falls_back_to_window(self):
        async with MindEnv(self.tmp) as env:
            roles(env, main="echo")
            env.app.settings.set("memory", {"review": False})
            env.app.settings.set("agent", {"history_tokens": 1200})
            self._fill(env, 20)
            env.script([{"error": 500, "message": "упало"}] * 3)
            res = await env.app.mind.say(env.sid, "дальше")
            await env.app.mind.idle()
            self.assertIsNone(res["error"])
            st = compact.state(env.app, env.sid)
            self.assertNotIn("summary", st)
            self.assertTrue(st.get("error"))
            n = len(fast_calls(env))
            await env.app.mind.say(env.sid, "ещё")  # повтор не раньше чем через 10 минут
            await env.app.mind.idle()
            self.assertEqual(len(fast_calls(env)), n)
            win = compact.window(env.app, env.sid, 1200)
            self.assertGreater(win.dropped, 0)
            self.assertEqual(win.messages[0]["role"], "user")

    @async_test
    async def test_compaction_off(self):
        async with MindEnv(self.tmp) as env:
            roles(env, main="echo")
            env.app.settings.set("memory", {"review": False, "compact": False})
            env.app.settings.set("agent", {"history_tokens": 1200})
            self._fill(env, 20)
            await env.app.mind.say(env.sid, "дальше")
            await env.app.mind.idle()
            self.assertEqual(fast_calls(env), [])
            self.assertEqual(compact.state(env.app, env.sid), {})


if __name__ == "__main__":
    unittest.main()


class NewPageTest(TempDirCase):
    @async_test
    async def test_new_page_daily_idle_and_manual(self):
        import time

        async with MindEnv(self.tmp) as env:
            app = env.app
            roles(env, main="echo")
            app.settings.set("memory", {"review": False})
            await app.mind.say(env.sid, "меня зовут Миша, запомни СТАРОЕ-СЛОВО")
            # выключено — граница не ставится
            self.assertIsNone(compact.page_due(app, env.sid))
            # каждый день: прошлое сообщение было вчера → новая страница
            app.settings.set("memory", {"new_page": "daily", "new_page_at": "04:00"})
            app.store.x("UPDATE messages SET created_at = created_at - 86400 * 2 WHERE session_id = ?", (env.sid,))
            self.assertEqual(compact.page_due(app, env.sid), "новый день")
            await app.mind.say(env.sid, "доброе утро")
            req = [r for r in env.fake.state.requests if r["body"].get("tools")][-1]["body"]["messages"]
            self.assertNotIn("СТАРОЕ-СЛОВО", json.dumps(req, ensure_ascii=False))  # старое в модель не ушло
            self.assertTrue(any(m.get("kind") == "page" for m in app.store.messages(env.sid)))
            self.assertTrue(app.index.search("СТАРОЕ-СЛОВО", limit=3))  # но находится поиском
            self.assertIsNone(compact.page_due(app, env.sid))  # сегодня уже была
            # после паузы
            app.settings.set("memory", {"new_page": "idle", "new_page_idle_hours": 2})
            self.assertIsNone(compact.page_due(app, env.sid))
            self.assertEqual(compact.page_due(app, env.sid, now=time.time() + 3 * 3600), "после паузы больше 2 ч")
            # руками; второй раз подряд — нечего
            app.store.update_session(env.sid, meta={"cc:p1": {"session": "x", "upto": "y"}})
            self.assertTrue(compact.new_page(app, env.sid))
            self.assertFalse(compact.new_page(app, env.sid))
            self.assertEqual(app.store.session(env.sid)["meta"]["cc:p1"], {})  # Claude Code тоже начнёт заново
            # сжатие после новой страницы считает от неё
            st = compact.state(app, env.sid)
            self.assertEqual(compact.boundary(st), st["upto"])
            # хозяйская граница не мешает сжатию выключенным: окно всё равно с границы
            app.settings.set("memory", {"compact": False})
            self.assertEqual(compact.window(app, env.sid, 5000).messages, [])
