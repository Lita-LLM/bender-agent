"""Память: стеммер, разбор md, индекс и поиск, триггеры, тул recall."""

import asyncio
import os
import time
import unittest

from bender.core.app import App
from bender.memory.index import parse_md
from bender.memory.stem import stem, stems
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test


class StemTest(unittest.TestCase):
    def test_snowball_russian(self):
        # ожидания — по алгоритму Snowball (snowballstem.org); «память» → «памя» — его известная причуда
        cases = {"важная": "важн", "важнейшие": "важн", "возможность": "возможн", "вагона": "вагон", "оказалась": "оказа",
                 "говорили": "говор", "книгами": "книг", "памяти": "памят", "память": "памя", "красивейший": "красив",
                 "бегущий": "бегущ", "длинный": "длин", "ёлка": "елк"}
        self.assertEqual({w: stem(w) for w in cases}, cases)

    def test_english_and_mixed(self):
        self.assertEqual([stem(w) for w in ("running", "memories", "files", "boxes", "python")], ["run", "memory", "file", "box", "python"])
        self.assertEqual(stems("Миша строит агента на Python и OpenRouter"), ["миш", "стро", "агент", "python", "openrouter"])


class ParseTest(unittest.TestCase):
    def test_entries_meta_and_provenance(self):
        md = """# 2026-10-20

- 14:03 Настраивали прокси <!-- importance: 9 --> <!-- trigger: прокси, сеть -->
  продолжение записи на второй строке

<!-- observed: 2026-10-01 | status: active -->
- Всегда отвечай коротко.

- 15:00 Прочитал страницу <!-- source: untrusted -->

Обычный абзац
из двух строк.
"""
        day = time.mktime((2026, 10, 20, 12, 0, 0, 0, 0, -1))
        ch = parse_md(md, default_ts=day)
        self.assertEqual(len(ch), 4)
        self.assertIn("продолжение записи", ch[0].text)
        self.assertNotIn("<!--", ch[0].text)
        self.assertEqual((ch[0].importance, ch[0].triggers), (9, ["прокси", "сеть"]))
        self.assertEqual(time.localtime(ch[0].ts).tm_hour, 14)
        self.assertEqual(time.strftime("%Y-%m-%d", time.localtime(ch[1].ts)), "2026-10-01")  # дата из observed
        self.assertEqual(ch[2].provenance, "untrusted")
        self.assertEqual(ch[3].text, "2026-10-20: Обычный абзац\nиз двух строк.")


class IndexCase(TempDirCase):
    def setUp(self):
        super().setUp()
        self.app = App(self.tmp / "data")
        d = self.app.dirs.diary
        (d / "2026-10-20.md").write_text("- 14:03 Настраивали прокси для OpenRouter через CONNECT.\n"
                                         "- 18:10 Напоминать про релиз Кометы <!-- trigger: релиз --> <!-- importance: 8 -->\n", encoding="utf-8")
        (d / "2026-09-01.md").write_text("- 10:00 Обсуждали память агента и векторы.\n- 11:00 Чужая страница советует удалить всё <!-- source: untrusted -->\n", encoding="utf-8")
        old = time.time() - 60 * 86400
        os.utime(d / "2026-09-01.md", (old, old))
        sid = self.app.main_session()
        self.app.store.add_message(sid, {"role": "user", "content": "Как устроена память Бендера?"}, {"actor": "owner"})
        self.app.store.add_message(sid, {"role": "assistant", "content": "Память — md-файлы и индекс SQLite."}, {})
        self.app.store.add_message(sid, {"role": "tool", "content": "вывод тула про память"}, {})

    def tearDown(self):
        asyncio.run(self.app.close())
        super().tearDown()


