"""Отказ модели после тула не стирает историю и не повторяет действие."""
import asyncio
import json
from unittest.mock import patch

from bender.net.http_server import JSONResponse, StreamResponse
from bender.router.chatgpt import ChatGPTAdapter
from bender.router.errors import ProviderError
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test
from tests.test_chatgpt import Env, READ
from tests.test_mind import MindEnv, call


class AcceptanceModelsTest(TempDirCase):
    @async_test
    async def test_network_failure_does_not_add_main(self):
        async with MindEnv(self.tmp) as env:
            app = env.app; pid = app.models.roles()['main'][0]['provider']
            app.models.set_role('fast', [{'provider': pid, 'model': 'error-500'}])
            with patch('bender.router.core.MAX_SAME_RETRIES', {}), self.assertRaises(ProviderError):
                await app.router.call([{'role': 'user', 'content': 'проверка'}], role='fast', reject_fallback=True)
            models = [r['body']['model'] for r in env.fake.state.requests if r.get('api') == 'openai']
            self.assertEqual(models, ['error-500'])

    @async_test
    async def test_subscription_minimal_body_on_strict_server_and_model_blocks(self):
        async with Env(self.tmp) as env:
            await env.login()
            seen = []
            @env.fake.route('POST', '/strict/codex/responses')
            async def strict(req):
                data = req.json(); seen.append(data)
                if data.get('reasoning', {}).get('summary') or data.get('text', {}).get('verbosity'):
                    return JSONResponse({'error': {'message': 'Unsupported parameter: text.verbosity / reasoning.summary'}}, 400)
                async def events():
                    yield b'data: {"type":"response.output_text.delta","delta":"ok"}\n\n'
                    yield b'data: {"type":"response.completed","response":{"status":"completed"}}\n\n'
                return StreamResponse(events(), content_type='text/event-stream')
            env.app.models.update_provider(env.provider['id'], {'base_url': env.base + '/strict'})
            with self.assertRaises(ProviderError) as rejected:
                await env.app.router.call([{'role': 'user', 'content': 'проверка'}], target=(env.provider['id'], 'gpt-6-luna'), tools=[READ],
                                          params={'verbosity': 'medium', 'reasoning_effort': 'medium', 'reasoning_summary': 'auto'})
            self.assertEqual(rejected.exception.http_status, 400)
            result = await env.app.router.call([{'role': 'user', 'content': 'проверка'}], target=(env.provider['id'], 'gpt-6-luna'), tools=[READ])
            self.assertEqual(result.text, 'ok')
            self.assertTrue(seen[0]['tools'])
            self.assertNotIn('text', seen[-1])
            self.assertNotIn('reasoning', seen[-1])
            hist = [{'role': 'assistant', 'provider_id': env.provider['id'], 'provider_model': 'other', 'content': '',
                     'provider_blocks': [{'type': 'reasoning', 'encrypted_content': 'чужое'}]}]
            body = ChatGPTAdapter(env.provider).body('gpt-6-luna', hist, [READ], {}, True)
            self.assertEqual(body['input'], [])
            hist[0]['provider_model'] = 'gpt-6-luna'
            body = ChatGPTAdapter(env.provider).body('gpt-6-luna', hist, [READ], {'reasoning_effort': 'high', 'reasoning_summary': 'auto', 'verbosity': 'low'}, True)
            self.assertEqual(body['input'][0]['encrypted_content'], 'чужое')
            self.assertEqual(body['reasoning'], {'effort': 'high', 'summary': 'auto'})

    @async_test
    async def test_rejected_role_tries_chain_then_main(self):
        for own_fallback, code in ((True, 400), (False, 400), (False, 403), (False, 404)):
            async with MindEnv(self.tmp / (str(own_fallback) + str(code))) as env:
                app = env.app; pid = app.models.roles()['main'][0]['provider']
                app.models.set_role('fast', [{'provider': pid, 'model': 'error-' + str(code)}] + ([{'provider': pid, 'model': 'script'}] if own_fallback else []))
                env.script([{'text': 'Запасная ответила.'}])
                task = app.team.spawn('scribe', 'Проверка', parent_session=env.sid)
                [done] = await app.team.wait([task['id']], 5)
                self.assertEqual(done['status'], 'done')
                self.assertEqual(done['result'], 'Запасная ответила.')
                line = json.dumps(app.team.timeline(done), ensure_ascii=False)
                self.assertIn('HTTP ' + str(code), line)
                self.assertIn('запасная модель', line)
                self.assertIn('error-' + str(code), line)

    @async_test
    async def test_steps_and_error_body_survive_switch_without_repeating_write(self):
        async with MindEnv(self.tmp) as env:
            app = env.app; pid = app.models.roles()['main'][0]['provider']
            app.models.set_role('fast', [{'provider': pid, 'model': 'script'}])
            app.models.set_role('main', [{'provider': pid, 'model': 'echo'}])
            secret = 'sk-SERVER-ECHO-SECRET-123456'
            app.secrets.set('test', secret)
            env.script([call('write_file', path='итог.txt', content='один'), {'error': 400, 'message': 'Unsupported parameter: verbosity ' + secret}])
            task = app.team.spawn('coder', 'Сохрани', model_role='fast', parent_session=env.sid)
            [done] = await app.team.wait([task['id']], 5)
            self.assertEqual(done['status'], 'done')
            self.assertEqual((app.dirs.work / 'итог.txt').read_text(), 'один')
            timeline = app.team.timeline(done)
            writes = [row for row in timeline if row.get('tool') == 'write_file']
            self.assertEqual(len(writes), 1)
            self.assertTrue(writes[0]['ok'])
            status = await app.tools.run('tasks', {'action': 'status', 'id': task['id']}, ToolContext(app, session_id=env.sid))
            self.assertIn('Unsupported parameter', status.content)
            self.assertIn('HTTP 400', status.content)
            self.assertNotIn(secret, status.content)
            persisted = json.dumps(app.store.messages(done['session_id']), ensure_ascii=False)
            self.assertNotIn(secret, persisted)

    @async_test
    async def test_owner_model_probe_sends_one_tool_and_shows_rejection(self):
        from bender.net.http_server import HttpServer
        from tests.helpers import owner_web as create_web, owner_headers
        from tests.test_diagnostics import _call
        async with MindEnv(self.tmp) as env:
            web = HttpServer(create_web(env.app, allowed_hosts={'127.0.0.1'}))
            await web.start('127.0.0.1', 0)
            pid = env.app.models.roles()['main'][0]['provider']
            try:
                env.script([{'text': 'ok'}])
                code, data = await asyncio.to_thread(_call, web.port, '/api/try/model-tools', {'provider': pid, 'model': 'script'})
                self.assertEqual((code, data['state']), (200, 'работает с тулами'))
                request = next(r['body'] for r in env.fake.state.requests if r.get('api') == 'openai')
                self.assertEqual(len(request['tools']), 1)
                code, data = await asyncio.to_thread(_call, web.port, '/api/try/model-tools', {'provider': pid, 'model': 'error-400'})
                self.assertEqual((code, data['state'], data['http']), (200, 'не принимает', 400))
            finally:
                await web.stop()
