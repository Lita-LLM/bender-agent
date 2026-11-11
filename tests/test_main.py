"""Ядро не импортирует argparse для обычного запуска; флаги и справка совместимы."""
import contextlib
import io
from unittest.mock import patch
import unittest
from bender.__main__ import build_parser,main
from bender.core.watchdog import quick_args


class MainTest(unittest.TestCase):
    def test_core_run_does_not_build_all_command_parsers(self):
        variants=[[],['--data','run'],['--data=--safe','run'],['--data','папка','--no-anim','run','--port','8877','--listen','--safe','--no-browser'],['--data=папка','run','--port=8878'],['run','--port','1','--port=2'],['run','--no-browser','--no-anim'],['--no-anim','run','--no-anim']]
        for raw in variants:
            expected=vars(build_parser().parse_args(raw))
            self.assertEqual(quick_args(raw,full=True),expected)
            with self.subTest(raw=raw),patch.dict('os.environ',{'BENDER_CORE':'1'}),patch('bender.__main__.build_parser',side_effect=AssertionError('Лишнее дерево argparse')),patch('bender.core.app.main',return_value=0) as run:
                self.assertEqual(main(raw),0)
                self.assertEqual(vars(run.call_args.args[0]),expected)

    def test_help_and_bad_flags_fall_back_to_full_parser(self):
        for raw in (['--help'],['run','--port','²'],['--data','--safe','run'],['doctor','--json']):self.assertIsNone(quick_args(raw,full=True))
        with patch.dict('os.environ',{'BENDER_CORE':'1'}),contextlib.redirect_stdout(io.StringIO()) as output,self.assertRaises(SystemExit):main(['--help'])
        self.assertIn('doctor',output.getvalue());self.assertIn('report',output.getvalue())