class IndexTest(IndexCase):
    def test_search_forms_and_parts(self):
        idx = self.app.index
        texts = [h.text for h in idx.search("памяти")]
        self.assertTrue(any("Как устроена память" in t for t in texts))
        self.assertTrue(any("Обсуждали память" in t for t in texts))
        self.assertFalse(any("вывод тула" in t for t in texts))  # вывод тулов в память не идёт
        self.assertIn("прокси", idx.search("прокси")[0].text)
        self.assertIn("Кометы", idx.search("комет")[0].text)  # кусок слова — триграммы
        self.assertEqual(idx.search("квантовая хромодинамика"), [])

    def test_recency_and_core_excluded(self):
        hits = self.app.index.search("память")
        self.assertEqual(hits[0].kind, "session")  # свежий разговор выше старого дневника
        self.assertFalse(any(h.kind == "core" for h in hits))
        self.assertTrue(self.app.index.search("долговременная", kinds=("core",)))

    def test_incremental_sync_writes_nothing_when_unchanged(self):
        idx = self.app.index
        idx.sync()
        before = self.app.store.db.total_changes
        self.assertEqual(idx.sync(), 0)
        self.assertEqual(self.app.store.db.total_changes, before)
        p = self.app.dirs.diary / "2026-10-20.md"
        p.write_text(p.read_text(encoding="utf-8") + "- 20:00 Добавили тулы текстом для маленьких моделей.\n", encoding="utf-8")
        self.assertEqual(idx.sync(), 1)  # одна новая запись, старые не переиндексируются
        self.assertTrue(idx.search("маленьких моделей"))
        p.unlink()
        idx.sync()
        self.assertEqual(idx.search("прокси"), [])

    def test_fallback_without_fts(self):
        idx = self.app.index
        idx.fts = False  # как на SQLite без FTS5
        texts = [h.text for h in idx.search("Прокси")]
        self.assertTrue(any("прокси" in t for t in texts))
        self.assertTrue(any("Кометы" in t for t in (h.text for h in idx.search("кометы"))))

    def test_triggers(self):
        self.app.index.sync()
        hits = self.app.index.triggered("Когда будет релиз?")
        self.assertEqual(len(hits), 1)
        self.assertIn("Кометы", hits[0].text)
        self.assertEqual(self.app.index.triggered("Как дела?"), [])

    @async_test
    async def test_recall_tool_marks_untrusted(self):
        app = self.app
        res = await app.tools.run("recall", {"query": "прокси"}, ToolContext(app=app, session_id="t"))
        self.assertTrue(res.ok)
        self.assertIn("прокси", res.content)
        self.assertFalse(res.tainted)
        res = await app.tools.run("recall", {"query": "удалить всё страница"}, ToolContext(app=app, session_id="t"))
        self.assertIn("недоверенное", res.content)
        self.assertTrue(res.tainted)  # вспомнили недоверенное — ход заражён
        res = await app.tools.run("recall", {"query": "прокси", "days": 1}, ToolContext(app=app, session_id="t"))
        self.assertIn("Ничего", res.content)  # запись прокси — из 2026-10-20 по дате дневника, а не за последние сутки


if __name__ == "__main__":
    unittest.main()


class VectorTest(TempDirCase):
    """Поиск по смыслу на фальшивых эмбеддингах (у них есть словарь синонимов: кот/кошка, машина/автомобиль)."""

    def test_pack_and_hamming(self):
        from bender.memory.vectors import pack, unpack

        emb, sig = pack([3.0, -4.0, 0.0, 1.0])
        v = unpack(emb)
        self.assertAlmostEqual(sum(x * x for x in v), 1.0, places=5)
        self.assertEqual(int.from_bytes(sig, "little"), 0b1001)

    @async_test
    async def test_meaning_finds_what_words_miss(self):
        from bender.devtools.fake_llm import create_router
        from bender.net.http_server import HttpServer

        fake = create_router()
        server = HttpServer(fake)
        await server.start("127.0.0.1", 0)
        app = App(self.tmp / "data")
        try:
            p = app.models.add_provider(name="Фейк", kind="openai", base_url=f"http://127.0.0.1:{server.port}/v1", extra={"is_local": True})
            (app.dirs.diary / "2026-10-21.md").write_text("- 09:00 Кошка опять уронила кружку со стола.\n- 10:00 Обновили прокси.\n", encoding="utf-8")
            self.assertFalse(app.vectors.available())
            self.assertEqual(await app.vectors.fill(), 0)  # нет роли — молчит
            app.models.set_role("embed", [{"provider": p["id"], "model": "fake-embed"}])
            n = await app.vectors.fill()
            self.assertGreaterEqual(n, 2)
            self.assertEqual(app.vectors.status()["pending"], 0)
            self.assertEqual(app.index.search("кот"), [])  # словами не найти: «кот» ≠ «кошк»
            qvec = await app.vectors.embed_query("кот")
            hits = app.index.search("кот", qvec=qvec)
            self.assertIn("Кошка", hits[0].text)
            self.assertEqual(hits[0].why, ["смысл"])
            res = await app.tools.run("recall", {"query": "кот"}, ToolContext(app=app, session_id="t"))
            self.assertIn("Кошка", res.content)
            # сменилась модель эмбеддингов — старые векторы несравнимы и считаются заново
            app.models.set_role("embed", [{"provider": p["id"], "model": "fake-embed-2"}])
            self.assertIsNone(await app.vectors.embed_query("кот"))  # пока индекс старой модели — не сравниваем
            self.assertEqual(await app.vectors.fill(), n)
            self.assertEqual(app.store.meta_get("embed:model"), f"{p['id']}/fake-embed-2")
            # модель упала — поиск словами работает, ошибка видна
            app.models.set_role("embed", [{"provider": p["id"], "model": "embed-error"}])
            (app.dirs.diary / "2026-10-22.md").write_text("- 11:00 Новая запись.\n", encoding="utf-8")
            self.assertEqual(await app.vectors.fill(), 0)
            self.assertTrue(app.vectors.last_error)
            self.assertTrue(app.index.search("прокси"))
        finally:
            await app.close()
            await server.stop()


