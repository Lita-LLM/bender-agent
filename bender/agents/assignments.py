"""Поручения между агентами стаи: письмо с номером, статусом и сроком; решает и видит хозяин."""
from __future__ import annotations

import json
import time
from ..core.errors import HumanError
from ..core.ids import new_id

SCHEMA = ["""CREATE TABLE IF NOT EXISTS flock_assignments (
 id TEXT PRIMARY KEY, creator TEXT NOT NULL, assignee TEXT NOT NULL, title TEXT NOT NULL, text TEXT NOT NULL DEFAULT '',
 state TEXT NOT NULL DEFAULT 'open', created REAL NOT NULL, updated REAL NOT NULL, due REAL NOT NULL DEFAULT 0,
 note TEXT NOT NULL DEFAULT '', letter TEXT NOT NULL DEFAULT '')""",
 "CREATE INDEX IF NOT EXISTS flock_assignments_state ON flock_assignments(state,assignee,creator)"]
STATES={'open':'Ждёт','taken':'В работе','done':'Готово','refused':'Отказ','cancelled':'Отменено'}
ACTIVE=('open','taken')
# что может сделать исполнитель; создатель может только отменить
MOVES={'taken':('open',),'done':('open','taken'),'refused':('open','taken')}


class Assignments:
    def __init__(self, manager):
        self.manager,self.host=manager,manager.host
        self.host.store.migrate('flock_assignments',1,SCHEMA)

    def get(self, ident):
        row=self.host.store.one('SELECT * FROM flock_assignments WHERE id=? OR id LIKE ?',(ident,str(ident)+'%')) if ident else None
        if not row:raise HumanError('assignment','Такого поручения нет','Список — тул assignments.',status=404)
        return dict(row)

    def name(self, agent):return self.manager.record(agent)['name'] if agent else ''

    def card(self, row):
        return {**row,'creator_name':self.name(row['creator']),'assignee_name':self.name(row['assignee']),'label':STATES.get(row['state'],row['state']),
                'overdue':bool(row['due'] and row['due']<time.time() and row['state'] in ACTIVE)}

    def listing(self, agent=None, *, active=True, limit=100):
        sql='SELECT * FROM flock_assignments';where=[];args=[]
        if agent:where.append('(creator=? OR assignee=?)');args+=[agent,agent]
        if active:where.append("state IN ('open','taken')")
        rows=self.host.store.q(sql+(' WHERE '+' AND '.join(where) if where else '')+' ORDER BY created DESC LIMIT ?',(*args,min(500,max(1,int(limit)))))
        return [self.card(dict(r)) for r in rows]

    def counts(self, agent):
        """Две цифры для строки в запросе агента: сколько поручений ждёт его и сколько он выдал и ещё не закрыто."""
        one=lambda col:self.host.store.one(f"SELECT count(*) AS n FROM flock_assignments WHERE {col}=? AND state IN ('open','taken')",(agent,))['n']
        return one('assignee'),one('creator')

    async def create(self, ctx, target, title, text, hours=0):
        target=self.manager.resolve(target);title=' '.join(str(title or '').split());text=str(text or '').strip()
        if not title or len(title)>120:raise HumanError('assignment','Нужно название до 120 знаков','Назови поручение коротко.')
        if not text or len(text)>20000:raise HumanError('assignment','Нужен текст поручения до 20 000 знаков','Опиши, что нужно сделать и что считать готовым.')
        hours=float(hours or 0)
        if not 0<=hours<=24*90:raise HumanError('assignment','Срок — от 0 до 2160 часов','0 — без срока.')
        ident=new_id()[:8];due=time.time()+hours*3600 if hours else 0
        letter=f"[Поручение №{ident}: {title}]\n{text}\n\n"+(f"Срок: {time.strftime('%d.%m %H:%M',time.localtime(due))}. " if due else '')+\
            f"Когда возьмёшься, закончишь или откажешься — тул assignment_update(id=\"{ident}\", state=taken|done|refused, note=...). Хозяин в этой переписке не участвует."
        sent=await self.manager.correspondence.send(ctx,target,letter,wait=False)
        now=time.time()
        self.host.store.x('INSERT INTO flock_assignments(id,creator,assignee,title,text,state,created,updated,due,letter) VALUES(?,?,?,?,?,?,?,?,?,?)',
                          (ident,ctx.app.agent_id,target,title,text,'open',now,now,due,str(sent.get('id') or '')))
        self.host.journal.write('agents','Поручение '+ident+': '+self.name(ctx.app.agent_id)+' → '+self.name(target),actor='agent',data={'id':ident,'title':title[:120]})
        self.host.bus.publish({'type':'assignments','agent_id':'*','id':ident})
        return ident,sent

    def update(self, who, ident, state, note=''):
        row=self.get(ident);note=' '.join(str(note or '').split())[:1000]
        if row['state'] not in ACTIVE:raise HumanError('assignment','Поручение уже закрыто: '+STATES[row['state']],'Новое поручение — тул assign.')
        if state=='cancelled':
            if who!='owner' and who!=row['creator']:raise HumanError('assignment','Отменить может тот, кто выдал поручение','Исполнитель может отказаться: state=refused.')
        elif state in MOVES:
            if who!=row['assignee']:raise HumanError('assignment','Менять статус может исполнитель','Создатель поручения может его отменить.')
            if row['state'] not in MOVES[state]:raise HumanError('assignment','Из «'+STATES[row['state']]+'» в «'+STATES[state]+'» нельзя','Статусы: taken, done, refused.')
        else:raise HumanError('assignment','Статус: taken, done, refused или cancelled','Выбери один из них.')
        if state in ('refused',) and not note:raise HumanError('assignment','При отказе нужна причина','Напиши в note, почему не берёшься: создатель увидит.')
        self.host.store.x('UPDATE flock_assignments SET state=?,note=?,updated=? WHERE id=?',(state,note or row['note'],time.time(),row['id']))
        row=self.get(row['id']);self.tell(row,state,note,who)
        self.host.bus.publish({'type':'assignments','agent_id':'*','id':row['id']})
        return self.card(row)

    def tell(self, row, state, note, who):
        """Второй стороне — служебная строка без нового хода (она увидит её в следующем разговоре); хозяину — только итог."""
        verb={'taken':'взял в работу','done':'выполнил','refused':'отказался от','cancelled':'отменил'}[state]
        actor='Хозяин' if who=='owner' else self.name(who)
        text=f"📌 {actor} {verb} поручение №{row['id']} «{row['title']}»"+(': '+note if note else '.')
        target=row['creator'] if state!='cancelled' else row['assignee']
        try:
            ctx=self.manager.get(target);ctx.mind.observe(ctx.main_session(),text,channel='agent',meta={'display':text,'assignment':row['id']})
        except Exception as exc:  # noqa: BLE001 — статус уже записан
            self.host.journal.write('agents','Не удалось сообщить о статусе поручения',data={'error':type(exc).__name__})
        if state in ('done','refused') and target!='main':
            self.host.mind.observe(self.host.main_session(),text,channel='agent',meta={'display':text,'assignment':row['id']})
