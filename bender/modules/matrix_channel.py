"""Канал хозяина поверх отдельного Matrix-модуля: привязка, права и доставка в стаю."""
import asyncio
import base64
import hmac
import json
import secrets
import time
from urllib.parse import urlsplit

from ..channels import media
from ..core.errors import HumanError
from ..mcp.manager import digest
from ..tools.registry import ToolContext
from ..tools.web import untrusted
from .matrix_transport import Transport,SECRET,failed,homeserver


class Matrix:
    def __init__(self,app):
        self.app=app.host;self.transport=Transport(app);self.task=None;self.sender=None;self.listener=None
        self.outbox=asyncio.Queue(maxsize=32);self.state='off';self.notice='';self.code=None;self.restarting=None;self.runtime=False;self.stopping=False
        self.app.bus.listen(self.answer)

    def settings(self):return self.app.settings.group('matrix')

    def identity(self):
        cfg=self.settings()
        return digest({'homeserver':cfg['homeserver'],'room':cfg['room_id'],'token':(self.app.secrets.get(SECRET),self.app.secrets.allow(SECRET))})

    def owner(self):
        owner=self.app.store.meta_get('matrix:owner',{})
        return owner if owner.get('binding')==self.identity() else None

    def public(self):
        cfg=self.settings();ready=False;reason=''
        try:self.transport.setup();ready=True
        except (HumanError,OSError,ValueError) as error:reason=str(error)
        code=self.code if self.code and self.code['expires']>time.monotonic() else None
        return {'settings':cfg,'has_token':bool(self.app.secrets.get(SECRET)),'ready':ready,'reason':reason,'state':self.state,'notice':self.notice,
                'owner':self.owner(),'code':code['value'] if code else None,'pair_ready':self.app.store.meta_get('matrix:cursor',{}).get('binding')==self.identity()}

    async def configure(self,changes,token=None):
        # Вызывается только защищённым owner API, не инструментом агента.
        cfg={**self.settings(),**{key:value for key,value in changes.items() if key in ('homeserver','room_id','agent_id')}}
        cfg['homeserver']=homeserver(cfg['homeserver'])
        if not isinstance(cfg['room_id'],str) or not cfg['room_id'].startswith('!') or len(cfg['room_id'])>4096 or any(ord(c)<33 for c in cfg['room_id']):raise failed('Укажи id отдельной комнаты !…:сервер.')
        self.app.agents.get(cfg['agent_id'])
        if token is not None and not isinstance(token,str):raise failed('Нужен текст access_token Matrix.')
        if token:
            if not isinstance(token,str) or len(token)>32768 or any(ord(c)<33 for c in token):raise failed('Нужен access_token без пробелов/управляющих символов.')
        await self.stop()
        if token:
            self.app.secrets.set(SECRET,token,note='Matrix: токен бота',allow={'hosts':[urlsplit(cfg['homeserver']).hostname],'tools':[]})
        self.app.settings.set('matrix',{**{key:cfg[key] for key in ('homeserver','room_id','agent_id')},'enabled':False},by='owner');self.code=None
        self.event();return self.public()

    async def connect(self):
        cfg=self.settings();stamp=self.transport.stamp();ctx=ToolContext(self.app,actor='owner',tool='matrix_connect')
        await ctx.check('net.send',cfg['homeserver'],'войти ботом в выбранную комнату Matrix',detail={'room':cfg['room_id']})
        self.transport.validate(stamp,enabled=False)
        await self.transport.call('join',enabled=False)
        await ctx.check('net.read',cfg['homeserver'],'проверить комнату Matrix')
        info=await self.transport.call('status',enabled=False);self.transport.validate(stamp,enabled=False);self.require_room(info,paired=False)
        self.app.settings.set('matrix',{'enabled':True},by='owner')
        self.start();return self.public()

    def pair(self):
        if not self.settings()['enabled'] or self.state not in ('ok','unpaired') or not self.public()['pair_ready']:raise failed('Сначала подключи Matrix и дождись первого успешного /sync; затем получи код.')
        self.code={'value':secrets.token_hex(4),'expires':time.monotonic()+600,'attempts':0,'binding':self.identity()};self.event();return self.public()

    def event(self):self.app.bus.publish({'type':'channel','channel':'matrix','agent_id':'*'})

    def require_room(self,info,*,paired=True):
        if info.get('encrypted'):raise failed('Matrix-комната шифрована. Для этого модуля нужна отдельная комната без E2EE.')
        members=info.get('members');bot=info.get('bot');owner=self.owner()
        if not isinstance(members,list) or len(members)!=2 or bot not in members:raise failed('В комнате должны состоять ровно бот и хозяин. Посторонний участник блокирует канал.')
        if paired and (not owner or set(members)!={bot,owner['user_id']}):raise failed('Привязка хозяина устарела или участники комнаты изменились. Проверь комнату и привяжи заново.')

    def start(self):
        self.runtime=True
        if getattr(self.app,'_closed',False) or self.app.safe or not self.settings()['enabled'] or self.task and not self.task.done():return
        self.stopping=False
        self.task=asyncio.create_task(self.run(),name='matrix-sync')
        self.sender=asyncio.create_task(self.deliver(),name='matrix-delivery')

    def reconfigure(self):
        if getattr(self.app,'_closed',False) or not self.runtime:return
        if self.restarting and not self.restarting.done():return
        async def restart():
            await self.stop();self.start()
        try:self.restarting=asyncio.get_running_loop().create_task(restart(),name='matrix-reconfigure')
        except RuntimeError:pass

    async def stop(self):
        # Отмена может совпасть с завершением внутреннего wait_for. Даже если она
        # поглощена обменом, отправитель не должен снова встать в пустую очередь.
        self.stopping=True
        tasks=[task for task in (self.task,self.sender) if task and task is not asyncio.current_task()]
        for task in tasks:task.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)
        self.task=None;self.sender=None
        await self.transport.close()
        while not self.outbox.empty():self.outbox.get_nowait()
        self.state='off';self.code=None;self.event()

    async def run(self):
        try:
            while not self.stopping and self.settings()['enabled'] and not self.app.safe:
                started=time.monotonic()
                try:
                    await self.poll_once()
                    self.state='ok' if self.owner() else 'unpaired';self.notice=''
                except asyncio.CancelledError:raise
                except Exception as error:
                    self.state='error';self.notice=self.app.guard.scrub(str(error)) if isinstance(error,HumanError) else 'Ошибка Matrix. Проверь сервер, модуль и настройки.'
                if self.stopping:break
                self.event()
                await asyncio.sleep(max(0,self.settings()['poll_seconds']-(time.monotonic()-started)))
        finally:self.state='off';self.event()

    async def poll_once(self):
        cfg=self.settings();stamp=self.transport.stamp();identity=self.identity()
        ctx=ToolContext(self.app,actor='owner',tool='matrix_receive')
        await ctx.check('net.read',cfg['homeserver'],'получить сообщения Matrix')
        info=await self.transport.call('status');self.require_room(info,paired=bool(self.owner()))
        cursor=self.app.store.meta_get('matrix:cursor',{});since=cursor.get('since') if cursor.get('binding')==identity else None
        data=await self.transport.call('sync',{'since':since} if since else {})
        self.transport.validate(stamp)
        next_batch=data.get('next_batch')
        if not isinstance(next_batch,str) or not next_batch or len(next_batch)>4096:raise failed('Matrix не вернул next_batch; история не перечитывается как новое поручение.')
        # После long-poll участники/шифрование могли измениться.
        info=await self.transport.call('status');self.require_room(info,paired=bool(self.owner()))
        room=(data.get('rooms') or {}).get('join',{}).get(cfg['room_id'],{})
        events=(room.get('timeline') or {}).get('events',[])
        if not isinstance(events,list) or len(events)>100:raise failed('Слишком много событий Matrix за пакет.')
        if since:
            for event in events:
                self.transport.validate(stamp)
                await self.incoming(event,info)
        # Начальный timeline — история, не новые команды и не старые коды привязки.
        if cursor.get('binding')!=identity or cursor.get('since')!=next_batch:self.app.store.meta_set('matrix:cursor',{**cursor,'binding':identity,'since':next_batch})

    def selected(self):
        cfg=self.settings();owner=self.owner()
        if not owner:raise failed('Сначала привяжи хозяина кодом из пульта.')
        ctx=self.app.agents.get(cfg['agent_id']);key='matrix:session:'+digest({'room':cfg['room_id'],'owner':owner['user_id']})
        sid=ctx.store.meta_get(key)
        if not sid or not ctx.store.session(sid):
            sid=ctx.store.new_session('chat','Matrix',meta={'home_channel':'matrix','matrix_room':cfg['room_id']});ctx.store.meta_set(key,sid)
        return ctx,sid

    async def incoming(self,event,info):
        if not isinstance(event,dict) or event.get('sender')==info['bot']:return
        eid=event.get('event_id');sender=event.get('sender');content=event.get('content') or {}
        if not isinstance(eid,str) or not eid or len(eid)>4096 or not isinstance(content,dict):return
        stamp=self.transport.stamp()
        seen=self.app.store.meta_get('matrix:seen',[])
        if eid in seen:return
        # Отмечаем до последствий: потерянный ответ не создаёт повтор поручения.
        self.app.store.meta_set('matrix:seen',(seen+[eid])[-5000:])
        if event.get('type')=='m.room.encrypted':raise failed('Пришло шифрованное событие. Этот модуль не расшифровывает E2EE.')
        text=content.get('body','')
        if not isinstance(text,str):text=''
        owner=self.owner()
        if not owner:
            if event.get('type')!='m.room.message' or content.get('msgtype')!='m.text' or not text.startswith('/pair '):return
            if not self.code or self.code.get('binding')!=self.identity() or self.code['expires']<time.monotonic() or self.code['attempts']>=5:return
            self.code['attempts']+=1
            if not hmac.compare_digest(text[6:].strip().encode(),self.code['value'].encode()):return
            if sender not in info['members'] or sender==info['bot']:return
            self.app.store.meta_set('matrix:owner',{**self.app.store.meta_get('matrix:owner',{}),'user_id':sender,'room_id':self.settings()['room_id'],'binding':self.identity()});self.code=None
            ctx,sid=self.selected();await self.send(ToolContext(ctx,actor='owner',session_id=sid),'Привязал хозяина Matrix. Теперь можно писать здесь.');self.event();return
        if sender!=owner['user_id']:return
        ctx,sid=self.selected();ref=self.remember(ctx,eid,sid,text,'in')
        if event.get('type')=='m.reaction':
            relation=content.get('m.relates_to') or {};key=relation.get('key')
            if relation.get('rel_type')!='m.annotation' or not isinstance(key,str):return
            target=next((k for k,v in ctx.store.meta_get('matrix:refs',{}).items() if v['event_id']==relation.get('event_id') and v['session']==sid),None)
            if target:
                ctx.mind.observe(sid,untrusted('реакция хозяина Matrix','На #'+target+': '+key[:64]),channel='matrix',tainted=True,meta={'matrix_event':eid})
            return
        if event.get('type')!='m.room.message':return
        attachments=[];kind=content.get('msgtype')
        if kind in ('m.file','m.image','m.audio','m.video'):
            if content.get('file') or not isinstance(content.get('url'),str):raise failed('Вложение зашифровано или без MXC-адреса; пришли обычный файл в комнате без E2EE.')
            media.require(ctx)
            await ToolContext(ctx,actor='owner',session_id=sid).check('net.read',self.settings()['homeserver'],'скачать вложение хозяина Matrix')
            item=await self.transport.call('download',{'uri':content['url']})
            raw=base64.b64decode(item['data'],validate=True)
            if len(raw)>min(self.settings()['max_file_mb'],ctx.settings.get('attachments','max_mb',20))*1024*1024:raise failed('Вложение Matrix больше выбранного предела.')
            attachments=[await media.save(ctx,content.get('filename') or text or 'файл',raw,kind='photo' if kind=='m.image' else 'audio' if kind=='m.audio' else 'document',source='matrix',session=sid)]
        elif kind not in ('m.text','m.notice'):return
        text=text[:30000]
        tainted=bool(attachments or content.get('m.relates_to') or content.get('m.new_content'))
        if content.get('m.new_content'):text=untrusted('правка сообщения Matrix',text)
        self.transport.validate(stamp)
        await ctx.mind.say(sid,'[Matrix #'+str(ref)+'] '+text,channel='matrix',actor='owner',tainted=tainted,content=media.content('[Matrix #'+str(ref)+'] '+text,attachments,vision=ctx.settings.get('attachments','vision',True)) if attachments else None,
                           message_meta={'matrix_event':eid,'matrix_room':self.settings()['room_id'],'attachments':attachments},wait=False)

    def remember(self,ctx,eid,sid,text,direction):
        records=ctx.store.meta_get('matrix:refs',{})
        old=next((key for key,value in records.items() if value['event_id']==eid and value['session']==sid),None)
        if old:return int(old)
        number=ctx.store.meta_get('matrix:next_ref',0)+1
        records[str(number)]={'event_id':eid,'session':sid,'room':self.settings()['room_id'],'text':text[:1000],'direction':direction}
        ctx.store.meta_set('matrix:refs',dict(list(records.items())[-2000:]));ctx.store.meta_set('matrix:next_ref',number);return number

    def ref(self,ctx,number):
        row=ctx.app.store.meta_get('matrix:refs',{}).get(str(number))
        if not row or row['session']!=ctx.session_id or row['room']!=self.settings()['room_id']:raise failed('Сообщение не относится к этому разговору Matrix.')
        return row['event_id']

    def destination(self,ctx):
        agent,sid=self.selected()
        if ctx.app.agent_id!=agent.agent_id or ctx.session_id!=sid:raise failed('Это не привязанный разговор хозяина Matrix.')
        return sid

    async def send(self,ctx,text,*,reply_to=None):
        self.destination(ctx);stamp=self.transport.stamp()
        await ctx.check('msg.send','matrix:owner','отправить сообщение хозяину Matrix',to_owner=True)
        self.transport.validate(stamp);self.destination(ctx)
        result=await self.transport.call('send',{'text':text,**({'reply':self.ref(ctx,reply_to)} if reply_to is not None else {})})
        eid=result.get('event_id')
        if not isinstance(eid,str):raise failed('Matrix не вернул event_id; перед повтором проверь комнату.')
        return self.remember(ctx.app,eid,ctx.session_id,text,'out')

    async def react(self,ctx,number,key):
        self.destination(ctx);eid=self.ref(ctx,number);stamp=self.transport.stamp()
        await ctx.check('msg.send','matrix:owner','поставить реакцию хозяину Matrix',to_owner=True)
        self.transport.validate(stamp);self.destination(ctx)
        return await self.transport.call('react',{'event_id':eid,'key':key})

    async def files(self,ctx,files,caption):
        self.destination(ctx);stamp=self.transport.stamp()
        if any(len(item['bytes'])>self.settings()['max_file_mb']*1024*1024 for item in files):raise failed('Вложение Matrix больше выбранного предела, не более 20 МБ.')
        await ctx.check('msg.send','matrix:owner','отправить вложения хозяину Matrix',to_owner=True)
        for item in files:
            self.transport.validate(stamp);self.destination(ctx)
            await self.transport.call('upload',{'data':base64.b64encode(item['bytes']).decode(),'name':item['name'],'mime':item['mime'],'kind':item['kind'],'caption':caption})

    def answer(self,event):
        if event.get('type')!='answer' or event.get('channel')!='matrix':return
        if self.stopping or not self.owner() or not self.settings()['enabled'] or not self.sender or self.sender.done():return
        if event.get('agent_id','main')!=self.settings()['agent_id']:return
        if (event.get('result') or {}).get('sent_all'):return
        ctx,sid=self.selected()
        if event.get('session')!=sid:return
        try:self.outbox.put_nowait((ctx,sid,(event.get('result') or {}).get('text') or '',self.identity()))
        except asyncio.QueueFull:self.notice='Очередь Matrix переполнена; итог сохранён в пульте.';self.event()

    async def deliver(self):
        while not self.stopping:
            ctx,sid,text,identity=await self.outbox.get()
            try:
                if identity!=self.identity():continue
                if text:await self.send(ToolContext(ctx,actor='system',session_id=sid,tool='matrix_reply'),text)
            except asyncio.CancelledError:raise
            except Exception:
                self.notice='Ответ Matrix не доставлен. Прочитай его в пульте; автоматического повтора нет.';self.event()

    async def close(self):
        self.runtime=False
        if self.restarting and self.restarting is not asyncio.current_task():
            self.restarting.cancel();await asyncio.gather(self.restarting,return_exceptions=True)
        await self.stop()
