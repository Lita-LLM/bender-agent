"""Свободные настройки, предложения хозяину и закрытые настройки доверия."""
from bender.core.errors import HumanError
from bender.core.settings import NeedsOwner
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv


class AuthorityTest(TempDirCase):
    @async_test
    async def test_free_preferences_role_notice_journal_and_unknown_fields(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;seen=[];app.bus.listen(seen.append);pid=app.models.roles()['main'][0]['provider']
            for group,changes in [('heart',{'quiet_hours':'22:00-09:00','interval_minutes':45}),('sleep',{'at':'04:00'}),('agent',{'language':'ru','memory_notices':'verbose'}),('appearance',{'large_font':True}),('voice',{'enabled':True,'auto_speak':True}),('telegram',{'notify':False}),('search',{'providers':['ddg','brave']})]:
                app.settings.set(group,changes,by='agent')
            app.models.set_role('fast',[{'provider':pid,'model':'before','future':{'keep':1}}])
            result=await app.tools.run('settings',{'group':'roles','changes':{'fast':[{'provider':pid,'model':'after'}]},'reason':'меньше задержка'},ToolContext(app,session_id=env.sid))
            self.assertTrue(result.ok,result.content)
            self.assertEqual(app.models.roles()['fast'][0]['future'],{'keep':1})
            notices=[e for e in seen if e['type']=='notify' and e['kind']=='models']
            self.assertEqual(len(notices),1);self.assertEqual(notices[0]['channel'],'pult')
            self.assertIn('меньше задержка',notices[0]['text'])
            self.assertTrue(any(m['role']=='event' and 'Переключил fast' in m['content'] for m in app.store.messages(env.sid)))
            self.assertTrue(app.journal.verify()['ok'])
            self.assertFalse(any(e['type']=='approval' for e in seen))

    @async_test
    async def test_role_paid_transition_removal_and_approved_change(self):
        async with MindEnv(self.tmp) as env:
            app=env.app;pid=app.models.roles()['main'][0]['provider']
            paid=app.models.add_provider(name='Платный',base_url=env.base+'/v1',api_key='fake-paid')
            with self.assertRaises(NeedsOwner):app.settings.set('roles',{'fast':[{'provider':paid['id'],'model':'paid'}]},by='agent')
            app.models.set_role('fast',[{'provider':pid,'model':'a'},{'provider':pid,'model':'b'}])
            with self.assertRaises(NeedsOwner):app.settings.set('roles',{'fast':[{'provider':pid,'model':'c'}]},by='agent')
            with self.assertRaises(NeedsOwner):app.settings.set('roles',{'fast':[{'provider':pid,'model':'a'},{'provider':pid,'model':'b','enabled':False}]},by='agent')
            with self.assertRaises(HumanError):app.settings.set('roles',{'fast':[{'provider':'new','model':'a'}]},by='agent')
            env.auto_approve()
            result=await app.tools.run('settings',{'group':'roles','changes':{'fast':[{'provider':paid['id'],'model':'paid'}]},'reason':'хозяин просит'},ToolContext(app,session_id=env.sid))
            self.assertTrue(result.ok,result.content);self.assertEqual(len(env.approvals),1)
            self.assertEqual(app.models.roles()['fast'][0]['provider'],paid['id'])

    @async_test
    async def test_ask_owner_only_limits_network_and_own_skill(self):
        async with MindEnv(self.tmp) as env:
            app=env.app
            app.settings.set("agent", {"max_steps": 25})
            app.settings.set("team", {"max_seconds": 600})
            for group,changes in [('agent',{'max_steps':100}),('team',{'max_seconds':1800}),('mcp',{'timeout_seconds':40}),('telegram',{'api_base':'https://elsewhere.example'}),('backups',{'include_secrets':True}),('updates',{'auto':'night'})]:
                with self.assertRaises(NeedsOwner,msg=group):app.settings.set(group,changes,by='agent')
            with self.assertRaises(HumanError) as e:app.settings.set('workshop',{'network_proxy':True},by='agent')
            self.assertEqual(e.exception.code,'owner_only')
            app.settings.set('skill_trust',{'mine':{'hash':'approved','origin':'experience'},'foreign':{'hash':'approved','origin':'owner'}})
            app.settings.set('skills',{'disabled':['mine','foreign']})
            app.settings.set('skills',{'disabled':['foreign']},by='agent')
            with self.assertRaises(NeedsOwner):app.settings.set('skills',{'disabled':[]},by='agent')
            fields={(g['name'],f['key']):f for g in app.settings.describe() for f in g['fields']}
            self.assertEqual(fields['agent','language']['permission'],'free')
            self.assertEqual(fields['agent','max_steps']['permission'],'ask')
            self.assertEqual(fields['workshop','network_proxy']['permission'],'owner')
