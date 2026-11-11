"""Повторная работа не удерживает завершённые ходы, слушателей и старые соединения."""
import asyncio
from bender.core.app import App
from bender.net.http_client import HttpClient
from bender.net.http_server import HttpServer, Router, TextResponse
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv


class LifecycleTest(TempDirCase):
    @async_test
    async def test_finished_sessions_release_turns(self):
        async with MindEnv(self.tmp) as env:
            for i in range(25):
                sid=env.app.store.new_session('chat','Обкатка '+str(i))
                env.script([{'text':'Завершено'}])
                result=await env.app.mind.say(sid,'Проверка')
                self.assertIsNone(result['error'])
                await env.app.mind.idle()
            await asyncio.sleep(0)
            self.assertEqual(len(env.app.mind.turns),0)
            self.assertEqual(len(env.app.mind.background),0)

    @async_test
    async def test_heart_restarts_do_not_duplicate_listeners(self):
        app=App(self.tmp/'data')
        try:
            app.settings.set('heart',{'mode':'off'})
            app.settings.set('sleep',{'enabled':False})
            for _ in range(12):
                app.heart.start();await asyncio.sleep(0);await app.heart.stop()
            self.assertEqual(app.bus._listeners.count(app.heart._on_event),1)
        finally:await app.close()

    @async_test
    async def test_http_pool_is_bounded_across_origins(self):
        router=Router()
        @router.route('GET','/')
        async def get(req):return TextResponse('ответ')
        client=HttpClient(use_env_proxy=False);servers=[]
        try:
            for _ in range(40):
                server=HttpServer(router);await server.start('127.0.0.1',0);servers.append(server)
                async with await client.get('http://127.0.0.1:'+str(server.port)+'/') as response:
                    self.assertEqual(await response.read(),b'\xd0\xbe\xd1\x82\xd0\xb2\xd0\xb5\xd1\x82')
            self.assertLessEqual(sum(map(len,client._pool.values())),32)
            for _ in range(3):
                async with await client.get('http://127.0.0.1:'+str(servers[-1].port)+'/') as response:await response.read()
            self.assertLessEqual(sum(map(len,client._pool.values())),32)
        finally:
            await client.close()
            for server in servers:await server.stop()
