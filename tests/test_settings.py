import os
import unittest

from bender.core.secrets import Secrets
from bender.core.settings import NeedsOwner, Settings
from bender.core.store import Store
from bender.core.errors import HumanError
from bender.router.config import ModelsConfig
from tests.helpers import TempDirCase


class SettingsTest(TempDirCase):
    def test_defaults_set_and_unknown_keys(self):
        st = Store(self.tmp / "b.db")
        s = Settings(st)
        self.assertEqual(s.get("guard", "profile"), "careful")
        diff = s.set("heart", {"interval_minutes": "15", "enabled": "нет"})
        self.assertEqual(diff["interval_minutes"], (30, 15))
        self.assertFalse(s.get("heart", "enabled"))
        # ключ из будущей версии сохраняется
        st.x("UPDATE settings SET value = ? WHERE grp = 'heart'", ('{"interval_minutes": 15, "future_key": 7}',))
        s2 = Settings(st)
        self.assertEqual(s2.group("heart")["future_key"], 7)
        s2.set("heart", {"interval_minutes": 20})
        self.assertEqual(Settings(st).group("heart")["future_key"], 7)
        with self.assertRaises(HumanError):
            s.set("heart", {"interval_minutes": 0})
        with self.assertRaises(HumanError):
            s.set("guard", {"profile": "anarchy"})
        st.close()

    def test_agent_cannot_weaken(self):
        st = Store(self.tmp / "b.db")
        s = Settings(st)
        with self.assertRaises(NeedsOwner) as cm:
            s.set("guard", {"profile": "off"}, by="agent")
        self.assertIn("Профиль", cm.exception.hint)
        # ужесточать агенту можно
        s.set("guard", {"profile": "careful"}, by="agent")
        # хозяину можно всё, включая выключить
        s.set("guard", {"profile": "off", "hardline": False}, by="owner")
        self.assertEqual(s.get("guard", "profile"), "off")
        s.set("guard", {"profile": "normal"}, by="agent")  # с «Выключен» на «Обычный» — это ужесточение
        with self.assertRaises(NeedsOwner):
            s.set("web", {"listen": True}, by="agent")
        st.close()

    def test_secrets_file_mode(self):
        sec = Secrets(self.tmp / "secrets.json")
        sec.set("x", "sk-1234567890abcdef")
        self.assertEqual(Secrets(self.tmp / "secrets.json").get("x"), "sk-1234567890abcdef")
        self.assertEqual(sec.masked("x"), "sk-1…cdef")
        if os.name == "posix":
            self.assertEqual(os.stat(self.tmp / "secrets.json").st_mode & 0o777, 0o600)

    def test_nonfinite_numbers_are_rejected(self):
        st=Store(self.tmp/"b.db");settings=Settings(st)
        try:
            for number in (float("nan"),float("inf"),float("-inf")):
                with self.assertRaises(HumanError):settings.set("body",{"daily_usd":number})
        finally:st.close()

    def test_models_config(self):
        st = Store(self.tmp / "b.db")
        s = Settings(st)
        sec = Secrets(self.tmp / "secrets.json")
        mc = ModelsConfig(s, sec)
        p = mc.add_provider(preset="openrouter", api_key="sk-or-abcdefgh12345")
        self.assertEqual(p["base_url"], "https://openrouter.ai/api/v1")
        self.assertEqual(sec.get(p["keys"][0]), "sk-or-abcdefgh12345")
        mc.set_role("main", [{"provider": p["id"], "model": "x/y:free"}])
        self.assertEqual(mc.roles()["main"][0]["model"], "x/y:free")
        pub = mc.public_providers()[0]
        self.assertNotIn("sk-or-abcdefgh12345", str(pub))
        mc.remove_provider(p["id"])
        self.assertEqual(mc.roles()["main"], [])
        self.assertIsNone(sec.get(p["keys"][0]))
        st.close()


if __name__ == "__main__":
    unittest.main()
