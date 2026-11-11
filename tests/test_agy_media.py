"""Картинки ACP agy: байты, история, встречные запросы и отсутствие скрытой потери медиа."""
import asyncio
import base64
import os
from pathlib import Path
import sys
import tempfile
import unittest

from bender.router.agy_media import prepare, stream_acp
from bender.router.capabilities import description
from bender.router.errors import ProviderError
from bender.router.probes import png
from bender.voice.input import supports
from types import SimpleNamespace

FAKE = '''import json,sys
def send(ev):
 print(json.dumps(ev),flush=True)
for line in sys.stdin:
 ev=json.loads(line)
 if 'method' not in ev:
  assert ev.get('error',{}).get('code')==-32601 or ev.get('result',{}).get('outcome',{}).get('outcome')=='cancelled'
  continue
 method=ev['method'];params=ev['params'];res={}
 if method=='initialize':res={'agentCapabilities':{'promptCapabilities':{'image':True,'audio':False}}}
 if method=='session/new':res={'sessionId':'test-session'}
 if method=='session/set_mode':assert params['modeId']=='plan'
 if method=='session/set_model':assert params['modelId']=='gemini-test'
 if method=='session/set_config_option':assert params['value']=='low'
 if method=='session/prompt':
  assert params['prompt'][1]['type']=='image'
  assert params['prompt'][1]['mimeType']=='image/png'
  send({'jsonrpc':'2.0','id':90,'method':'session/request_permission','params':{'sessionId':'test-session'}})
  send({'jsonrpc':'2.0','id':91,'method':'fs/read_text_file','params':{'path':'secret'}})
  send({'jsonrpc':'2.0','method':'session/update','params':{'sessionId':'test-session','update':{'sessionUpdate':'agent_message_chunk','content':{'type':'text','text':'green'}}}})
  res={'stopReason':'end_turn'}
 send({'jsonrpc':'2.0','id':ev['id'],'result':res})
'''


class AgyMediaTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.part={'type':'image','media_type':'image/png','data':base64.b64encode(png((20,190,40))).decode()}

    def test_preserves_images_from_tools_and_history(self):
        rows=[{'role':'user','content':[{'type':'text','text':'one'},self.part]},
              {'role':'tool','name':'look','tool_call_id':'id','content':[self.part]}]
        prepared,media=prepare(rows)
        self.assertEqual(len(media),2)
        self.assertEqual(media[0]['data'],self.part['data'])
        self.assertIn('изображение 1',prepared[0]['content'])
        self.assertIn('изображение 2',prepared[1]['content'])
        self.assertEqual(prepared[1]['tool_call_id'],'id')
        self.assertIsInstance(rows[0]['content'],list)

    def test_audio_and_bad_images_are_not_silently_discarded(self):
        for part in ({'type':'audio','format':'wav','data':'AAAA'},
                     {**self.part,'data':'not-base64'}, {**self.part,'media_type':'image/svg+xml'}):
            with self.assertRaises(ProviderError):
                prepare([{'role':'user','content':[part]}])
        with self.assertRaises(ProviderError):
            prepare([{'role':'user','content':[self.part]*21}])

    def test_capabilities_keep_audio_on_ears_path(self):
        p={'kind':'antigravity'}
        self.assertIsNone(description(p,'gemini-test','vision')['value'])
        self.assertFalse(description(p,'gemini-test','audio_input')['value'])
        self.assertFalse(supports(SimpleNamespace(provider=p,model='gemini-test'),'wav'))
        p['model_capabilities']={'gemini-test':{'vision':{'value':False,'source':'owner'}}}
        self.assertFalse(description(p,'gemini-test','vision')['value'])

    async def test_acp_image_stream_and_denial_of_builtin_requests(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            script=root/'fake_acp.py'
            script.write_text(FAKE,encoding='utf-8')
            events=[]
            code,_=await stream_acp([sys.executable,str(script)],prompt='color?',media=prepare([{'content':[self.part]}])[1],
                model='gemini-test',effort='low',cwd=root,env=os.environ.copy(),timeout=3,on_event=events.append)
            self.assertEqual(code,0)
            self.assertEqual(events[0]['step_update']['text_delta'],'green')
            self.assertEqual(events[-1]['result']['status'],'SUCCESS')
