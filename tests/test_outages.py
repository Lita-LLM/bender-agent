"""Десять виртуальных минут обрыва настоящих сокетов: паузы, возврат и отмена."""
import asyncio
from types import SimpleNamespace
from unittest.mock import patch
from bender.core.app import App
from bender.devtools.fake_mail import FakeMail
from bender.net.http_server import HttpServer,Router,JSONResponse
from tests.helpers import TempDirCase,async_test


class AsyncClock:
    def __init__(self):self.now=0;self.waits=[];self.on_wait=None
    def __getattr__(self,name):return getattr(asyncio,name)
    async def sleep(self,delay):
        self.waits.append(delay);self.now+=delay
        if self.on_wait:await self.on_wait()
        await asyncio.sleep(0)
    async def wait_for(self,coro,timeout):
        coro.close()
        await self.sleep(timeout)
        raise asyncio.TimeoutError()


class OutageTest(TempDirCase):
    @async_test
    async def test_telegram_recovers_without_request_storm(self):
        app=App(self.tmp/'data');clock=AsyncClock();router=Router();calls=[];success=asyncio.Event()
        @router.route('POST','/bot123:FAKE/{method}')
        async def api(req):
            calls.append((req.params['method'],clock.now))
            if clock.now<600:return JSONResponse({'ok':False,'description':'Нет сети','error_code':503},status=503)
            if req.params['method']=='getMe':return JSONResponse({'ok':True,'result':{'id':1,'username':'fake'}})
            success.set();await asyncio.sleep(30)
            return JSONResponse({'ok':True,'result':[]})
        server=HttpServer(router);await server.start('127.0.0.1',0)
        app.settings.set('telegram',{'api_base':f'http://127.0.0.1:{server.port}'})
        app.secrets.set('telegram:token','123:FAKE')
        try:
            with patch('bender.channels.telegram.asyncio',clock):
                task=asyncio.create_task(app.telegram._run())
                try:
                    await asyncio.wait_for(success.wait(),3)
                    self.assertEqual(app.telegram.state,'ok')
                    # Не чаще раза в 30 с за 10 минут обрыва, а когда сеть вернулась — бот снова слышит не позже чем через 30 с
                    # (раньше пауза дорастала до 5 минут, и бот молчал, когда связь уже была).
                    self.assertLessEqual(len(calls),25)
                    self.assertGreaterEqual(clock.now,600)
                    self.assertLess(clock.now,600+30+5)
                    self.assertEqual(clock.waits[:5],[5,10,20,30,30])
                finally:
                    task.cancel();await asyncio.gather(task,return_exceptions=True)
        finally:await app.close();await server.stop()

    @async_test
    async def test_mail_recovers_after_network_returns(self):
        from tests.test_watchdog import _free_port
        app=App(self.tmp/'data');clock=AsyncClock();mail=FakeMail();imap,smtp=_free_port(),_free_port();success=asyncio.Event();attempts=[]
        app.settings.set('mail',{'enabled':True,'address':'owner@example.com','imap_host':'127.0.0.1','imap_port':imap,'imap_security':'none','smtp_host':'127.0.0.1','smtp_port':smtp,'smtp_security':'none'})
        app.secrets.set('mail:password','secret')
        original=app.mail.poll
        async def poll():
            attempts.append(clock.now)
            result=await original();success.set()
            # Удерживаем уже успешную проверку: ускоренный цикл иначе успеет начать следующую.
            await asyncio.Event().wait()
            return result
        async def restore():
            if clock.now>=600 and not mail.imap:await mail.start(imap,smtp)
        clock.on_wait=restore
        try:
            with patch('bender.channels.mail.asyncio',clock),patch.object(app.mail,'poll',side_effect=poll):
                task=asyncio.create_task(app.mail._run())
                try:
                    await asyncio.wait_for(success.wait(),3)
                    self.assertEqual(app.mail.state,'ok')
                    self.assertLessEqual(len(attempts),7)
                    self.assertGreaterEqual(clock.now,600)
                    self.assertEqual(clock.waits[:3],[60,120,240])
                finally:task.cancel();await asyncio.gather(task,return_exceptions=True)
        finally:await app.close();await mail.stop()
