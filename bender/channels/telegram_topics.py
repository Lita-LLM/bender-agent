"""Темы лички: явный адрес каждого запроса и привязка к отдельной сессии.

Протокол — telegram-bot-api-10.3.md: create/editForumTopic, message_thread_id.
Адрес наследует int ради прежнего API каналов; thread существует только в памяти,
на HTTP-границе разворачивается в два поля. Общего «текущего thread» нет.
"""
from __future__ import annotations
import asyncio
import time
from ..core.errors import HumanError
from ..tools.registry import ToolContext

THREAD_METHODS={'sendMessage','sendRichMessage','sendMessageDraft','sendRichMessageDraft','sendChatAction',
                'sendPhoto','sendDocument','sendVideo','sendAnimation','sendVoice','sendAudio','sendMediaGroup'}


class Address(int):
    def __new__(cls,chat,thread=None):
        value=int.__new__(cls,chat)
        value.thread=thread
        return value

    def route(self):return {'chat':int(self),'thread':self.thread}


class Topics:
    def __init__(self,tg):self.tg=tg;self.host=tg.app;self.locks={}

    def enabled(self,chat=None):
        owner=self.tg.owner() or {};private=owner.get('chat_id') or owner.get('user_id')
        return bool(self.tg.settings().get('topics',True) and (self.tg.me or {}).get('has_topics_enabled') and (chat is None or chat==private))

    def rows(self):return self.host.store.meta_get('telegram:topics',{})

    @staticmethod
    def key(chat,thread):return str(int(chat))+':'+str(thread)

    def lookup(self,chat,thread):
        rows=self.rows();row=rows.get(self.key(chat,thread))
        for _ in range(8):
            if not row or not row.get('replacement'):return row
            row=rows.get(self.key(chat,row['replacement']))
        raise HumanError('telegram_topic','Повреждена цепочка тем','Создай новый разговор командой /new.')

    def address(self,chat,thread=None):
        if not self.enabled(chat):return Address(chat)
        row=self.lookup(chat,thread) if thread else None
        return Address(chat,row['thread'] if row else thread)

    def for_session(self,ctx,sid):
        if not self.enabled():return None
        for row in reversed(list(self.rows().values())):
            if row.get('agent')==ctx.agent_id and row.get('session')==sid and not row.get('replacement'):
                return self.address(row['chat'],row['thread'])
        return None

    def name(self,ctx,sid):
        row=ctx.store.session(sid) or {}
        return ((ctx.selfdir.identity().get('name') or ctx.agent_id)+' · '+str(row.get('title') or 'Новый разговор'))[:128]

    def bind(self,chat,thread,ctx,sid,name):
        rows=self.rows();key=self.key(chat,thread)
        rows[key]={**rows.get(key,{}),'chat':int(chat),'thread':thread,'agent':ctx.agent_id,'session':sid,'name':name}
        self.host.store.meta_set('telegram:topics',rows)
        self.host.bus.publish({'type':'channel','channel':'telegram','agent_id':'*','state':self.tg.state})
        from .telegram_routes import bind
        bind(self.tg,int(chat),ctx,sid)
        ctx.store.update_session(sid,meta={'home_channel':'telegram'})
        return Address(chat,thread)

    async def create(self,ctx,chat,sid):
        await ToolContext(ctx,actor='owner',session_id=sid).check('settings','telegram:topics','создать тему разговора хозяина')
        name=self.name(ctx,sid)
        result=await self.tg.api('createForumTopic',{'chat_id':int(chat),'name':name})
        thread=(result or {}).get('message_thread_id')
        if not isinstance(thread,int) or isinstance(thread,bool) or thread<=0:
            raise HumanError('telegram_topic','Telegram не вернул номер новой темы','Повтори /new; созданную сессию можно открыть в пульте.')
        return self.bind(chat,thread,ctx,sid,name)

    async def open(self,ctx,chat,sid):
        existing=self.for_session(ctx,sid)
        if existing:return existing
        return await self.create(ctx,chat,sid)

    async def rename(self,ctx,sid):
        address=self.for_session(ctx,sid)
        if not address:return
        row=self.lookup(address,address.thread);name=self.name(ctx,sid)
        if row['name']==name:return
        await ToolContext(ctx,actor='owner',session_id=sid).check('settings','telegram:topics','обновить название темы по заголовку разговора')
        await self.tg.api('editForumTopic',{'chat_id':int(address),'message_thread_id':address.thread,'name':name})
        rows=self.rows();rows[self.key(address,address.thread)]={**row,'name':name};self.host.store.meta_set('telegram:topics',rows)

    async def incoming(self,msg):
        chat=(msg.get('chat') or {}).get('id')
        if not self.enabled(chat):return Address(chat)
        thread=msg.get('message_thread_id')
        if thread is None and msg.get('is_topic_message'):
            # При потерянном номере используем только известный ответ, не «последнюю тему».
            reply=msg.get('reply_to_message') or {}
            route=self.host.store.meta_get('telegram:message_routes',{}).get(str(chat)+':'+str(reply.get('message_id')), {})
            if route:
                try:known=self.for_session(self.host.agents.get(route['agent']),route['session'])
                except HumanError:known=None
                if known:thread=known.thread
            if thread is None:
                ctx,sid=self.tg.selected(int(chat))
                options=[(row['name'],{'thread':row['thread'],'message':msg}) for row in self.rows().values() if row['chat']==chat and not row.get('replacement')]
                options.append(('Общий поток',{'thread':None,'message':msg}))
                await self.tg.commands.pick(ctx,int(chat),sid,'topic_route','Telegram не указал номер темы. Куда передать это сообщение?',options)
                self.host.journal.write('channel','Telegram: потерян номер входящей темы; запрошен выбор хозяина')
                return None
        if thread is None:return Address(chat)
        if not isinstance(thread,int) or isinstance(thread,bool) or thread<=0:
            raise HumanError('telegram_topic','Неверный номер темы Telegram','Пришли сообщение ещё раз или создай /new.')
        binding=self.lookup(chat,thread)
        if not binding:
            ctx,_=self.tg.selected(int(chat))
            title=str((msg.get('forum_topic_created') or {}).get('name') or 'Тема Telegram '+str(thread))[:128]
            await ToolContext(ctx,actor='owner').check('settings','telegram:topics','привязать тему хозяина к отдельному разговору')
            sid=ctx.store.new_session('chat',title)
            self.bind(chat,thread,ctx,sid,title)
            await self.rename(ctx,sid)
        if msg.get('forum_topic_created'):return None
        return self.address(chat,thread)

    def repair_decision(self,ctx,action):
        from ..guard.policy import Decision
        try:chat,thread=(int(value) for value in action.target.split(':',1))
        except (TypeError,ValueError):return Decision('deny','Неверная тема для восстановления.','telegram-topic',False)
        row=self.lookup(chat,thread)
        rule=ctx.guard.policy._rule_for(action)
        allowed=action.actor=='system' and action.tool=='telegram_topic_repair' and self.enabled(chat) and bool(row) and not(rule and rule.get('verdict')=='deny')
        return Decision('allow' if allowed else 'deny','Служба может восстановить только прежнюю привязанную тему хозяина.','telegram-topic-repair',False)

    async def repair(self,address):
        key=self.key(address,address.thread)
        lock=self.locks.setdefault(key,asyncio.Lock())
        try:
            async with lock:
                old=self.lookup(address,address.thread)
                if not old:raise HumanError('telegram_topic','Неизвестная тема не восстанавливается','Открой /new в личке бота.')
                if old['thread']!=address.thread:return Address(address,old['thread'])
                if time.time()-old.get('repair_at',0)<60:
                    raise HumanError('telegram_topic','Telegram снова не находит восстановленную тему','Повторная отправка остановлена. Проверь темы у @BotFather и повтори сообщение позже.')
                ctx=self.host.agents.get(old['agent'])
                session=ctx.store.session(old['session'])
                if not session or session['meta'].get('archived'):raise HumanError('telegram_topic','Разговор темы закрыт или удалён','Создай /new.')
                check=ToolContext(ctx,actor='system',unattended=True,tool='telegram_topic_repair')
                await check.check('telegram.topic',key,'восстановить привязанную тему после thread not found')
                result=await self.tg.api('createForumTopic',{'chat_id':int(address),'name':self.name(ctx,old['session'])})
                thread=(result or {}).get('message_thread_id')
                if not isinstance(thread,int) or isinstance(thread,bool) or thread<=0 or thread==old['thread']:
                    raise HumanError('telegram_topic','Telegram не создал замену темы','Повтори /new вручную.')
                rows=self.rows();rows[key]={**old,'replacement':thread}
                rows[self.key(address,thread)]={**old,'thread':thread,'repair_at':time.time(),'name':self.name(ctx,old['session'])}
                self.host.store.meta_set('telegram:topics',rows)
                self.host.journal.write('channel','Telegram: тема пересоздана после thread not found',data={'old':old['thread'],'new':thread,'agent_id':ctx.agent_id,'session':old['session']})
                return Address(address,thread)
        finally:
            if not lock.locked():self.locks.pop(key,None)
