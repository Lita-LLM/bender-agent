"""Самообновление агента закрыто и в git-клоне, и в распакованной программе."""
import shlex
from unittest.mock import patch

from bender.guard.control import updates_command
from bender.guard.policy import Action
from tests.helpers import TempDirCase


class ControlUpdatesTest(TempDirCase):
    def test_native_and_argv_paths_in_zip_without_git(self):
        for name in ('program','program with spaces'):
            root=self.tmp/name;root.mkdir()
            elsewhere=self.tmp/(name+'-other');elsewhere.mkdir()
            with patch('bender.guard.control.source_roots',return_value={root}):
                for command in (f'git -C "{root}" pull',shlex.join(['git','-C',str(root),'pull'])):
                    self.assertTrue(updates_command(Action('exec',command)),command)
                if ' ' not in str(root):self.assertTrue(updates_command(Action('exec',f'git -C {root} pull')))
                self.assertFalse(updates_command(Action('exec',shlex.join(['git','-C',str(elsewhere),'pull']))))
                self.assertFalse(updates_command(Action('exec',f'git -C "{root}" status')))
                self.assertFalse(updates_command(Action('exec',f'git -C "{root}" pull',actor='owner')))
