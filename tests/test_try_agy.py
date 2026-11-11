"""bender try agy: только факты о бинарнике, модель не вызывается."""
import os
import subprocess
import sys
from pathlib import Path

from tests.helpers import TempDirCase


class TryAgyTest(TempDirCase):
    def run_try(self, *args):
        root = Path(__file__).resolve().parents[1]
        return subprocess.run([sys.executable, '-m', 'bender', '--data', str(self.tmp / 'data'), '--no-anim', 'try', 'agy', *args], cwd=root,
                              env={**os.environ, 'PYTHONPATH': str(root), 'PATH': str(self.tmp), 'PYTHONUTF8':'1'}, capture_output=True, text=True, encoding='utf-8', timeout=60)

    def test_not_found_and_fake_binary(self):
        done = self.run_try()
        self.assertEqual(done.returncode, 1)
        self.assertIn('agy не найден', done.stdout)
        # Настоящий исполняемый файл доступен и на Windows; аргументы Python в оболочку не передаём.
        if os.name == 'nt':
            import shutil
            fake = self.tmp / 'agy.exe'
            shutil.copyfile(sys.executable, fake)
            done = self.run_try(str(fake))
            self.assertEqual(done.returncode, 1, done.stdout + done.stderr)
            return
        fake = self.tmp / 'agy'
        fake.write_text('#!' + sys.executable + '\nimport sys\nprint("agy 1.2.3" if "--version" in sys.argv else "usage: agy --print --input-format --output-format stream-json --mode --model --effort")\n')
        fake.chmod(0o755)
        done = self.run_try(str(fake))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('1.2.3', done.stdout)
        self.assertIn('поток событий есть'.split()[0], done.stdout)
        self.assertIn('принятия риска', done.stdout)
