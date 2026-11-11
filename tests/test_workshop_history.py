"""Прежние правки ядра — только история; ни тул, ни слот их не применяют."""
from unittest.mock import patch
from bender.core.app import App
from bender.core.errors import HumanError
from bender.core.paths import PROGRAM_DIR
from bender.core.settings import SCHEMA
from bender.guard.policy import Action
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test


class HistoryTest(TempDirCase):
    @async_test
    async def test_legacy_core_readonly_and_setting_preserved(self):
        app=App(self.tmp/'data')
        try:
            from bender.core.settings import Settings
            app.store.x("INSERT OR REPLACE INTO settings(grp,value,updated_at) VALUES('workshop',?,0)", ('{"changes_enabled":true,"future":42}',))
            previous = Settings(app.store)
            previous.set('workshop',{'lessons':False})
            self.assertTrue(previous.group('workshop')['changes_enabled'])
            self.assertEqual(previous.group('workshop')['future'],42)
            record={'id':'p-oldcore','kind':'core','status':'approved','name':'Прежняя правка','diff':'+pass','hash':'старый'}
            app.workshop.save(record)
            changes=app.store.db.total_changes
            self.assertEqual(app.workshop.public()['proposals'][0],record)
            self.assertEqual(app.store.db.total_changes,changes)
            self.assertNotIn('propose_change',app.tools.tools)
            self.assertNotIn('changes_enabled',[f.key for f in SCHEMA['workshop'].fields])
            self.assertFalse((await app.tools.run('propose_change',{},ToolContext(app,actor='owner'))).ok)
            self.assertFalse(hasattr(app.versions,'stage_workshop'))
            app.settings.set('updates',{'allow_development':True})
            with patch.object(app.versions,'inspect',return_value={'hash':'старый','workshop_proposal':record['id']}):
                with self.assertRaises(HumanError): app.versions.install('v-old','старый')
            from tests.helpers import owner_web as create_web, owner_headers
            from bender.net.http_server import HttpServer
            from bender.net.http_client import HttpClient
            server=HttpServer(create_web(app,allowed_hosts={'127.0.0.1','localhost'}))
            await server.start('127.0.0.1',0)
            client=HttpClient()
            try:
                base=f'http://127.0.0.1:{server.port}'
                response=await client.get(base+'/api/workshop/p-oldcore',headers=owner_headers())
                self.assertEqual((await response.json())['diff'],'+pass')
                for action in ('approve','reject','definitions','enabled'):
                    response=await client.post(base+'/api/workshop/p-oldcore/'+action,json={'hash':'старый','enabled':True},headers=owner_headers(**{'X-Bender':'1'}))
                    self.assertGreaterEqual(response.status,400)
                    await response.read()
            finally:
                await client.close()
                await server.stop()
            for relative in ('bender/body/__init__.py','bender/memory/index.py','bender/web/static/app.js'):
                self.assertEqual(app.guard.policy.decide(Action('fs.write',str(PROGRAM_DIR/relative))).rule,'sealed')
        finally:await app.close()
