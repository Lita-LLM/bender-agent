"""Сердце, шаг 1: время словами, cron, задачи (напоминание, ход агента, рефлекс), полномочия, пропуски, бегун."""

import asyncio
import datetime as dt
import json
import time
import unittest

from bender.core.errors import HumanError
from bender.heart import cron, when
from bender.heart.jobs import make_schedule, next_fire
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv, call

NOW = time.mktime((2026, 10, 22, 14, 30, 0, 0, 0, -1))  # четверг, 14:30 местного


def at(ts):
    return dt.datetime.fromtimestamp(ts).strftime("%a %d.%m %H:%M")


class TimeTest(unittest.TestCase):
    def test_when_parses_human_time(self):
        cases = {"через 20 минут": "Thu 22.10 14:50", "+1h30m": "Thu 22.10 16:00", "завтра в 9": "Fri 23.10 09:00",
                 "в пятницу 18:30": "Fri 23.10 18:30", "23.10 10:00": "Fri 23.10 10:00", "2026-10-23 09:00": "Fri 23.10 09:00",
                 "18:00": "Thu 22.10 18:00", "10:00": "Fri 23.10 10:00", "в 7 вечера": "Thu 22.10 19:00", "через полчаса": "Thu 22.10 15:00",
                 "чт 10:00": "Thu 29.10 10:00", "in 15 minutes": "Thu 22.10 14:45"}
        for text, want in cases.items():
            self.assertEqual(at(when.parse(text, NOW)), want, text)
        for bad in ("когда-нибудь", "2026-10-01 10:00", "31.02", "25:00"):
            with self.assertRaises(HumanError, msg=bad):
                when.parse(bad, NOW)

    def test_every_and_window(self):
        self.assertEqual(when.every("30m"), 1800)
        self.assertEqual(when.every("каждые 2 часа"), 7200)
        self.assertEqual(when.every("каждый час"), 3600)
        with self.assertRaises(HumanError):
            when.every("10s")  # чаще раза в минуту — нельзя
        night = time.mktime((2026, 10, 22, 23, 30, 0, 0, 0, -1))
        self.assertTrue(when.window_ok("22:00-06:00", night))
        self.assertFalse(when.window_ok("08:00-23:00", night))
        self.assertEqual(at(when.next_window_start("08:00-23:00", night)), "Fri 23.10 08:00")

    def test_cron(self):
        c = cron.parse("0 9 * * 1-5")
        self.assertEqual(c.describe(), "в 09:00 по будням")
        self.assertEqual(at(c.next_after(NOW)), "Fri 23.10 09:00")
        self.assertEqual(at(c.next_after(c.next_after(NOW))), "Mon 26.10 09:00")
        self.assertEqual(at(cron.parse("*/15 * * * *").next_after(NOW)), "Thu 22.10 14:45")
        self.assertEqual(at(cron.parse("0 0 29 2 *").next_after(NOW)), "Tue 29.02 00:00")  # 2028
        self.assertEqual(at(cron.parse("0 10 13 * fri").next_after(NOW)), "Fri 23.10 10:00")  # 13-е ИЛИ пятница
        self.assertEqual(at(cron.parse("@daily").next_after(NOW)), "Fri 23.10 00:00")
        self.assertEqual(cron.parse("0 9 * * 0").weekdays, cron.parse("0 9 * * 7").weekdays)
        for bad in ("0 9 * *", "60 * * * *", "* * * * 9", "5-1 * * * *", "*/0 * * * *"):
            with self.assertRaises(HumanError, msg=bad):
                cron.parse(bad)

    def test_schedule_with_window(self):
        s = make_schedule(every="1h", now=NOW)
        self.assertEqual(at(next_fire(s, NOW)), "Thu 22.10 15:30")
        late = time.mktime((2026, 10, 22, 22, 45, 0, 0, 0, -1))
        self.assertEqual(at(next_fire(s, late, window="08:00-23:00")), "Fri 23.10 08:00")  # 23:30 вне окна → утро
        c = make_schedule(cron="0 * * * *")
        self.assertEqual(at(next_fire(c, late, window="08:00-23:00")), "Fri 23.10 08:00")


