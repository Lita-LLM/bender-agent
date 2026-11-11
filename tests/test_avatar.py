"""Лицо хранится отдельно, принимает только параметры и сохраняет поля будущего."""
import json
from bender.memory.avatar import read, write, validate
from bender.core.errors import HumanError
from tests.helpers import TempDirCase, async_test


class AvatarTest(TempDirCase):
    def test_params_and_future_fields(self):
        path=self.tmp/'AVATAR.json'
        path.write_text(json.dumps({'future':42,'shape':'round'}))
        self.assertEqual(read(self.tmp)['shape'],'round')
        write(self.tmp,{'color':'#112233','shape':'square','eyes':'slit'})
        self.assertEqual(json.loads(path.read_text())['future'],42)
        self.assertEqual(read(self.tmp)['color'],'#112233')
        for value in ({'color':'url(javascript:x)'},{'shape':'script'},{'eyes':'<svg>'}):
            with self.assertRaises(HumanError):validate(value)

    @async_test
    async def test_owner_api_roundtrip(self):
        from bender.core.app import App
        from tests.helpers import owner_web as create_web, owner_headers
        from bender.net.http_server import HttpServer
        from bender.net.http_client import HttpClient
        app=App(self.tmp/'data')
        server=HttpServer(create_web(app,allowed_hosts={'127.0.0.1'}))
        await server.start('127.0.0.1',0)
        client=HttpClient()
        try:
            base=f'http://127.0.0.1:{server.port}'
            response=await client.post(base+'/api/avatar',json={'shape':'square','eyes':'slit','color':'#ffb840'},headers=owner_headers(**{'X-Bender':'1'}))
            self.assertEqual(response.status,200)
            await response.read()
            response=await client.get(base+'/api/state',headers=owner_headers())
            self.assertEqual((await response.json())['avatar']['shape'],'square')
        finally:
            await client.close();await server.stop();await app.close()