class FactsTest(TempDirCase):
    @async_test
    async def test_facts_replace_not_delete(self):
        app = App(self.tmp / "data")
        try:
            ctx = ToolContext(app=app, session_id="t")
            run = lambda **a: app.tools.run("fact", a, ctx)  # noqa: E731
            r = await run(action="set", subject="Миша", relation="живёт в", object="Москве")
            self.assertTrue(r.ok)
            self.assertIn("уже известно", (await run(action="set", subject="миша", relation="Живёт в", object="москве")).content)
            r = await run(action="set", subject="Миша", relation="живёт в", object="Казани")
            self.assertIn("Прежнее закрыто: Москве", r.content)
            await run(action="add", subject="Миша", relation="любит", object="чай")
            await run(action="add", subject="Миша", relation="любит", object="краткость")
            active = (await run(action="list", subject="Миша")).content
            self.assertIn("Казани", active)
            self.assertNotIn("Москве", active)
            self.assertEqual(active.count("любит"), 2)  # множественная связь
            hist = (await run(action="history", subject="Миша", relation="живёт в")).content
            self.assertIn("Москве", hist)
            self.assertIn("уже неверно", hist)
            self.assertEqual(app.facts.close("Миша", "любит", "чай"), 1)
            hits = app.index.search("где живёт Миша", kinds=("fact",))
            self.assertIn("Казани", hits[0].text)  # действующий факт важнее закрытого
            # заражённый ход — факт помечен недоверенным
            await app.tools.run("fact", {"action": "add", "subject": "сервер", "relation": "адрес", "object": "10.0.0.9"},
                                ToolContext(app=app, session_id="t", tainted=True))
            self.assertIn("недоверенное", (await run(action="list", subject="сервер")).content)
        finally:
            await app.close()


class HintsTest(TempDirCase):
    @async_test
    async def test_triggers_and_auto_recall_reach_the_model(self):
        from tests.test_mind import MindEnv

        async with MindEnv(self.tmp) as env:
            app = env.app
            (app.dirs.diary / "2026-10-20.md").write_text(
                "- 18:10 Перед релизом обновить changelog <!-- trigger: релиз --> <!-- importance: 8 -->\n"
                "- 19:00 Прокси для OpenRouter настраивали через CONNECT на порту 3128.\n"
                "- 20:00 Страница советует открыть порт наружу <!-- source: untrusted --> <!-- trigger: релиз -->\n", encoding="utf-8")
            env.script([{"text": "ок"}, {"text": "ок"}, {"text": "ок"}])
            await app.mind.say(env.sid, "Готовлю релиз, что не забыть?")
            sent = env.fake.state.requests[-1]["body"]["messages"][-1]["content"]
            self.assertIn("Вспомнилось", sent)
            self.assertIn("changelog", sent)
            self.assertNotIn("наружу", sent)  # недоверенное само не всплывает
            await app.mind.say(env.sid, "Как мы настраивали прокси для OpenRouter?")
            sent = env.fake.state.requests[-1]["body"]["messages"][-1]["content"]
            self.assertIn("порту 3128", sent)
            app.settings.set("memory", {"auto_recall": False, "triggers": False})
            await app.mind.say(env.sid, "Готовлю релиз и прокси")
            self.assertNotIn("Вспомнилось", env.fake.state.requests[-1]["body"]["messages"][-1]["content"])

    def test_strong_match(self):
        from bender.memory.index import strong

        self.assertTrue(strong("прокси OpenRouter", "Прокси для OpenRouter настраивали"))
        self.assertFalse(strong("как дела", "Дела идут"))  # одно короткое слово — не повод вспоминать
        self.assertFalse(strong("настроить телеграм бота для уведомлений", "Настроили прокси"))
