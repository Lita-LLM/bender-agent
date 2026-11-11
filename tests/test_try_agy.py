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
                              env={**os.environ, 'PYTHONPATH': str(root), 'PATH': str(self.tmp)}, capture_output=True, text=True, timeout=60)

    def test_not_found_and_fake_binary(self):
        done = self.run_try()
        self.assertEqual(done.returncode, 1)
        self.assertIn('agy не найден', done.stdout)
        fake = self.tmp / 'agy'
        fake.write_text('#!' + sys.executable + '\nimport sys\nprint("agy 1.2.3" if "--version" in sys.argv else "usage: agy -p --output-format stream-json --resume mcp")\n')
        fake.chmod(0o755)
        done = self.run_try(str(fake))
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn('1.2.3', done.stdout)
        self.assertIn('поток событий есть'.split()[0], done.stdout)
        self.assertIn('не поддерживает обёртки', done.stdout)
