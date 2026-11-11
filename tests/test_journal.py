"""Бортжурнал: цепочка хешей, якорь хвоста; уборка старых снимков."""

import asyncio
import time

from bender.core.app import App
from bender.core.store import Store
from bender.guard.journal import Journal
from tests.helpers import TempDirCase


class JournalTest(TempDirCase):
    def make(self):
        store = Store(self.tmp / "b.db")
        self.addCleanup(store.close)
        return store, Journal(store, self.tmp / "local" / "journal.key")

    def test_chain_and_tail_anchor(self):
        store, j = self.make()
        for i in range(5):
            j.write("test", f"запись {i}")
        self.assertEqual(j.verify(), {"ok": True, "checked": 5, "broken_at": None})
        store.x("DELETE FROM journal WHERE seq > 3")  # обрезали хвост — цепочка сама цела
        v = j.verify()
        self.assertFalse(v["ok"])
        self.assertTrue(v["truncated"])
        self.assertEqual((v["anchor"], v["last"]), (5, 3))

    def test_edit_in_the_middle(self):
        store, j = self.make()
        for i in range(4):
            j.write("test", f"запись {i}")
        store.x("UPDATE journal SET summary = 'подделка' WHERE seq = 2")
        self.assertEqual(j.verify()["broken_at"], 2)

    def test_new_journal_after_restart_keeps_anchor(self):
        store, j = self.make()
        j.write("test", "раз")
        j2 = Journal(store, self.tmp / "local" / "journal.key")
        j2.write("test", "два")
        self.assertTrue(j2.verify()["ok"])


class HousekeepingTest(TempDirCase):
    def test_prunes_old_snapshots_and_writes_nothing_when_clean(self):
        app = App(self.tmp / "data")
        try:
            old = app.store.add_call(session_id="s", role="main", provider="p", model="m", usage={}, cost=0, seconds=0, notice=None, request={"messages": ["x" * 1000]})
            new = app.store.add_call(session_id="s", role="main", provider="p", model="m", usage={}, cost=0, seconds=0, notice=None, request={"messages": ["y"]})
            app.store.x("UPDATE calls SET created_at = ? WHERE id = ?", (time.time() - 30 * 86400, old))
            self.assertEqual(app.housekeeping()["call_snapshots"], 1)
            self.assertIsNone(app.store.call(old)["request"])
            self.assertIsNotNone(app.store.call(new)["request"])
            before = app.store.db.total_changes
            self.assertEqual(app.housekeeping(), {"call_snapshots": 0, "checkpoint_days": 0, "migration_copies": 0})
            self.assertEqual(app.store.db.total_changes, before)  # чисто — ни одной записи
        finally:
            asyncio.run(app.close())
