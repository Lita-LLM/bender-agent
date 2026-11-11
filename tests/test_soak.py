"""Короткая форма того же долгого сценария; все органы настоящие, данные временные."""
from bender.devtools.soak import run, conclusion
from bender.core.errors import HumanError
from unittest.mock import AsyncMock,MagicMock,patch
from tests.helpers import TempDirCase,async_test


class SoakTest(TempDirCase):
    @async_test
    async def test_all_events_and_integrity(self):
        result=await run(seconds=.02,cycles=3)
        self.assertTrue(result['ok'],result)
        for key in ('owner','subagent','mail','hook','timer','skill','mcp','sleep','backup'):
            self.assertGreater(result['counts'][key],0,key)
        self.assertEqual(result['counts']['timer'],3)
        self.assertEqual(result['samples'][-1]['turns'],0)
        self.assertEqual(result['samples'][-1]['listeners'],result['samples'][0]['listeners'])

    def test_growth_is_not_silently_ignored(self):
        values={'rss_mb':30,'fds':20,'sockets':8,'threads':2,'asyncio':3,'listeners':2,'turns':0,'http_idle':2}
        self.assertFalse(conclusion([values,{**values,'fds':40}])['ok'])

    def test_report_path_cannot_overwrite_service_data(self):
        from bender.devtools.soak import main
        data=self.tmp/'data';data.mkdir();secret=data/'secrets.json';secret.write_text('не трогать')
        with patch('bender.devtools.soak.run',AsyncMock(return_value={'ok':True})),self.assertRaises(HumanError):
            main(['0','--report',str(secret)],MagicMock(),data=data)
        self.assertEqual(secret.read_text(),'не трогать')
        report=self.tmp/'soak.json'
        with patch('bender.devtools.soak.run',AsyncMock(return_value={'ok':True})):
            self.assertEqual(main(['0','--report',str(report)],MagicMock(),data=data),0)
        self.assertIn('true',report.read_text())
