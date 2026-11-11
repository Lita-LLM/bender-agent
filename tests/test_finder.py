"""Поиск по пульту: индекс из схемы настроек и кода экранов."""
from pathlib import Path

from bender.core.app import App
from bender.web import finder
from bender.web.api import STATIC
from tests.helpers import TempDirCase, async_test


class FinderTest(TempDirCase):
    @async_test
    async def test_index_points_every_setting_and_place_to_a_real_screen(self):
        app = App(self.tmp / "data")
        try:
            items = finder.index(app.settings.describe(), STATIC)
        finally:
            await app.close()
        screens = finder.screens(STATIC)
        routes = {"#/" + route for route in screens}
        self.assertTrue(all(item["route"] in routes for item in items), [i for i in items if i["route"] not in routes][:3])
        by = {(i["kind"], i["title"]): i for i in items}
        # Настройка Предохранителя ведёт на экран «Права», где она нарисована, а не в «Настройки».
        sandbox = by[("setting", "Песочница команд")]
        self.assertEqual(sandbox["route"], "#/guard")
        self.assertEqual(sandbox["key"], "guard.sandbox")
        self.assertIn("без песочницы", sandbox["words"])
        self.assertEqual(by[("setting", "Предлагать уборку ядра")]["route"], "#/settings")
        # Подписи с экранов: заголовок карточки и кнопка находятся без ручного списка.
        self.assertEqual(by[("heading", "Сейф")]["route"], "#/settings")
        self.assertEqual(by[("action", "Сделать снимок")]["route"], "#/backups")
        self.assertEqual(by[("screen", "Сны")]["route"], "#/dreams")
        # Ни дублей настройки полем того же экрана, ни общих кнопок вроде «Сохранить».
        self.assertNotIn(("field", "Песочница команд"), by)
        self.assertFalse(any(i["kind"] == "action" and i["title"] == "Сохранить" for i in items))
        for name, host in finder.HOSTS.items():
            self.assertTrue((Path(STATIC) / "screens" / f"{name}.js").exists(), name)
            self.assertIn(host, set(screens) | {"chat"}, name)
