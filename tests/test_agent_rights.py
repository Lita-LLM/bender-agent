"""Потолок не расширяется правилом, перепиской, настройкой или ролью команды."""
from bender.core.errors import HumanError
from bender.core.settings import NeedsOwner
from bender.guard.policy import Action
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase,async_test
from tests.test_mind import MindEnv


class AgentRightsTest(TempDirCase):
    @async_test
    async def test_templates_tools_folders_network_and_grants_cannot_expand(self):
        async with MindEnv(self.tmp) as env:
            host=env.app;host.agents.create('Помощник',agent_id='helper',awaken=False)
            ctx=host.agents.get('helper')
            self.assertTrue(ctx.tools.enabled('write_file'));self.assertFalse(ctx.tools.enabled('run'))
            ctx.guard.grant('test',[('*','*'),('exec','*'),('net.send','*')])
            ctx.settings.set('guard',{'enabled':False})
            for action in (Action('exec','true'),Action('net.send','https://example.org'),Action('fs.read',str(self.tmp/'outside.txt'))):
                self.assertFalse((await ctx.guard.check(action,session_id='test')).allowed)
            ctx.settings.set('agent_rights',{'network_hosts':['example.org'],'tools':['web_fetch','about_me']})
            self.assertFalse(ctx.tools.enabled('write_file'));self.assertTrue(ctx.tools.enabled('web_fetch'))
            self.assertFalse((await ctx.guard.check(Action('net.read','https://foreign.org'))).allowed)
            self.assertTrue((await ctx.guard.check(Action('net.read','https://example.org'))).allowed)
            ctx.settings.set('agent_rights',{'profile':'reader','tools':None})
            self.assertFalse(ctx.tools.specs())
            with self.assertRaises(HumanError):host.agents.rights.check_spawn(ctx)
            with self.assertRaises(HumanError):ctx.settings.set('agent_rights',{'network_hosts':['https://invalid/']})

    @async_test
    async def test_settings_and_peer_message_cannot_confirm_rights(self):
        async with MindEnv(self.tmp) as env:
            host=env.app;host.agents.create('Один',agent_id='one',awaken=False);ctx=host.agents.get('one')
            with self.assertRaises(NeedsOwner):ctx.settings.set('agent_rights',{'profile':'full'},by='agent')
            calls=[];host.bus.listen(calls.append)
            tool=ToolContext(ctx,session_id=ctx.main_session(),actor='agent:main',tainted=True,unattended=True)
            result=await ctx.tools.run('settings',{'group':'agent_rights','changes':{'profile':'full'}},tool)
            self.assertFalse(result.ok);self.assertEqual(host.agents.rights.effective(ctx)['profile'],'helper')
            self.assertFalse(any(e['type']=='approval' for e in calls))
            ctx.settings.set('agent_rights',{'settings_free':'deny'})
            with self.assertRaises(HumanError):ctx.settings.set('appearance',{'large_font':True},by='agent')
            ctx.settings.set('agent_rights',{'settings_free':'ask'})
            with self.assertRaises(NeedsOwner):ctx.settings.set('appearance',{'large_font':True},by='agent')
            # Настоящее подтверждение хозяина меняет только выбранного агента.
            ctx.settings.set('agent_rights',{'settings_free':'allow'})
            host.bus.listen(lambda e:host.guard.respond(e['card']['id'],'once') if e['type']=='approval' else None)
            result=await ctx.tools.run('settings',{'group':'agent_rights','changes':{'profile':'full'}},ToolContext(ctx,session_id=ctx.main_session()))
            self.assertTrue(result.ok,result.content);self.assertEqual(host.agents.rights.effective(ctx)['profile'],'full')
            self.assertEqual(host.agents.rights.effective(host)['profile'],'full')

    @async_test
    async def test_spawn_intersection_daily_budget_and_owner_profile_ceiling(self):
        async with MindEnv(self.tmp) as env:
            host=env.app;host.agents.create('Помощник',agent_id='helper',awaken=False);ctx=host.agents.get('helper')
            task=ctx.team.spawn('coder','Проверь',parent_session=ctx.main_session())
            saved=ctx.team.get(task['id'])['budget']['authority']
            self.assertNotIn('exec',saved['capabilities']);self.assertNotIn('run',saved['tools'])
            ctx.team.cancel(task['id'])
            ctx.settings.set('agent_rights',{'daily_usd':0.01,'guard_profile':'careful','team_enabled':False})
            with self.assertRaises(HumanError):ctx.team.spawn('helper','Нельзя')
            ctx.settings.set('guard',{'profile':'off','enabled':False})
            self.assertEqual(ctx.guard.policy.get_settings()['profile'],'careful')
            ctx.store.add_call(session_id=ctx.main_session(),role='main',provider='fake',model='script',usage={},cost=.02,seconds=0,notice=None,request=None)
            with self.assertRaises(HumanError):await ctx.router.call([{'role':'user','content':'Бюджет'}])
            # Бюджет соседа не закрывает главного агента.
            env.script([{'text':'Главный работает'}])
            result=await host.router.call([{'role':'user','content':'Привет'}]);self.assertEqual(result.text,'Главный работает')
