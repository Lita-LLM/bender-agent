"""Автозапуск только на временном HOME: PATH, ручная копия, голос и Termux."""
import os
from pathlib import Path
from unittest.mock import patch

from bender.core.app import App
from bender.core.run_lock import RunLock
from bender.core import service
from tests.helpers import TempDirCase,async_test


class ServiceTest(TempDirCase):
    @async_test
    async def test_manual_copy_enables_without_second_start_and_preserves_path(self):
        app=App(self.tmp/'data');home=self.tmp/'home';home.mkdir();calls=[]
        try:
            with patch('pathlib.Path.home',return_value=home),patch.object(service,'available',return_value=True),patch.object(service,'command',side_effect=lambda argv,**kw:calls.append(argv) or ''):
                with RunLock(app.dirs.root),patch.dict(os.environ,{'PATH':'/пример с пробелом/%bin:/usr/bin'}):
                    result=await service.change(app,'install')
                    self.assertIn('PID '+str(os.getpid()),result['message'])
                    self.assertIn('Ctrl+C',result['message'])
                unit=service.unit_path().read_text()
                self.assertIn('Restart=on-failure',unit)
                self.assertIn('-m bender --no-anim run --no-browser',unit)
                self.assertIn('PATH=/пример с пробелом/%%bin:/usr/bin',unit)
                self.assertIn('BENDER_HOME='+str(app.dirs.root),unit)
                enable=next(argv for argv in calls if 'enable' in argv);self.assertNotIn('--now',enable)
                await service.change(app,'remove')
                self.assertFalse(service.unit_path().exists())
                self.assertTrue(any('disable' in argv and '--now' in argv for argv in calls))
        finally:await app.close()

    @async_test
    async def test_core_start_without_manual_copy_and_existing_voice_alias(self):
        app=App(self.tmp/'data');home=self.tmp/'home';home.mkdir();calls=[]
        try:
            with patch('pathlib.Path.home',return_value=home),patch.object(service,'available',return_value=True),patch.object(service,'command',side_effect=lambda argv,**kw:calls.append(argv) or ''):
                await service.change(app,'install')
                self.assertTrue(any('enable' in argv and '--now' in argv for argv in calls))
                folder=app.dirs.local/'voice';(folder/'venv/bin').mkdir(parents=True)
                (folder/'venv/bin/python').write_text('тест');(folder/'installed.json').write_text('{}')
                old=folder/'bender-voice.service';old.write_text('прежний unit')
                service.unit_path(True).symlink_to(old)
                await service.change(app,'install',voice=True)
                self.assertFalse(service.unit_path(True).is_symlink())
                self.assertIn('local_server.py',service.unit_path(True).read_text())
                self.assertIn('RestartSec=60',service.unit_path(True).read_text())
                self.assertEqual(old.read_text(),'прежний unit')
        finally:await app.close()

    @async_test
    async def test_termux_boot_and_owner_rule(self):
        app=App(self.tmp/'data');home=self.tmp/'home';home.mkdir()
        try:
            with patch('pathlib.Path.home',return_value=home),patch.dict(os.environ,{'TERMUX_VERSION':'тест'}),patch.object(service,'command') as command:
                result=await service.change(app,'install')
                self.assertIn('Termux:Boot',result['message'])
                text=service.unit_path().read_text();self.assertIn('termux-wake-lock',text)
                self.assertIn('BENDER_SERVICE_KIND=termux',text)
                self.assertEqual(service.unit_path().stat().st_mode&0o777,0o700)
                command.assert_not_called()
                await service.change(app,'remove');self.assertFalse(service.unit_path().exists())
                app.guard.add_rule('system.service','*','deny')
                from bender.tools.registry import Denied
                with self.assertRaises(Denied):await service.change(app,'install')
        finally:await app.close()

    def test_unit_passes_systemd_verify_with_unusual_program_path(self):
        import shutil, subprocess
        program=self.tmp/'прог с пробелом %x';(program/'bender').mkdir(parents=True);(program/'bender/__main__.py').write_text('')
        with patch.dict(os.environ,{'BENDER_PROGRAM_DIR':str(program)}):
            unit=service.core_unit(self.tmp/'data')
        line=next(l for l in unit.splitlines() if l.startswith('WorkingDirectory='))
        # systemd не снимает кавычки у WorkingDirectory: «"/…"» — «path is not absolute», служба не грузится.
        self.assertEqual(line,'WorkingDirectory='+str(program.resolve()).replace('%','%%'))
        tool=shutil.which('systemd-analyze')
        if not tool:self.skipTest('systemd-analyze нет')
        path=self.tmp/'bender.service';path.write_text(unit)
        result=subprocess.run([tool,'verify','--man=no',str(path)],capture_output=True,text=True,timeout=60)
        self.assertNotIn('not absolute',result.stdout+result.stderr)
        self.assertNotIn('fatal error',result.stdout+result.stderr)
