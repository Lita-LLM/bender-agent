"""Одна SQLite: неизвестное и прежний интерфейс сохранены, память/FTS раздельны."""
from bender.core.store import Store
from bender.core.paths import DataDir, AgentDataDir
from bender.agents.store import NamespaceStore
from bender.memory.index import MemoryIndex
from tests.helpers import TempDirCase


class AgentStoreTest(TempDirCase):
    def test_scoped_transactions_literals_upsert_and_previous_interface(self):
        root=Store(self.tmp/'bender.db')
        try:
            first=NamespaceStore(root,'first'); second=NamespaceStore(root,'second')
            for db,text in ((root,'main'),(first,'messages sessions mem_chunks'),(second,'другое')):
                sid=db.new_session('main',text,sid='same-id')
                db.add_message(sid,{'role':'user','content':text})
                db.meta_set('same','value');db.meta_set('same',text)
            self.assertEqual(len(root.sessions()),1)
            self.assertEqual(first.messages('same-id')[0]['content'],'messages sessions mem_chunks')
            self.assertEqual(second.messages('same-id')[0]['content'],'другое')
            self.assertEqual(first.meta_get('same'),'messages sessions mem_chunks')
            self.assertEqual(first.session('same-id')['agent_id'],'first')
            first.close();self.assertEqual(root.sessions()[0]['title'],'main')
            self.assertEqual(NamespaceStore(root,'first').messages('same-id'),first.messages('same-id'))
            with self.assertRaises(ValueError):
                with first.tx() as conn:
                    conn.execute("INSERT INTO meta VALUES('rollback','1','first')")
                    raise ValueError()
            self.assertIsNone(first.meta_get('rollback'))
        finally:root.close()

    def test_fts_and_files_do_not_cross_contexts(self):
        root=Store(self.tmp/'bender.db');dirs=DataDir(self.tmp/'data');dirs.ensure()
        try:
            first=NamespaceStore(root,'first');second=NamespaceStore(root,'second')
            for db,paths,text in ((root,dirs,'ОСНОВНОЙ-ПРИВАТНЫЙ'),(first,AgentDataDir(dirs,'first'),'ПЕРВЫЙ-ПРИВАТНЫЙ'),(second,AgentDataDir(dirs,'second'),'ВТОРОЙ-ПРИВАТНЫЙ')):
                paths.ensure();(paths.diary/'test.md').write_text(text)
                index=MemoryIndex(db,paths);index.sync_files()
                self.assertTrue(index.search(text))
                if db is first:self.assertFalse(index.search('ОСНОВНОЙ-ПРИВАТНЫЙ'))
            self.assertEqual(root.one('PRAGMA quick_check')[0],'ok')
        finally:root.close()
