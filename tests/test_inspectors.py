"""Происхождение, правки источника, слияние без отмывания и живые связи recall."""
import json
from bender.core.app import App
from bender.core.errors import HumanError
from bender.mind.loop import Turn
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase,async_test


class InspectorTest(TempDirCase):
    @async_test
    async def test_file_edit_merge_forget_and_usage(self):
        app=App(self.tmp/'data')
        try:
            diary=app.dirs.diary/'2026-10-22.md'
            diary.write_text('# Наблюдения\n\n- Кошки мурлычут утром.\n\n<!-- source:untrusted -->\n- Кошки спят вечером.\n')
            app.index.sync(None)
            hits=app.index.search('кошки',limit=20,kinds=('diary',))
            self.assertEqual(len(hits),2)
            sid=app.main_session()
            mid=app.store.add_message(sid,{'role':'user','content':'Вспомни кошек'},{'actor':'owner','channel':'pult'})
            app.mind.turns[sid]=Turn(sid,'pult','owner',False,first_id=mid)
            result=await app.tools.run('recall',{'query':'кошки'},ToolContext(app,session_id=sid))
            self.assertTrue(result.ok)
            detail=app.inspector.detail(hits[0].id)
            self.assertGreaterEqual(detail['usage']['hits'],1)
            self.assertEqual(detail['uses'][0]['turn'],mid)
            ids=[h.id for h in hits]
            await app.inspector.change(ids,'merge','- Кошки мурлычут и спят.','Объединить два наблюдения')
            app.index.rebuild()
            merged=app.index.search('кошки',limit=20,kinds=('diary',))
            self.assertEqual(len(merged),1)
            self.assertEqual(merged[0].provenance,'untrusted')
            self.assertIn('# Наблюдения',diary.read_text())
            detail=app.inspector.detail(merged[0].id)
            with self.assertRaises(HumanError):await app.inspector.change([merged[0].id],'edit','новое','Причина','не тот хеш')
            await app.inspector.change([merged[0].id],'forget','','Устаревшие сведения',detail['source_hash'])
            app.index.rebuild()
            self.assertFalse(app.index.search('кошки',kinds=('diary',)))
            self.assertGreaterEqual(app.store.one('SELECT count(*) FROM mem_edit_history')[0],2)
        finally:await app.close()

    @async_test
    async def test_session_overrides_facts_and_forwarded_provenance(self):
        app=App(self.tmp/'data')
        try:
            sid=app.main_session()
            app.store.add_message(sid,{'role':'user','content':'Сиреневые дирижабли'},{'actor':'owner','channel':'telegram','untrusted':True})
            app.index.sync(None)
            hit=app.index.search('дирижабли',kinds=('session',))[0]
            self.assertEqual(hit.provenance,'untrusted')
            self.assertEqual(app.inspector.detail(hit.id)['origin']['meta']['channel'],'telegram')
            await app.inspector.change([hit.id],'edit','Зелёные дирижабли','Исправление цвета')
            app.index.rebuild()
            changed=app.index.search('зелёные',kinds=('session',))[0]
            self.assertEqual(changed.provenance,'untrusted')
            self.assertIn('Сиреневые',app.store.messages(sid)[0]['content'])
            await app.inspector.change([changed.id],'forget','','Не вспоминать этот эпизод')
            app.index.rebuild()
            self.assertFalse(app.index.search('дирижабли',kinds=('session',)))
            fact=app.facts.add('Миша','город','Казань',provenance='owner')
            hit=app.index.search('Казань',kinds=('fact',))[0]
            await app.inspector.change([hit.id],'edit',json.dumps({'subject':'Миша','relation':'город','object':'Самара'}),'Хозяин переехал')
            app.facts.reindex()
            self.assertEqual(app.facts.active()[0]['object'],'Самара')
            self.assertFalse(app.index.search('Казань',kinds=('fact',)))
            self.assertEqual(app.facts.history('Миша')[0]['id'],fact['id'])
        finally:await app.close()

    @async_test
    async def test_journal_filter_and_disabled_inspector(self):
        app=App(self.tmp/'data')
        try:
            app.journal.write('decision','Проба',actor='agent',data={'capability':'fs.write','verdict':'deny','channel':'telegram','session':'s1'})
            rows=app.journal.recent(actor='agent',capability='fs.write',verdict='deny',channel='telegram',session='s1')
            self.assertEqual(len(rows),1)
            self.assertEqual(app.journal.recent(session='s2'),[])
            from bender.guard.policy import Action
            from bender.core.paths import PROGRAM_DIR
            decision=await app.guard.check(Action('fs.write',str(PROGRAM_DIR/'bender/body/__init__.py')),session_id=app.main_session())
            self.assertEqual(decision.rule,'sealed')
            self.assertTrue(app.journal.recent(capability='fs.write',verdict='deny'))
            self.assertTrue(app.journal.verify()['ok'])
            app.settings.set('inspectors',{'enabled':False})
            with self.assertRaises(HumanError):app.inspector
        finally:await app.close()

    @async_test
    async def test_core_paragraph_updates_prompt_and_limit(self):
        app=App(self.tmp/'data')
        try:
            app.selfdir.write('MEMORY.md','# Память\n\n- Кисель холодный.\n')
            app.index.sync(None)
            hit=app.index.search('кисель',kinds=('core',))[0]
            await app.inspector.change([hit.id],'edit','- Кисель тёплый.','Хозяин уточнил')
            self.assertIn('тёплый',app.selfdir.read('MEMORY.md'))
            hit=app.index.search('кисель',kinds=('core',))[0]
            app.settings.set('memory',{'core_limit':500})
            before=app.selfdir.read('MEMORY.md')
            with self.assertRaises(HumanError):await app.inspector.change([hit.id],'edit','я'*600,'Проверка лимита')
            self.assertEqual(app.selfdir.read('MEMORY.md'),before)
            await app.inspector.change([hit.id],'forget','','Сведения больше не нужны')
            self.assertNotIn('Кисель',app.selfdir.read('MEMORY.md'))
        finally:await app.close()
