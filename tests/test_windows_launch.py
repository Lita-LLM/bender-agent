"""Повторный запуск в Windows: проверка прежней копии получает код целиком, а не по словам."""
import os
from unittest.mock import patch

from bender import __main__ as entry
from bender.core import watchdog
from tests.helpers import TempDirCase


class WindowsLaunchTest(TempDirCase):
    def test_second_start_with_run_lock_passes_code_as_one_argument(self):
        data = self.tmp / 'данные с пробелом'; (data / 'local').mkdir(parents=True)
        (data / 'local' / 'run.lock').write_text('')
        with patch('os.name', 'nt'), patch('subprocess.call', return_value=7) as call, \
                patch('os.spawnv', side_effect=AssertionError('spawnv в Windows не берёт аргументы в кавычки')):
            self.assertEqual(entry._watch(watchdog, str(data), False, False), 7)
        args = call.call_args.args[0]
        self.assertEqual(args[1], '-c')
        self.assertTrue(args[2].startswith('import sys; '))
        self.assertEqual(args[3:], [watchdog.PROGRAM_DIR, str(data)])

    def test_preflight_command_really_runs(self):
        data = self.tmp / 'данные'; (data / 'local').mkdir(parents=True)
        (data / 'local' / 'run.lock').write_text('')
        with patch.object(watchdog, 'main', return_value=0) as main:
            self.assertEqual(entry._watch(watchdog, str(data), False, False), 0)
        main.assert_called_once()


if __name__ == '__main__':
    import unittest; unittest.main()
