"""Мусор и бесконечный поток не удерживают ход; запасная модель видна хозяину."""
import asyncio
from unittest.mock import patch
from bender.net.http_server import TextResponse,StreamResponse
from bender.net.http_client import Timeouts,NetError
from tests.helpers import async_test
from tests.test_router import Env,MSGS
import unittest


class ProviderFaultTest(unittest.TestCase):
    @async_test
    async def test_corrupt_json_falls_back_without_traceback(self):
        async with Env() as env:
            @env.fake.route('POST','/broken/chat/completions')
            async def broken(req):return TextResponse('[1,2,3]',content_type='application/json')
            env.providers['fo']['base_url']=f'http://127.0.0.1:{env.server.port}/broken'
            env.roles['main']=[{'provider':'fo','model':'broken'},{'provider':'fa','model':'echo'}]
            result=await env.sb.call(MSGS)
            self.assertIn('Эхо',result.text)
            self.assertTrue(result.notice)
            self.assertTrue(any(item['class']=='protocol' for item in result.attempts))

    @async_test
    async def test_total_timeout_covers_trickling_body(self):
        async with Env() as env:
            @env.fake.route('GET','/trickle')
            async def trickle(req):
                async def chunks():
                    for _ in range(100):
                        yield b': ping\n\n';await asyncio.sleep(.005)
                return StreamResponse(chunks())
            with self.assertRaises(NetError) as failure:
                async with await env.http.get(f'http://127.0.0.1:{env.server.port}/trickle',timeouts=Timeouts(read=.03,total=.06)) as response:await response.read()
            self.assertEqual(failure.exception.kind,'timeout')

    @async_test
    async def test_hanging_candidate_times_out_and_chain_continues(self):
        async with Env() as env:
            @env.fake.route('POST','/hanging/chat/completions')
            async def hanging(req):await asyncio.sleep(.3);return TextResponse('не успел')
            env.providers['fo'].update(base_url=f'http://127.0.0.1:{env.server.port}/hanging',timeout=.03)
            env.roles['main']=[{'provider':'fo','model':'hang'},{'provider':'fa','model':'echo'}]
            with patch('bender.router.core.BACKOFF_BASE',.001):result=await env.sb.call(MSGS)
            self.assertIn('Эхо',result.text)
            self.assertTrue(result.notice)
            self.assertTrue(env.sb.health.keys or env.sb.health.models)
