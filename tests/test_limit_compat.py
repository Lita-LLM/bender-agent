"""Настоящая схема прежнего слота читает новую базу и сохраняет свой выбор."""
import importlib.util
import json
from pathlib import Path
import sys

from bender.core.settings import Settings
from bender.core.store import Store
from bender.diagnostics.doctor import Reader
from tests.helpers import TempDirCase


class LimitCompatTest(TempDirCase):
    def test_frozen_previous_settings_reads_zero_large_values_and_owner_edit_wins(self):
        path=Path(__file__).parent/'fixtures'/'legacy'/'settings_before_unlimited.py'
        spec=importlib.util.spec_from_file_location('bender.core._legacy_limits',path)
        module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module
        try:
            spec.loader.exec_module(module)
            store=Store(self.tmp/'b.db')
            try:
                store.x('INSERT INTO settings VALUES(?,?,0)',('agent','{"max_steps":25,"max_turn_seconds":900,"future":{"keep":7}}'))
                current=Settings(store,migrate=True)
                self.assertEqual(current.get('agent','max_steps'),0)
                self.assertEqual(Settings(store).get('agent','max_steps'),0)
                previous=module.Settings(store)
                self.assertEqual(previous.get('agent','max_steps'),25)
                self.assertEqual(previous.get('agent','max_turn_seconds'),900)
                current.set('team',{'max_steps':10000,'max_seconds':0})
                self.assertEqual(Settings(store).get('team','max_steps'),10000)
                self.assertEqual(Settings(store).get('team','max_seconds'),0)
                self.assertEqual(module.Settings(store).get('team','max_steps'),100)
                self.assertEqual(module.Settings(store).get('team','max_seconds'),600)
                previous.set('agent',{'max_steps':17})
                reloaded=Settings(store,migrate=True)
                self.assertEqual(reloaded.get('agent','max_steps'),17)
                self.assertEqual(reloaded.get('agent','future'),{'keep':7})
                marker=store.meta_get('settings:limit_compat')
                reader=Reader(self.tmp/'b.db')
                try:self.assertEqual(Settings(reader).get('team','max_seconds'),0)
                finally:reader.close()
                self.assertEqual(store.meta_get('settings:limit_compat'),marker)
            finally:store.close()
        finally:sys.modules.pop(spec.name,None)

    def test_intermediate_zero_storage_is_repaired_without_changing_effective_value(self):
        store=Store(self.tmp/'intermediate.db')
        try:
            store.x('INSERT INTO settings VALUES(?,?,0)',('agent','{"max_steps":0,"max_turn_seconds":0}'))
            store.meta_set('settings:limits-v2',True)
            settings=Settings(store,migrate=True)
            self.assertEqual(settings.get('agent','max_steps'),0)
            raw=json.loads(store.one("SELECT value FROM settings WHERE grp='agent'")['value'])
            self.assertEqual(raw['max_steps'],25)
            self.assertEqual(raw['max_turn_seconds'],900)
            self.assertEqual(Settings(store).get('agent','max_steps'),0)
        finally:store.close()
