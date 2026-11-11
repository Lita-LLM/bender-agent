"""Псевдонимы файлов не обходят границы ни на одном типе тома."""
from pathlib import Path
import os
from unittest.mock import patch

from bender.core.errors import HumanError
from bender.core.pathutil import canonical, case_sensitive, relative_path, within, same
from bender.guard.policy import Policy, Action, HOME
from bender.guard.control import sealed, CONTROL_PATHS, protected_change
from tests.helpers import TempDirCase


class PathsTest(TempDirCase):
    def test_real_volume_probe_cached_and_no_prefix_confusion(self):
        self.assertIsInstance(case_sensitive(self.tmp), bool)
        with patch('bender.core.pathutil.tempfile.mkstemp', side_effect=AssertionError('повторная запись')):
            case_sensitive(self.tmp)
        self.assertTrue(within(self.tmp / 'work' / 'new', self.tmp / 'work'))
        self.assertFalse(within(self.tmp / 'work-other', self.tmp / 'work'))
        self.assertTrue(same(self.tmp / 'a' / '..' / 'b', self.tmp / 'b'))

    def test_insensitive_volume_protects_control_and_data(self):
        data = self.tmp / 'data'
        policy = Policy(work_dir=data / 'work', data_dir=data, get_settings=lambda: {'profile': 'trust'}, get_rules=lambda: [])
        with patch('bender.core.pathutil.case_sensitive', return_value=False):
            for root in CONTROL_PATHS:
                path = root.swapcase() + ('' if root.endswith('.py') else '/probe.py')
                with self.subTest(path=path):
                    self.assertTrue(protected_change(path))
            for name in ('BENDER.DB', 'WoRkShOp/x', 'EXAMS/x', 'LOCAL/x'):
                self.assertEqual(policy.decide(Action('fs.write', str(data / name))).rule, 'sealed')
            self.assertEqual(policy.decide(Action('fs.write', str(HOME / '.SSH/authorized_keys'))).verdict, 'deny')
            self.assertEqual(policy.decide(Action('fs.read', str(data / 'SECRETS.JSON'))).verdict, 'deny')
            with patch('bender.core.pathutil.unicode_equivalent', return_value=True):
                self.assertTrue(within(data / 'CAFÉ/a', data / 'cafe\u0301'))

    def test_windows_alias_table_rejected_even_in_linux_packages(self):
        for name in ('bender/guard./x.py', 'bender/guard /x.py', 'a::$DATA', 'a:stream', r'\\?\C:\bender\guard',
                     r'\\.\NUL', 'CON', 'con.txt', 'NUL/file', 'LPT1.log', 'COM¹', 'a/../b', r'a\b', 'C:/a'):
            with self.subTest(name=name), self.assertRaises(HumanError):
                relative_path(name)
        relative_path('references/обычный.md')

    def test_links_existing_parent_and_hardlink(self):
        folder = self.tmp / 'real'
        folder.mkdir()
        link = self.tmp / 'link'
        try:
            link.symlink_to(folder, target_is_directory=True)
        except OSError:
            self.skipTest('ОС не разрешает создание ссылок')
        self.assertTrue(within(link / 'not-yet' / 'x', folder))
        secret = folder / 'secret'
        secret.write_text('тест')
        alias = self.tmp / 'alias'
        os.link(secret, alias)
        self.assertTrue(same(secret, alias))