class HeartTest(TempDirCase):
    def ev(self, env, kind):
        return [m for m in env.app.store.messages(env.app.main_session()) if m.get("kind") == kind]

    @async_test
    async def test_remind_fires_once_into_chat(self):
        async with MindEnv(self.tmp) as env:
            seen = []
            env.app.bus.listen(lambda e: seen.append(e) if e.get("type") == "notify" else None)
            job = env.app.heart.add("хлеб", kind="remind", text="Купить хлеб", at="через 20 минут")
            res = await env.app.heart.fire(env.app.heart.jobs.get(job.id), manual=True)
            self.assertEqual(res["status"], "ok")
            self.assertEqual([m["content"] for m in self.ev(env, "remind")], ["⏰ Купить хлеб"])
            self.assertEqual(seen[0]["text"], "Купить хлеб")
            j = env.app.heart.jobs.get(job.id)
            self.assertFalse(j.enabled)  # разовое — выключилось
            self.assertIsNone(j.next_run)
            self.assertEqual(j.runs, 1)
            self.assertEqual(env.app.heart.jobs.history(job.id)[0]["status"], "ok")
            self.assertTrue(env.app.journal.verify()["ok"])

    @async_test
    async def test_agent_job_in_main_is_unattended(self):
        async with MindEnv(self.tmp) as env:
            env.auto_approve()  # если бы спросили — тест бы это увидел
            env.script([call("write_file", path="/tmp/вне-папки.txt", content="x"), {"text": "Сводка: всё тихо."}])
            job = env.app.heart.add("сводка", kind="agent", prompt="Пришли сводку", cron="0 9 * * *")
            res = await env.app.heart.fire(env.app.heart.jobs.get(job.id), manual=True)
            self.assertEqual(res["status"], "ok")
            msgs = env.app.store.messages(env.app.main_session())
            first = [m for m in msgs if m["role"] == "user"][0]
            self.assertTrue(first["content"].startswith("[Задача по расписанию «сводка»"))
            self.assertEqual(first["meta"]["actor"], "schedule")
            self.assertNotIn("untrusted", first["meta"])
            tool = [m for m in msgs if m["role"] == "tool"][0]
            self.assertIn("спросить некого", tool["content"])  # без хозяина рядом — отказ, а не вопрос
            self.assertEqual(env.approvals, [])
            self.assertTrue(env.app.heart.jobs.get(job.id).enabled)  # cron — живёт дальше
            await env.app.mind.idle()
            self.assertEqual(env.app.store.q("SELECT COUNT(*) AS n FROM calls WHERE role = 'fast'")[0]["n"], 0)  # разбор после cron не идёт

    @async_test
    async def test_isolated_job_reports_to_main_and_authority_allows(self):
        async with MindEnv(self.tmp) as env:
            outside = self.tmp / "вне"
            outside.mkdir()
            env.script([call("write_file", path=str(outside / "отчёт.txt"), content="готово"), {"text": "Записал отчёт."}])
            job = env.app.heart.add("отчёт", kind="agent", prompt="Запиши отчёт", every="1d", session="isolated",
                                    authority=[{"capability": "fs.write", "target": str(outside) + "/*"}])
            res = await env.app.heart.fire(env.app.heart.jobs.get(job.id), manual=True)
            self.assertEqual(res["status"], "ok")
            self.assertEqual((outside / "отчёт.txt").read_text(encoding="utf-8"), "готово")  # полномочие сработало
            self.assertEqual(env.app.store.session(res["session"])["kind"], "cron")
            self.assertEqual([m["content"] for m in self.ev(env, "job")], ["📋 «отчёт»: Записал отчёт."])
            self.assertEqual(env.app.guard._turn_grants, {})  # полномочие только на время хода
            # без полномочия то же самое — отказ
            env.script([call("write_file", path=str(outside / "второй.txt"), content="x"), {"text": "Не вышло."}])
            j2 = env.app.heart.add("без прав", kind="agent", prompt="Запиши", every="1d", session="isolated")
            await env.app.heart.fire(env.app.heart.jobs.get(j2.id), manual=True)
            self.assertFalse((outside / "второй.txt").exists())

    @async_test
    async def test_missed_and_late(self):
        async with MindEnv(self.tmp) as env:
            h = env.app.heart
            j = h.add("старое", kind="remind", text="Давно было", at="через 20 минут")
            h.jobs.postpone(j, time.time() - 8 * 86400)
            self.assertEqual((await h.fire(h.jobs.get(j.id)))["status"], "missed")
            self.assertEqual(self.ev(env, "remind"), [])
            j = h.add("опоздал", kind="remind", text="Позвонить", at="через 20 минут")
            h.jobs.postpone(j, time.time() - 2 * 3600)
            self.assertEqual((await h.fire(h.jobs.get(j.id)))["status"], "ok")
            self.assertEqual(self.ev(env, "remind")[0]["content"], "⏰ Позвонить (с опозданием на 2 ч)")

    @async_test
    async def test_file_reflex_writes_nothing_while_calm(self):
        async with MindEnv(self.tmp) as env:
            h = env.app.heart
            f = self.tmp / "следим.txt"
            f.write_text("раз", encoding="utf-8")
            j = h.add("файл", kind="reflex", every="1h", check={"type": "file", "path": str(f)})
            await h.fire(h.jobs.get(j.id))  # первый замер: запомнить хеш
            self.assertIn("hash", h.jobs.get(j.id).meta["reflex"])
            db = env.app.store.db
            before = db.total_changes
            for _ in range(3):
                h._quiet.pop(j.id, None)
                self.assertEqual((await h.fire(h.jobs.get(j.id)))["status"], "quiet")
            self.assertEqual(db.total_changes, before)  # спокойно — ни одной записи
            self.assertIsNotNone(h._quiet.get(j.id))
            self.assertEqual(h.jobs.history(j.id), [])
            f.write_text("два", encoding="utf-8")
            self.assertEqual((await h.fire(h.jobs.get(j.id)))["status"], "ok")
            self.assertIn("изменился", self.ev(env, "reflex")[0]["content"])
            self.assertEqual(len(h.jobs.history(j.id)), 1)

    @async_test
    async def test_runner_sleeps_until_due_and_wakes_on_add(self):
        async with MindEnv(self.tmp) as env:
            h = env.app.heart
            h.start()
            await asyncio.sleep(0.05)
            h.add("скоро", kind="remind", text="Пора", at=time.time() + 1.2)  # poke будит бегуна: спать 300 с нельзя
            for _ in range(60):
                if self.ev(env, "remind"):
                    break
                await asyncio.sleep(0.05)
            self.assertEqual([m["content"] for m in self.ev(env, "remind")], ["⏰ Пора"])
            env.app.guard.halt()
            h.add("после стопа", kind="remind", text="Не сейчас", at=time.time() + 0.3)
            await asyncio.sleep(0.8)
            self.assertEqual(len(self.ev(env, "remind")), 1)  # стоп-кран: задачи ждут
            await h.stop()

    @async_test
    async def test_tools(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            env.auto_approve()
            ctx = ToolContext(app=app, session_id=env.sid, actor="agent")
            r = await app.tools.run("remind", {"text": "позвонить маме", "when": "завтра в 10:00"}, ctx)
            self.assertTrue(r.ok, r.content)
            self.assertIn("завтра в 10:00", r.content)
            r = await app.tools.run("remind", {"text": "x", "when": "когда-нибудь"}, ctx)
            self.assertFalse(r.ok)
            self.assertIn("Примеры", r.content)
            r = await app.tools.run("schedule", {"action": "add", "name": "диск", "every": "1h", "check": {"type": "disk", "path": "/", "min_free_gb": 0.001}}, ctx)
            self.assertTrue(r.ok, r.content)
            r = await app.tools.run("schedule", {"action": "add", "name": "чистка", "prompt": "почисти загрузки", "cron": "0 4 * * *",
                                                 "authority": [{"capability": "fs.delete", "target": "~/Downloads/*"}]}, ctx)
            self.assertTrue(r.ok, r.content)
            self.assertEqual(env.approvals[-1]["capability"], "schedule")  # полномочия — только с согласия хозяина
            r = await app.tools.run("schedule", {"action": "list"}, ctx)
            self.assertIn("«позвонить маме»", r.content)
            self.assertIn("в 04:00 каждый день", r.content)
            r = await app.tools.run("schedule", {"action": "pause", "job": "диск"}, ctx)
            self.assertIn("на паузе", r.content)
            r = await app.tools.run("schedule", {"action": "remove", "job": "чистка"}, ctx)
            self.assertTrue(r.ok)
            self.assertEqual(len(app.heart.jobs.all()), 2)


class AdoptTest(TempDirCase):
    def test_openclaw_jobs_and_intents_become_heart(self):
        import asyncio

        from bender.core.app import App
        from bender.heart.adopt import adopt

        app = App(self.tmp / "d")
        try:
            future_ms = (time.time() + 86400) * 1000
            app.store.meta_set("import:openclaw:cron", [
                {"id": "a", "name": "Сводка", "enabled": True, "job": {"schedule": {"kind": "cron", "expr": "0 9 * * 1-5"}, "sessionTarget": "isolated",
                                                                         "payload": {"kind": "agentTurn", "message": "Сводка"}, "policy": {"activeHours": {"start": "08:00", "end": "22:00"}}}},
                {"id": "b", "name": "Полив", "enabled": True, "job": {"schedule": {"kind": "every", "everyMs": 7200000}, "sessionTarget": "main",
                                                                        "payload": {"kind": "systemEvent", "text": "Полей цветы"}}},
                {"id": "c", "name": "Странная", "job": {"schedule": {"kind": "lunar"}, "payload": {"kind": "agentTurn", "message": "?"}}},
            ])
            app.store.meta_set("import:openclaw:intents", [
                {"id": "i1", "description": "Спроси про changelog", "trigger_keywords": json.dumps(["релиз"]), "status": "armed", "expires_at": future_ms},
                {"id": "i2", "description": "Старое", "trigger_keywords": "отпуск", "status": "armed", "expires_at": 1000},
            ])
            out = adopt(app)
            self.assertEqual((out["jobs"], out["intents"]), (2, 1))
            self.assertEqual(len(out["skipped"]), 2)
            jobs = {j.name: j for j in app.heart.jobs.all()}
            self.assertFalse(jobs["Сводка"].enabled)
            self.assertEqual(jobs["Сводка"].window, "08:00-22:00")
            self.assertEqual(jobs["Полив"].action, {"type": "remind", "text": "Полей цветы"})
            self.assertEqual(jobs["Полив"].schedule["every"], 7200)
            it = app.heart.intents.all()[0]
            self.assertEqual((it["text"], it["keywords"], it["source"]), ("Спроси про changelog", ["релиз"], "openclaw"))
            self.assertLess(it["expires"], time.time() + 2 * 86400)
            self.assertEqual(adopt(app), {"jobs": 0, "intents": 0, "skipped": []})  # повтор — ничего
            j = app.heart.set_enabled(jobs["Сводка"].id, True)
            self.assertIsNotNone(j.next_run)  # хозяин включил — пошло по расписанию
        finally:
            asyncio.run(app.close())


if __name__ == "__main__":
    unittest.main()
