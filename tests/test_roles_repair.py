"""Старые повреждённые ссылки чинятся явно и при записи, новый неверный ввод отвергается."""
import io
import json
import zipfile

from bender.core.errors import HumanError
from bender.diagnostics.doctor import collect,repair_roles
from bender.diagnostics.report import build
from tests.helpers import TempDirCase,async_test
from tests.test_mind import MindEnv


class RolesRepairTest(TempDirCase):
    @async_test
    async def test_role_write_drops_existing_damage_but_rejects_new_bad_input(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;provider=app.models.roles()['main'][0]['provider']
            bad={'provider':'исчезнувший-голос','model':'piper-ru','future':'сохранено в журнале'}
            app.settings.replace('roles',{**app.models.roles(),'ears':[bad],'voice':[bad]})
            app.settings.set('roles',{'voice':[{'provider':provider,'model':'voice'},bad]},by='agent')
            self.assertEqual(app.models.roles()['ears'],[])
            self.assertEqual(len(app.models.roles()['voice']),1)
            self.assertEqual(app.store.meta_get('models:role_repair_notice')['count'],2)
            rows=app.journal.recent(kind='models')
            self.assertEqual(len(rows),1);self.assertEqual(rows[0]['data']['count'],2)
            self.assertTrue(app.journal.verify()['ok'])
            with self.assertRaises(HumanError):app.models.set_role('voice',[bad])
            with self.assertRaises(HumanError):app.models.set_role('voice',[{'provider':provider,'model':'voice','params':[]}])

    @async_test
    async def test_chain_warns_once_per_agent_and_distinct_bad_candidate(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;child=app.agents.get(app.agents.create('Сосед',awaken=False)['id'])
            bad={'provider':'gone','model':'old'}
            app.settings.replace('roles',{'ears':[bad]})
            for _ in range(5):
                self.assertEqual(app.router.chain('ears',fallback_main=False),[])
                self.assertEqual(child.router.chain('ears',fallback_main=False),[])
            rows=app.host._shared_journal.recent(kind='models')
            self.assertEqual(len(rows),2)
            self.assertEqual({r['data']['agent_id'] for r in rows},{'main',child.agent_id})
            app.settings.replace('roles',{'ears':[{**bad,'model':'different'}]})
            app.router.chain('ears',fallback_main=False)
            self.assertEqual(len(app.journal.recent(kind='models')),2)
            app.settings.replace('roles',{'ears':[]});app.router.chain('ears',fallback_main=False)
            app.settings.replace('roles',{'ears':[bad]});app.router.chain('ears',fallback_main=False)
            self.assertEqual(len(app.journal.recent(kind='models')),3)

    @async_test
    async def test_doctor_finds_child_roles_repairs_with_journal_and_report_has_no_names(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;child=app.agents.get(app.agents.create('Секретное имя агента',awaken=False)['id'])
            private='Личная модель 83901'
            child.settings.replace('roles',{'voice':[{'provider':'Личный провайдер 32911','model':private}]})
            rows=collect(app.dirs.root)['rows'];bad=next(row for row in rows if row['id']=='role_links')
            self.assertEqual(bad['level'],'error');self.assertEqual(bad['details']['broken'],1)
            raw,_=build(app.dirs.root)
            with zipfile.ZipFile(io.BytesIO(raw)) as archive:
                text=''.join(archive.read(name).decode() for name in archive.namelist())
                for value in (child.agent_id,'Секретное имя агента',private,'Личный провайдер 32911'):self.assertNotIn(value,text)
                report=json.loads(archive.read('doctor.json'))
                self.assertEqual(next(row for row in report['rows'] if row['id']=='role_links')['details']['broken'],1)
            result=await repair_roles(app)
            self.assertEqual(result['count'],1);self.assertEqual(child.models.roles()['voice'],[])
            self.assertTrue(app.journal.verify()['ok'])
            self.assertEqual(next(row for row in collect(app.dirs.root)['rows'] if row['id']=='role_links')['level'],'ok')
            self.assertEqual((await repair_roles(app))['count'],0)
