"""Протокол настоящего agy-facade: поток, тулы, ошибки, таймаут и уборка."""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from bender.router.agy import run_agy, list_models, stream_facade
from bender.router.errors import ProviderError
from bender.router.types import ToolSpec
from bender.router.effort import describe

FAKE = '''import json, sys, time
if '--help' in sys.argv:
 print('--input-format --output-format --mode --model --effort --disable-slash-commands')
elif 'models' in sys.argv:
 print('gemini-test\\tТестовая модель')
else:
 request = json.loads(sys.stdin.readline())
 assert request['event'] == 'user'
 prompt = request['message']['content']
 if 'WAIT_FOREVER' in prompt:
  time.sleep(60)
 elif 'FAIL_TURN' in prompt:
  print(json.dumps({'event':'result','result':{'status':'ERROR','error':'quota exceeded'}}),flush=True)
 else:
  text = '<tool_call>{"name":"read_file","arguments":{"path":"note.txt"}}</tool_call>' if 'TOOLS_TEST' in prompt else 'Ответ agy.'
  for part in (text[:8],text[8:]):
   print(json.dumps({'event':'step_update','step_update':{'step_type':'agent_response','text_delta':part}}),flush=True)
  print(json.dumps({'event':'result','result':{'status':'SUCCESS','usage':{'input_tokens':7413,'output_tokens':7}}}),flush=True)
 time.sleep(60)
'''


class AgyEngineTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        script = self.root / 'fake_agy.py'
        script.write_text(FAKE, encoding='utf-8')
        self.p = {'id':'agy', 'name':'Antigravity', 'kind':'antigravity',
                  'risk_accepted':True, 'command':[sys.executable,str(script)], 'timeout':3}
        self.cand = SimpleNamespace(provider=self.p,model='gemini-test')
        self.router = SimpleNamespace()
        self.home = patch('bender.router.agy.Path.home', return_value=self.root)
        self.home.start()
        self.addCleanup(self.home.stop)
        self.events = []

    async def call(self, prompt='Привет', tools=None, params=None):
        return await run_agy(self.router,self.cand,[{'role':'user','content':prompt}],tools,params or {},
                             lambda kind,text:self.events.append((kind,text)))

    async def test_real_protocol_stream_and_usage(self):
        res = await self.call(params={'effort':'high'})
        self.assertEqual(res.text,'Ответ agy.')
        self.assertEqual(''.join(t for k,t in self.events),'Ответ agy.')
        self.assertEqual((res.usage.input,res.usage.output),(7413,7))

    async def test_bender_tools_are_returned_not_executed(self):
        res = await self.call('TOOLS_TEST',tools=[ToolSpec('read_file','Читать файл',{'type':'object'})])
        self.assertEqual(res.finish,'tool_calls')
        self.assertEqual(res.tool_calls[0].name,'read_file')
        self.assertEqual(res.tool_calls[0].arguments,{'path':'note.txt'})
        self.assertNotIn('<tool_call>', ''.join(t for k,t in self.events))

    async def test_risk_and_failed_turn(self):
        self.p['risk_accepted'] = False
        with self.assertRaises(ProviderError):
            await self.call()
        self.p['risk_accepted'] = True
        with self.assertRaises(ProviderError) as caught:
            await self.call('FAIL_TURN')
        self.assertEqual(caught.exception.klass,'rate_limit')

    async def test_timeout_and_cancel(self):
        self.p['timeout'] = 0.3
        with self.assertRaises(ProviderError) as caught:
            await self.call('WAIT_FOREVER')
        self.assertEqual(caught.exception.klass,'timeout')
        self.p['timeout'] = 30
        task = asyncio.create_task(self.call('WAIT_FOREVER'))
        await asyncio.sleep(0.2)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task

    async def test_catalog_from_cli(self):
        rows = await list_models(self.p)
        self.assertEqual(rows[0]['id'],'gemini-test')

    async def test_requested_effort_is_passed_to_cli(self):
        self.assertEqual(describe(self.p,'gemini-test',{'effort':'xhigh'})['effective'],'xhigh')
        from bender.router import agy
        original = agy.stream_facade
        seen = []
        async def capture(args, **kwargs):
            seen.extend(args)
            return await original(args, **kwargs)
        with patch.object(agy,'stream_facade',capture):
            await self.call(params={'effort':'xhigh'})
        self.assertEqual(seen[seen.index('--effort')+1],'xhigh')

    async def test_settings_require_risk_acceptance(self):
        from bender.core.errors import HumanError
        from tests.test_mind import MindEnv
        async with MindEnv(self.root / 'app') as env:
            with self.assertRaises(HumanError) as caught:
                env.app.models.add_provider(preset='antigravity')
            self.assertEqual(caught.exception.code,'agy_risk')

    async def test_stream_cleans_process_after_success(self):
        events = []
        code, _ = await stream_facade(self.p['command'],prompt='test',cwd=self.root,
            env=os.environ.copy(),timeout=3,on_event=events.append)
        self.assertEqual(code,0)
        self.assertEqual(events[-1]['event'],'result')
