"""Общий переезд: новые агенты, неизменный источник, ключи и явная замена."""
import asyncio
import json
from bender.core.errors import HumanError
from bender.importers import flock
from tests.helpers import async_test
from tests.test_import import ImportCase, tree_hash


class FlockImportTest(ImportCase):
    @async_test
    async def test_preview_then_two_agents_preserves_main_source_and_credentials(self):
        app=self.app
        app.selfdir.write('MEMORY.md','Память хозяина')
        main_before=app.selfdir.read('MEMORY.md')
        from bender.channels.telegram import TOKEN_SECRET
        app.secrets.set(TOKEN_SECRET,'123:abc')
        key=app.secrets.reuse('existing:key','sk-fake-123456')
        config=json.loads((self.src/'openclaw.json').read_text())
        workspace=self.src/'work-two';workspace.mkdir()
        (workspace/'MEMORY.md').write_text('Память второго')
        config['channels']={'telegram':{'botToken':'123:abc','allowFrom':['42']}}
        config['agents']['entries']['second']={'name':'Второй','workspace':str(workspace),'agentDir':str(self.src/'agents/second/agent')}
        (self.src/'openclaw.json').write_text(json.dumps(config))
        before=tree_hash(self.src)
        plan=await asyncio.to_thread(flock.make_plan,app,'openclaw',str(self.src))
        again=await asyncio.to_thread(flock.make_plan,app,'openclaw',str(self.src))
        self.assertEqual(plan['hash'],again['hash'])
        self.assertEqual(len(app.agents.public()),1)
        selected=[i['id'] for i in plan['items'] if i['selected'] and i['action']!='skip']
        report=await flock.apply(app,'openclaw',str(self.src),expected=plan['hash'],items=selected)
        self.assertEqual(report['errors'],[])
        self.assertEqual(len(report['agents']),2)
        self.assertEqual([app.agents.rights.effective(app.agents.get(a['id']))['profile'] for a in report['agents']],['full','helper'])
        self.assertEqual(app.selfdir.read('MEMORY.md'),main_before)
        self.assertEqual(app.store.sessions('imported'),[])
        agents=[app.agents.get(a['id']) for a in report['agents']]
        self.assertIn('Память второго',agents[1].selfdir.read('MEMORY.md'))
        self.assertNotIn('Память второго',agents[0].selfdir.read('MEMORY.md'))
        self.assertEqual(tree_hash(self.src),before)
        self.assertEqual(app.secrets.get(TOKEN_SECRET),'123:abc')
        self.assertEqual([a.store.meta_get('telegram:import_candidate')['state'] for a in agents],['offer_topic','offer_topic'])
        self.assertEqual(app.secrets.reuse('another:key','sk-fake-123456'),key)
        self.assertEqual(len(app.agents.public()),3)

    @async_test
    async def test_replace_requires_confirmation_and_changed_plan_is_rejected(self):
        app=self.app
        plan=await asyncio.to_thread(flock.make_plan,app,'openclaw',str(self.src),'','current')
        with self.assertRaises(HumanError):
            await flock.apply(app,'openclaw',str(self.src),destination='current',expected=plan['hash'],items=[])
        (self.src/'workspace/MEMORY.md').write_text('Изменение после предпросмотра')
        with self.assertRaises(HumanError):
            await flock.apply(app,'openclaw',str(self.src),destination='current',expected=plan['hash'],items=[],confirmed=True)
        self.assertFalse(app.selfdir.relocating())

    def test_shared_key_collision_and_removing_provider_preserve_other_user(self):
        app=self.app
        one=app.models.add_provider(name='Первый',base_url='https://one.invalid/v1',api_key='shared')
        two=app.models.add_provider(name='Второй',base_url='https://two.invalid/v1',api_key='shared')
        ref=app.models.providers()[two['id']]['keys'][0]
        app.models.remove_provider(one['id'])
        self.assertEqual(app.secrets.get(ref),'shared')
        self.assertNotEqual(app.secrets.reuse(ref,'different'),ref)
        self.assertEqual(app.secrets.get(ref),'shared')

    def test_foreign_source_and_import_backup_are_closed(self):
        from bender.importers.sourceguard import check
        other= self.app.agents.get(self.app.agents.create('Другой',awaken=False)['id'])
        with self.assertRaises(HumanError):check(self.app,other.dirs.self_dir)
        self.assertTrue(self.app.agents.private_path(other,other.dirs.imports/'before.sqlite','fs.read'))

    @async_test
    async def test_owner_selects_template_and_hash_covers_it(self):
        plan=await asyncio.to_thread(flock.make_plan,self.app,'openclaw',str(self.src))
        source=plan['destinations'][0]['source_agent'];profiles={source:'observer'}
        chosen=await asyncio.to_thread(flock.make_plan,self.app,'openclaw',str(self.src),profiles=profiles)
        self.assertNotEqual(plan['hash'],chosen['hash'])
        with self.assertRaises(HumanError):
            await flock.apply(self.app,'openclaw',str(self.src),expected=plan['hash'],items=[],profiles=profiles)
        selected=[i['id'] for i in chosen['items'] if i['selected'] and i['action']!='skip']
        result=await flock.apply(self.app,'openclaw',str(self.src),expected=chosen['hash'],items=selected,profiles=profiles)
        ctx=self.app.agents.get(result['agents'][0]['id'])
        self.assertEqual(self.app.agents.rights.effective(ctx)['profile'],'observer')
