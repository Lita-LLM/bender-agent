"""Окна Windows (превью): политика окон и тулы на подменённом исполнителе (сам исполнитель проверяется приёмкой на Windows)."""
import base64
from unittest import mock

from bender.tools import screen
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv

NOTEPAD = {'hwnd': 11, 'title': 'Заметки — Блокнот', 'class': 'Notepad', 'pid': 1, 'exe': r'C:\Windows\notepad.exe', 'left': 0, 'top': 0, 'width': 400, 'height': 300}
TERMINAL = {'hwnd': 12, 'title': 'Терминал', 'class': 'X', 'pid': 2, 'exe': r'C:\Windows\System32\cmd.exe', 'left': 0, 'top': 0, 'width': 400, 'height': 300}
PULT = {'hwnd': 13, 'title': 'Бендер — Mozilla Firefox', 'class': 'Y', 'pid': 3, 'exe': r'C:\Program Files\Firefox\firefox.exe', 'left': 0, 'top': 0, 'width': 400, 'height': 300}


class FakeExecutor:
    def __init__(self):
        self.calls = []

    async def call(self, **command):
        self.calls.append(command)
        if command['op'] == 'list':
            return {'windows': [NOTEPAD, TERMINAL, PULT]}
        if command['op'] == 'shot':
            return {'png': base64.b64encode(b'png').decode(), 'width': 400, 'height': 300}
        return {}


class ScreenTest(TempDirCase):
    def test_policy_hard_list_beats_the_allow_list(self):
        allowed = ['notepad.exe', 'cmd.exe', 'firefox.exe']
        self.assertEqual(screen.verdict(NOTEPAD, allowed), (True, ''))
        self.assertFalse(screen.verdict(TERMINAL, allowed)[0])  # терминал разрешён списком, но закрыт всегда
        self.assertFalse(screen.verdict(PULT, allowed)[0])  # пульт узнаётся по заголовку
        self.assertFalse(screen.verdict(NOTEPAD, [])[0])
        self.assertFalse(screen.verdict({**NOTEPAD, 'exe': ''}, allowed)[0])
        self.assertFalse(screen.verdict({**NOTEPAD, 'title': 'Выполнить'}, allowed)[0])

    @async_test
    async def test_off_by_default_and_not_windows(self):
        async with MindEnv(self.tmp) as env:
            ctx = ToolContext(env.app, session_id=env.sid)
            result = await env.app.tools.run('screen_windows', {}, ctx)
            self.assertFalse(result.ok);self.assertIn('выключена', result.content)
            env.app.settings.set('screen', {'enabled': True, 'programs': ['notepad.exe']}, by='owner')
            with mock.patch.object(screen.sys,'platform','linux'):
                result = await env.app.tools.run('screen_windows', {}, ctx)
            self.assertIn('только на Windows', result.content)

    @async_test
    async def test_tools_see_only_allowed_windows_and_ask_before_acting(self):
        async with MindEnv(self.tmp) as env:
            env.auto_approve('once')
            app = env.app
            app.settings.set('screen', {'enabled': True, 'programs': ['notepad.exe', 'cmd.exe']}, by='owner')
            fake = FakeExecutor()
            ctx = ToolContext(app, session_id=env.sid)
            with mock.patch.object(screen.sys, 'platform', 'win32'), mock.patch.object(screen, 'executor_of', return_value=fake):
                listed = await app.tools.run('screen_windows', {}, ctx)
                self.assertIn('notepad.exe', listed.content);self.assertNotIn('cmd.exe', listed.content);self.assertNotIn('Firefox', listed.content)
                seen = await app.tools.run('screen_look', {'window': 11}, ctx)
                self.assertTrue(seen.ok, seen.content)
                self.assertEqual(seen.model_parts[1]['media_type'], 'image/png')
                refused = await app.tools.run('screen_look', {'window': 12}, ctx)
                self.assertFalse(refused.ok);self.assertIn('cmd.exe', refused.content)
                clicked = await app.tools.run('screen_click', {'window': 11, 'x': 5, 'y': 6}, ctx)
                self.assertTrue(clicked.ok, clicked.content)
                typed = await app.tools.run('screen_type', {'window': 11, 'text': 'привет'}, ctx)
                self.assertTrue(typed.ok)
                self.assertEqual([c['op'] for c in fake.calls if c['op'] != 'list'], ['shot', 'click', 'type'])
                self.assertTrue(any(card['capability'] == 'screen.act' for card in env.approvals), 'каждое действие — через карточку')
                bad = await app.tools.run('screen_click', {'window': 13, 'x': 1, 'y': 1}, ctx)
                self.assertFalse(bad.ok)
