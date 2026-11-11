"""Дополняющие миграции сохраняют прежние INSERT."""
from bender.core.errors import HumanError
from tests.helpers import TempDirCase


class StoreCompatTest(TempDirCase):
    def test_new_columns_keep_old_inserts_working(self):
        from bender.core.store import Store

        store = Store(self.tmp / "compat.db")
        try:
            store.migrate("compat-test", 1, ["CREATE TABLE IF NOT EXISTS legacy (id TEXT PRIMARY KEY)"])
            with self.assertRaises(HumanError):
                store.migrate("compat-test", 2, ["ALTER TABLE legacy ADD COLUMN required TEXT NOT NULL"])
            with self.assertRaises(HumanError):
                store.migrate("compat-test", 2, ["ALTER TABLE legacy ADD COLUMN required TEXT NOT NULL DEFAULT (NULL)"])
            store.migrate("compat-test", 2, ["ALTER TABLE legacy ADD COLUMN note TEXT NOT NULL DEFAULT 'прежний'"])
            store.x("INSERT INTO legacy(id) VALUES(?)", ("старый интерфейс",))
            self.assertEqual(store.one("SELECT note FROM legacy")["note"], "прежний")
        finally:
            store.close()

