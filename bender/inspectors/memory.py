"""Провенанс, использования recall и правки хозяина с объяснением и историей."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import time

from ..core.errors import HumanError
from ..core.fsutil import atomic_write_text
from ..core.pathutil import within, canonical
from ..memory.index import Chunk
from ..tools.registry import ToolContext

SCHEMA=[
    'CREATE TABLE IF NOT EXISTS mem_links (id INTEGER PRIMARY KEY, th TEXT NOT NULL, session TEXT, turn TEXT, call_id TEXT, channel TEXT, tainted INTEGER NOT NULL DEFAULT 0, at REAL NOT NULL)',
    'CREATE INDEX IF NOT EXISTS mem_links_text ON mem_links(th,at)',
    'CREATE TABLE IF NOT EXISTS mem_overrides (doc_key TEXT NOT NULL, ref TEXT NOT NULL, original_hash TEXT NOT NULL, text TEXT NOT NULL, hidden INTEGER NOT NULL DEFAULT 0, tainted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(doc_key,ref,original_hash))',
    'CREATE TABLE IF NOT EXISTS mem_edit_history (id INTEGER PRIMARY KEY, at REAL NOT NULL, action TEXT NOT NULL, reason TEXT NOT NULL, data TEXT NOT NULL)',
]


def th(text):
    return hashlib.sha1(text.encode()).hexdigest()


def ensure(store):
    store.migrate('memory_inspector',1,SCHEMA)


def overlays(store,doc_id,chunks):
    """Изменения разговоров/фактов живут поверх источника и переживают переиндексацию."""
    if not store.one("SELECT 1 FROM sqlite_master WHERE name='mem_overrides'"):
        return chunks
    rows=store.q('SELECT * FROM mem_overrides WHERE doc_key=(SELECT key FROM mem_docs WHERE id=?)',(doc_id,))
    changes={(r['ref'],r['original_hash']):r for r in rows}
    result=[]
    for c in chunks:
        change=changes.get((c.ref,c.hash))
        if not change and c.ref.startswith('fact:'):
            change=next((r for r in rows if r['ref']==c.ref),None)
        if change:
            if change['hidden']:continue
            c=Chunk(change['text'],c.ref,c.ts,'untrusted' if change['tainted'] else c.provenance,c.importance,c.triggers)
        result.append(c)
    return result


def record(agent_ctx,hits,query,ctx):
    if not agent_ctx.settings.get('inspectors','enabled',True) or not agent_ctx.settings.get('inspectors','track_recall',True):return
    ensure(agent_ctx.store)
    agent_ctx.index.overrides=True
    turn=agent_ctx.mind.turns.get(ctx.session_id)
    call=agent_ctx.store.one('SELECT id FROM calls WHERE session_id=? ORDER BY created_at DESC LIMIT 1',(ctx.session_id,))
    with agent_ctx.store.tx() as db:
        for hit in hits[:5]:
            if hit.kind!='core':
                db.execute('INSERT INTO mem_links(th,session,turn,call_id,channel,tainted,at) VALUES(?,?,?,?,?,?,?)',(th(hit.text),ctx.session_id,getattr(turn,'first_id',None),call['id'] if call else None,getattr(turn,'channel',None),int(ctx.tainted or hit.provenance=='untrusted'),time.time()))


class Inspector:
    def __init__(self,agent_ctx):
        self.app=agent_ctx
        self.require()
        ensure(agent_ctx.store)
        agent_ctx.index.overrides=True

    def require(self):
        if not self.app.settings.get('inspectors','enabled',True):
            raise HumanError('inspector_off','Инспекторы выключены','Включи их в настройках.')

    def row(self,ident):
        self.require()
        try:ident=int(ident)
        except (TypeError,ValueError):raise HumanError('memory_id','Некорректный номер записи','Выбери запись из поиска.') from None
        row=self.app.store.one('SELECT c.*,d.key AS doc_key,d.path,d.title FROM mem_chunks c JOIN mem_docs d ON d.id=c.doc_id WHERE c.id=?',(ident,))
        if not row:raise HumanError('memory_missing','Запись больше не в индексе','Обнови поиск или переиндексируй память.')
        return dict(row)

    def fragment(self,row):
        if row['kind'] not in ('core','diary') or not row.get('path'):return None
        path=canonical(row['path'])
        if not (within(path,self.app.dirs.self_dir) or within(path,self.app.dirs.diary)) or path.suffix!='.md':
            raise HumanError('memory_path','Источник за пределами памяти','Перестрой индекс.')
        raw=path.read_text(encoding='utf-8')
        lines=raw.splitlines(keepends=True)
        match=re.fullmatch(r'L(\d+)(?:\+\d+)?',row['ref'])
        if not match:raise HumanError('memory_ref','Нет строки источника','Открой файл личности или дневник.')
        start=int(match.group(1))-1
        if not 0<=start<len(lines):raise HumanError('memory_stale','Источник изменился','Обнови индекс и выбери запись заново.')
        end=start+1
        while end<len(lines):
            line=lines[end].strip()
            if not line or line.startswith(('#','<!--','- ','* ')) or re.match(r'\d+[.)]\s',line):break
            end+=1
        while start>0 and lines[start-1].strip().startswith('<!--') and lines[start-1].strip().endswith('-->'):
            start-=1
        return {'path':path,'raw':raw,'start':start,'end':end,'text':''.join(lines[start:end]),'hash':hashlib.sha256(raw.encode()).hexdigest()}

    def detail(self,ident):
        row=self.row(ident)
        fragment=self.fragment(row)
        hashed=th(row['text'])
        usage=self.app.store.one('SELECT hits,first,last,queries FROM mem_usage WHERE th=?',(hashed,))
        origin=None
        if row['kind']=='session':
            ref=row['ref'].split('+')[0]
            source=self.app.store.one('SELECT id,session_id,created_at,meta FROM messages WHERE id=?',(ref,))
            if source:origin={**dict(source),'meta':json.loads(source['meta'])}
        fact=self.app.store.one('SELECT * FROM facts WHERE id=?',(int(row['ref'][5:]),)) if row['kind']=='fact' and row['ref'].startswith('fact:') else None
        links=[dict(r) for r in self.app.store.q('SELECT * FROM mem_links WHERE th=? ORDER BY at DESC LIMIT 200',(hashed,))]
        return {**row,'editable_text':json.dumps({k:fact[k] for k in ('subject','relation','object')},ensure_ascii=False,indent=2) if fact else fragment['text'] if fragment else row['text'],'source_hash':fragment['hash'] if fragment else row['hash'],
                'origin':origin,'usage':{**dict(usage),'queries':json.loads(usage['queries'])} if usage else {'hits':0,'last':None,'queries':[]},'uses':links,
                'history':[dict(r) for r in self.app.store.q('SELECT * FROM mem_edit_history WHERE data LIKE ? ORDER BY id DESC LIMIT 50',('%"doc_key": '+json.dumps(row['doc_key'],ensure_ascii=False)+'%',))]}

    async def change(self,ids,action,text,reason,expected=None):
        self.require()
        if action not in ('edit','forget','merge') or not isinstance(ids,list) or not 1<=len(ids)<=20 or len(set(ids))!=len(ids):
            raise HumanError('memory_action','Нужно выбрать от 1 до 20 разных записей','Для слияния выбери хотя бы две.')
        if action=='merge' and len(ids)<2:raise HumanError('memory_merge','Для слияния нужны две записи','Отметь вторую запись в поиске.')
        if not isinstance(reason,str) or not 3<=len(reason)<=1000:raise HumanError('memory_reason','Объясни причину изменения','От 3 до 1000 знаков; объяснение останется в журнале.')
        if action!='forget' and (not isinstance(text,str) or not text.strip() or len(text)>12000):raise HumanError('memory_text','Нужен новый текст до 12 000 знаков','Проверь редактор.')
        rows=[self.row(i) for i in ids]
        fragments=[self.fragment(r) for r in rows]
        for row,fragment in zip(rows,fragments):
            version=expected.get(str(row['id'])) if isinstance(expected,dict) else expected if row==rows[0] else None
            if version and version!=(fragment['hash'] if fragment else row['hash']):
                raise HumanError('memory_stale','Запись изменилась после просмотра','Обнови инспектор перед правкой.')
        await ToolContext(self.app,actor='owner').check('memory.core',','.join(map(str,ids)),action+': '+reason,detail={'ids':ids,'text':text,'reason':reason})
        # После ожидания карточки проверяем исходники ещё раз.
        for fragment in fragments:
            if fragment and fragment['path'].read_text(encoding='utf-8')!=fragment['raw']:
                raise HumanError('memory_stale','Источник изменился во время подтверждения','Повтори просмотр.')
        tainted=any(r['provenance']=='untrusted' for r in rows)
        new_fact=None
        if rows[0]['kind']=='fact' and action!='forget':
            try:
                new_fact=json.loads(text)
                if not isinstance(new_fact,dict) or any(not isinstance(new_fact.get(k),str) or not 1<=len(new_fact[k].strip())<=2000 for k in ('subject','relation','object')):raise ValueError()
            except ValueError:
                raise HumanError('memory_fact','Факту нужны subject, relation и object в JSON','Правь три поля в редакторе; история прежнего факта останется.') from None
        plans={}
        changes=[]
        for n,(row,fragment) in enumerate(zip(rows,fragments)):
            hidden=action=='forget' or (action=='merge' and n>0)
            new='' if hidden else text
            if tainted and new and fragment:
                new='<!-- source:untrusted -->\n'+new
            if fragment:
                plan=plans.setdefault(str(fragment['path']),{'raw':fragment['raw'],'spans':[]})
                for start,end,_ in plan['spans']:
                    if max(start,fragment['start'])<min(end,fragment['end']):
                        raise HumanError('memory_overlap','Выбраны части одного абзаца','Оставь одну запись: редактор меняет весь исходный абзац.')
                plan['spans'].append((fragment['start'],fragment['end'],new.rstrip()+'\n' if new else ''))
            if row['kind']=='fact':hidden=True;new=''
            changes.append({'doc_key':row['doc_key'],'ref':row['ref'],'original_hash':row['hash'],'text':new,'hidden':int(hidden),'tainted':int(tainted),'old':row['text'],'file':bool(fragment)})
        for path,plan in plans.items():
            lines=plan['raw'].splitlines(keepends=True)
            for start,end,new in sorted(plan['spans'],reverse=True):lines[start:end]=[new]
            value=''.join(lines)
            if Path(path).name in ('MEMORY.md','USER.md'):
                key='core_limit' if Path(path).name=='MEMORY.md' else 'user_limit'
                limit=self.app.settings.get('memory',key,8000)
                if len(value)>limit:raise HumanError('memory_limit','Ядро памяти переполнится','Сократи новый текст.')
            plan['new']=value
        saved=[]
        try:
            for path,plan in plans.items():
                if self.app.settings.get('guard','checkpoints',True):self.app.guard.checkpoints.take(Path(path),'перед правкой инспектора: '+reason)
                atomic_write_text(path,plan['new']);saved.append(path)
            with self.app.store.tx() as db:
                for row,change in zip(rows,changes):
                    if change['file']:
                        db.execute("UPDATE mem_docs SET stamp='' WHERE id=?",(row['doc_id'],))
                    else:
                        prior=db.execute('SELECT original_hash FROM mem_overrides WHERE doc_key=? AND ref=?',(row['doc_key'],row['ref'])).fetchone()
                        if prior:change['original_hash']=prior['original_hash']
                        db.execute('INSERT OR REPLACE INTO mem_overrides(doc_key,ref,original_hash,text,hidden,tainted) VALUES(?,?,?,?,?,?)',tuple(change[k] for k in ('doc_key','ref','original_hash','text','hidden','tainted')))
                        chunks=[] if change['hidden'] else [Chunk(change['text'],row['ref'],row['ts'],'untrusted' if tainted else row['provenance'],row['importance'])]
                        # Частичное обновление без удаления остальных записей документа.
                        db.execute('DELETE FROM mem_chunks WHERE id=?',(row['id'],))
                        if self.app.index.fts:
                            db.execute('DELETE FROM mem_fts WHERE rowid=?',(row['id'],));db.execute('DELETE FROM mem_tri WHERE rowid=?',(row['id'],))
                        # Вставку делаем после транзакции, общий индекс применит тот же override при rebuild.
                        change['chunks']=chunks
                    if row['kind']=='fact' and row['ref'].startswith('fact:'):
                        db.execute('UPDATE facts SET valid_to=COALESCE(valid_to,?) WHERE id=?',(time.time(),int(row['ref'][5:])))
                    db.execute('UPDATE mem_usage SET promoted=? WHERE th=?',(time.time(),th(row['text'])))
                db.execute('INSERT INTO mem_edit_history(at,action,reason,data) VALUES(?,?,?,?)',(time.time(),action,reason,json.dumps([{k:v for k,v in c.items() if k!='chunks'} for c in changes],ensure_ascii=False)))
        except BaseException:
            for path in saved:atomic_write_text(path,plans[path]['raw'])
            raise
        for row,change in zip(rows,changes):
            if not change['file'] and change.get('chunks'):
                self.app.index._replace_chunks(row['doc_id'],row['kind'],change['chunks'],append=True)
        if new_fact:
            self.app.facts.add(new_fact['subject'],new_fact['relation'],new_fact['object'],provenance='untrusted' if tainted else rows[0]['provenance'],source='Инспектор: '+reason)
        elif any(r['kind']=='fact' for r in rows):
            self.app.facts.reindex()
        self.app.index.sync_files()
        self.app.journal.write('memory','Инспектор: '+action,actor='owner',data={'ids':ids,'reason':reason,'action':action})
        self.app.bus.publish({'type':'memory'})
        return {'ok':True,'action':action,'reason':reason,'changed':len(ids)}
