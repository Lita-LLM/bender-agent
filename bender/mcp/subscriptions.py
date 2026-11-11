"""Подписки MCP 2026 по HTTP: события в пульт и карантин, без автоматических действий."""
import asyncio
import copy
import json
import time
from dataclasses import replace

from .. import __version__
from ..core.errors import HumanError
from ..core.ids import new_id
from ..net.http_client import Timeouts
from ..net.sse import parse_sse
from .client import NEW,Http,LIMIT,_message
from .manager import digest

SUBSCRIPTION='io.modelcontextprotocol/subscriptionId'
FILTERS={'toolsListChanged':'notifications/tools/list_changed','promptsListChanged':'notifications/prompts/list_changed','resourcesListChanged':'notifications/resources/list_changed'}


def failed(hint):return HumanError('mcp_subscription','Подписка MCP остановлена',hint)


class Subscriptions:
    def __init__(self,manager):self.manager=manager;self.app=manager.app;self.active={};self.states={}

    def public(self):return copy.deepcopy(self.states)

    async def start(self,sid,filters,ctx):
        if ctx.actor!='owner' or ctx.unattended:raise failed('Подписку запускает хозяин из пульта.')
        ctx=replace(ctx,app=self.app)
        if not self.app.settings.get('mcp','subscriptions_enabled',False):raise failed('Включи подписки на экране MCP.')
        if not isinstance(filters,dict) or not filters or any(key not in FILTERS and key not in ('resourceSubscriptions','taskIds') for key in filters):raise failed('Выбери стандартные события/одобренные URI/свои задачи.')
        for key in FILTERS:
            if key in filters and type(filters[key]) is not bool:raise failed('Фильтр каталога должен быть переключателем.')
        for key in ('resourceSubscriptions','taskIds'):
            values=filters.get(key,[])
            if not isinstance(values,list) or len(values)>100 or any(not isinstance(v,str) or not v or len(v)>4096 for v in values) or len(set(values))!=len(values):raise failed('Неверный список '+key+'.')
        server=self.manager.get(sid);signature=self.manager.signature(server);trust=copy.deepcopy(self.manager.trust(sid))
        if server.get('transport')!='http':raise failed('Подписки сейчас доступны для Streamable HTTP 2026; stdio и старый SSE не объявляют эту возможность.')
        if filters.get('resourceSubscriptions'):
            self.manager.catalog.require()
            if any(self.manager.catalog.definition(sid,'resources',uri) is None for uri in filters['resourceSubscriptions']):raise failed('URI должен быть в отдельно одобренном каталоге ресурсов.')
        local_tasks=self.manager.tasks.records()
        raw_ids=[]
        for key in filters.get('taskIds',[]):
            entry=local_tasks.get(key)
            if not entry or entry['server']!=sid:raise failed('Можно подписаться только на собственную задачу этого MCP-сервера.')
            self.manager.tasks.bound(entry,ctx);raw_ids.append(entry['taskId'])
        await ctx.check('mcp.call',sid+'/subscriptions/listen','получать события MCP '+sid,detail={'notifications':filters})
        if self.manager.signature(self.manager.get(sid))!=signature or not self.manager.active(self.manager.get(sid)) or not self.app.settings.get('mcp','subscriptions_enabled',False) or self.manager.trust(sid)!=trust:raise failed('Подключение/настройки изменились во время подтверждения.')
        client=await self.manager.connect(server,ctx)
        if client.version!=NEW or not isinstance(client.transport,Http):raise failed('Нужен Streamable HTTP версии 2026.')
        if raw_ids and 'io.modelcontextprotocol/tasks' not in client.capabilities.get('extensions',{}):raise failed('Сервер не объявил Tasks 2026.')
        if len(self.active)>=16 and sid not in self.active:raise failed('Уже открыто 16 подписок; закрой лишнюю.')
        await self.stop(sid)
        wire=copy.deepcopy(filters)
        if 'taskIds' in wire:wire['taskIds']=raw_ids
        self.states[sid]={'active':True,'acknowledged':False,'notifications':{},'message':'Ожидаю подтверждение сервера'}
        self.active[sid]=asyncio.create_task(self.listen(sid,wire,client,signature,trust),name='mcp-subscription-'+sid)
        self.event();return self.manager.public()

    def event(self):self.app.bus.publish({'type':'mcp_subscriptions','agent_id':self.app.agent_id})

    def validate(self,sid,signature,trust):
        if not self.app.settings.get('mcp','subscriptions_enabled',False) or not self.manager.active(self.manager.get(sid)) or self.manager.signature(self.manager.get(sid))!=signature or self.manager.trust(sid)!=trust:raise failed('Подключение, права или одобрение изменились.')

    async def listen(self,sid,filters,client,signature,trust):
        try:
            mid='subscription-'+new_id();transport=client.transport
            capabilities={'extensions':{'io.modelcontextprotocol/tasks':{}}} if filters.get('taskIds') else {}
            meta={'io.modelcontextprotocol/protocolVersion':NEW,'io.modelcontextprotocol/clientInfo':{'name':'bender','version':__version__},'io.modelcontextprotocol/clientCapabilities':capabilities}
            message={'jsonrpc':'2.0','id':mid,'method':'subscriptions/listen','params':{'_meta':meta,'notifications':filters}}
            headers={key:value for key,value in transport.headers.items() if not key.lower().startswith('mcp-') and key.lower()!='accept'}
            headers.update({'Accept':'text/event-stream','Mcp-Method':'subscriptions/listen','Mcp-Protocol-Version':NEW})
            self.validate(sid,signature,trust)
            response=await transport.client.post(transport.url,json=message,headers=headers,timeouts=Timeouts(connect=15,read=300),retry_stale=False)
            async with response:
                if not response.ok or (response.headers.get('content-type') or '').split(';')[0].strip().lower()!='text/event-stream':raise failed('Нужен успешный SSE-ответ без редиректов; HTTP '+str(response.status)+'.')
                async def lines():
                    buffer=b'';size=0
                    async for chunk in response.iter_bytes():
                        buffer+=chunk
                        if len(buffer)>LIMIT:raise failed('Строка подписки больше 2 МБ.')
                        while b'\n' in buffer:
                            line,buffer=buffer.split(b'\n',1);size+=len(line)+1
                            if size>LIMIT:raise failed('Событие подписки больше 2 МБ.')
                            if not line.rstrip(b'\r'):size=0
                            yield line.rstrip(b'\r').decode('utf-8')
                    if buffer:yield buffer.decode('utf-8')
                ack=None;count=0;since=time.monotonic()
                async for event in parse_sse(lines()):
                    if not event.data:continue
                    self.validate(sid,signature,trust)
                    if time.monotonic()-since>=60:count=0;since=time.monotonic()
                    count+=1
                    if count>120:raise failed('Больше 120 событий в минуту; проверь сервер перед новым подключением.')
                    received=_message(json.loads(event.data));params=received.get('params') if 'method' in received else received.get('result')
                    if not isinstance(params,dict) or not isinstance(params.get('_meta'),dict) or params['_meta'].get(SUBSCRIPTION)!=mid:raise failed('Событие относится к другой подписке.')
                    if received.get('id')==mid and 'method' not in received:
                        if ack is None:raise failed('Сервер закрыл подписку до подтверждения.')
                        self.states[sid]['message']='Сервер завершил подписку';return
                    if 'id' in received:raise failed('В подписке допустимы только уведомления, не встречные запросы.')
                    method=received.get('method')
                    if ack is None:
                        if method!='notifications/subscriptions/acknowledged':raise failed('Первым должно быть подтверждение подписки.')
                        ack=params.get('notifications')
                        if not isinstance(ack,dict) or any(key not in filters for key in ack):raise failed('Сервер подтвердил незапрошенные события.')
                        for key,value in ack.items():
                            if key in FILTERS:
                                if value is not True or filters[key] is not True:raise failed('Неверное подтверждение каталога.')
                            elif not isinstance(value,list) or any(v not in filters[key] for v in value):raise failed('Сервер подтвердил чужой URI/задачу.')
                        self.states[sid].update(acknowledged=True,notifications=copy.deepcopy(ack),message='Подписка открыта');self.event();continue
                    self.notification(sid,method,params,ack,signature)
                    self.states[sid]['message']='Получено событие: '+str(method);self.event()
                raise failed('Поток оборвался. Подписку можно открыть заново кнопкой; автоматического повтора нет.')
        except asyncio.CancelledError:raise
        except Exception as error:
            self.states[sid]['message']=self.app.guard.scrub(error.hint if isinstance(error,HumanError) else 'Ошибка потока; проверь сервер и открой подписку заново.')
        finally:
            self.states[sid]['active']=False;self.active.pop(sid,None);self.event()

    def notification(self,sid,method,params,ack,signature):
        field=next((key for key,value in FILTERS.items() if value==method),None)
        if field:
            if ack.get(field) is not True:raise failed('Неожиданное изменение каталога.')
            if field=='toolsListChanged':
                self.manager.observed[sid]={};self.manager._observed_connections[sid]=signature;self.manager.register()
            else:
                catalog=self.manager.catalog
                found=catalog.observed.get(sid) if catalog.connections.get(sid)==signature else copy.deepcopy(self.manager.trust(sid).get('content',{}))
                found=copy.deepcopy(found or {});found['prompts' if field=='promptsListChanged' else 'resources']={}
                if field=='resourcesListChanged':found['templates']={}
                catalog.observed[sid]=found;catalog.connections[sid]=signature
            return
        if method=='notifications/resources/updated' and params.get('uri') in ack.get('resourceSubscriptions',[]):return
        if method=='notifications/tasks' and params.get('taskId') in ack.get('taskIds',[]):
            from .tasks import state
            result=state({k:v for k,v in params.items() if k!='_meta'})
            for entry in self.manager.tasks.records().values():
                if entry['server']==sid and entry['taskId']==params['taskId'] and entry['connection']==signature and digest(self.manager.definition(sid,entry['name']))==entry['definition']:
                    entry['state']=self.manager.scrub_schema(result);self.manager.tasks._save(entry);return
        raise failed('Неожиданное уведомление сервера; действия не выполнялись.')

    def cancel(self):
        for task in list(self.active.values()):
            if not task.get_loop().is_closed():task.get_loop().call_soon_threadsafe(task.cancel)

    async def stop(self,sid):
        task=self.active.get(sid)
        if task:
            task.cancel();await asyncio.gather(task,return_exceptions=True)

    async def close(self):
        tasks=list(self.active.values());self.cancel()
        if tasks:await asyncio.gather(*tasks,return_exceptions=True)
