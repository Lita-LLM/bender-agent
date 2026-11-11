"""Квоты из настоящего HTTP-ответа фальшивки, паспорт и одно уведомление на окно."""
import json
import time
from unittest.mock import patch

from bender.net.http_server import JSONResponse
from bender.router.core import Candidate
from bender.router.errors import ProviderError
from bender.router.fuel import headers_windows
from bender.mind.loop import Turn
from bender.mind.passport import messages
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv


class FuelTest(TempDirCase):
    @async_test
    async def test_headers_passport_sse_and_once_per_window_across_restart(self):
        async with MindEnv(self.tmp) as env:
            app = env.app; seen = []; app.bus.listen(seen.append)
            pid = app.models.roles()['main'][0]['provider']
            state = {'used': 70, 'reset': int(time.time())+3600}
            @env.fake.route('POST', '/fuel/chat/completions')
            async def response(req):
                return JSONResponse({'choices':[{'message':{'role':'assistant','content':'Ответ'},'finish_reason':'stop'}]}, headers={
                    'X-Codex-Primary-Used-Percent':str(state['used']), 'x-codex-primary-window-minutes':'300',
                    'x-codex-primary-reset-at':str(state['reset']), 'x-secret':'DO-NOT-STORE'})
            app.models.update_provider(pid, {'base_url':env.base+'/fuel', 'no_stream':True})
            source=[{'role':'system','content':'Правила'},{'role':'user','content':'Текст'}]
            for used in (70,85,100,100):
                state['used']=used
                await app.router.call(source)
            await app.mind.idle()
            self.assertEqual(len([e for e in seen if e['type']=='notify' and '100%' in e['text']]),1)
            self.assertEqual(len([e for e in seen if e['type']=='notify']),2)
            self.assertTrue(any(e['type']=='fuel' for e in seen))
            snapshot=app.fuel.snapshot(); self.assertEqual(snapshot['accounts'][0]['windows'][0]['used_percent'],100)
            self.assertEqual(snapshot['accounts'][0]['models'][0]['health'],'ok')
            self.assertNotIn('DO-NOT-STORE',json.dumps(snapshot,ensure_ascii=False))
            sent=messages(app,Turn(env.sid,'pult'),app.router.chain('main')[0],source)
            self.assertIn('Топливо', sent[-1]['content']); self.assertNotIn('Топливо',sent[0]['content'])
            app._fuel=None
            await app.router.call(source); await app.mind.idle()
            self.assertEqual(len([e for e in seen if e['type']=='notify']),2)
            state['reset']+=3600
            await app.router.call(source); await app.mind.idle()
            self.assertEqual(len([e for e in seen if e['type']=='notify']),3)
            app.settings.set('fuel',{'notify':False});state['reset']+=3600
            await app.router.call(source);await app.mind.idle()
            self.assertEqual(len([e for e in seen if e['type']=='notify']),3)

    def test_known_header_families_invalid_values_and_reset_formats(self):
        rows=dict(headers_windows({'x-ratelimit-limit-requests':'100','x-ratelimit-remaining-requests':'25','x-ratelimit-reset-requests':'1m2s',
              'anthropic-ratelimit-tokens-limit':'1000','anthropic-ratelimit-tokens-remaining':'500','anthropic-ratelimit-tokens-reset':'2026-10-24T00:00:00Z',
              'anthropic-ratelimit-unified-5h-utilization':'0.95','anthropic-ratelimit-unified-5h-reset':'1792886400',
              'x-codex-primary-used-percent':'NaN'},1000))
        self.assertEqual(rows['x-ratelimit-requests']['used_percent'],75)
        self.assertEqual(rows['x-ratelimit-requests']['reset_at'],1062)
        self.assertEqual(rows['anthropic-ratelimit-tokens']['used_percent'],50)
        self.assertEqual(rows['anthropic-ratelimit-unified-5h']['used_percent'],95)
        self.assertNotIn('x-codex-primary-',rows)

    @async_test
    async def test_cli_event_error_scrubbing_and_home_channel(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;seen=[];app.bus.listen(seen.append)
            app.store.update_session(env.sid, meta={'home_channel':'pult'})
            cand=app.router.chain('main')[0];app.secrets.set('test','secret-for-test')
            app.fuel.observe(cand,0,'claude',{'rateLimitType':'five_hour','utilization':1,'resetsAt':int(time.time())+3000})
            app.fuel.observe(cand,0,'error',{'http':429,'class':'rate_limit','body':'secret-for-test https://example.org/?token=123'})
            await app.mind.idle()
            result=app.fuel.snapshot();encoded=json.dumps(result,ensure_ascii=False)
            self.assertNotIn('secret-for-test',encoded);self.assertNotIn('example.org',encoded)
            self.assertEqual(result['accounts'][0]['last_limit_error']['http'],429)
            self.assertTrue(all(e['channel']=='pult' for e in seen if e['type']=='notify'))

    @async_test
    async def test_omniroute_button_uses_cached_quota_api_and_own_bearer(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;requests=[]
            @env.fake.route('GET','/api/usage/om-usage')
            async def quota(req):
                requests.append(req.headers.get('authorization'))
                return JSONResponse({'allowed':True,'providers':[{'connectionId':'private-id','provider':'codex','quotas':{'session':{'usedPercentage':62,'resetAt':'2026-10-25T00:00:00Z'}}}]})
            p=app.models.add_provider(preset='omniroute',base_url=env.base+'/v1',api_key='fake-quota-key')
            result=await app.fuel.refresh()
            self.assertEqual(requests,['Bearer fake-quota-key'])
            self.assertEqual(result['accounts'][0]['windows'][0]['used_percent'],62)
            self.assertNotIn('private-id',json.dumps(result))
            self.assertFalse(env.fake.state.requests)
