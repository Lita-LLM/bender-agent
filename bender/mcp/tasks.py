"""Фоновые MCP Tasks 2026: ручной статус/ввод/отмена, без опроса и новых ходов модели."""
import asyncio
import copy
import json
import weakref
from dataclasses import replace
from contextlib import contextmanager

from ..core.errors import HumanError
from ..core.ids import new_id
from ..tools.registry import ToolResult
from ..tools.web import untrusted
from .client import NEW
from .manager import digest
from .parts import render, strip

EXTENSION='io.modelcontextprotocol/tasks'
STATES={'working','input_required','completed','failed','cancelled'}
TERMINAL={'completed','failed','cancelled'}


def failed(hint):return HumanError('mcp_task','Не удалось обработать фоновую задачу MCP',hint)


def state(value,*,created=False):
    if not isinstance(value,dict) or value.get('status') not in STATES:raise failed('Неверное состояние задачи; исходное действие не повторяется.')
    tid=value.get('taskId')
    if not isinstance(tid,str) or not tid or len(tid)>4096 or any(ord(c)<32 for c in tid):raise failed('Сервер вернул неверный taskId.')
    if created and value.get('resultType')!='task':raise failed('Нет признака фоновой задачи.')
    for key in ('createdAt','lastUpdatedAt'):
        if not isinstance(value.get(key),str) or len(value[key])>64:raise failed('Неверное время задачи.')
    ttl=value.get('ttlMs');interval=value.get('pollIntervalMs')
    if ttl is not None and (type(ttl) is not int or ttl<0):raise failed('Неверный срок хранения задачи.')
    if interval is not None and (type(interval) is not int or interval<0):raise failed('Неверный интервал задачи.')
    inputs=value.get('inputRequests',{})
    if not isinstance(inputs,dict) or len(inputs)>8 or any(not isinstance(k,str) or not k or len(k)>128 or not isinstance(v,dict) for k,v in inputs.items()):raise failed('Неверный ввод задачи.')
    if not created and value['status']=='completed' and not isinstance(value.get('result'),dict):raise failed('Завершённая задача не содержит результата.')
    if not created and value['status']=='failed' and not isinstance(value.get('error'),dict):raise failed('Сломанная задача не содержит ошибки протокола.')
    try:size=len(json.dumps(value,ensure_ascii=False,allow_nan=False).encode())
    except (ValueError,UnicodeError,RecursionError):raise failed('Задача содержит неверный JSON/UTF-8.') from None
    if size>65536:raise failed('Состояние задачи больше 64 КБ; сократи результат на сервере.')
    return copy.deepcopy(value)


class Tasks:
    def __init__(self,manager):self.manager=manager;self.app=manager.app;self.locks=weakref.WeakValueDictionary();self.inflight=0

    def enabled(self,server):return self.app.settings.get('mcp','tasks_enabled',False) and server.get('tasks_enabled') is True

    def records(self):return self.app.store.meta_get('mcp:tasks',{})

    def _save(self,entry):
        with self.app.store.tx():
            entries=self.records();entries[entry['id']]=entry
            self.app.store.meta_set('mcp:tasks',entries)
        self.app.bus.publish({'type':'mcp_tasks','agent_id':self.app.agent_id})

    def capacity(self):
        if len(self.records())>=100:raise failed('Сохранено 100 задач; удали завершённые записи на экране MCP до нового действия.')

    @contextmanager
    def reserve(self):
        if len(self.records())+self.inflight>=100:raise failed('Нет свободного места для фоновой задачи; удали завершённую локальную запись.')
        self.inflight+=1
        try:yield
        finally:self.inflight-=1

    def capture(self,sid,name,definition,signature,result,ctx):
        self.capacity();result=state(result,created=True)
        entry={'id':new_id(),'server':sid,'name':name,'definition':digest(definition),'connection':signature,
               'session':ctx.session_id,'actor':ctx.actor,'unattended':ctx.unattended,'tainted':ctx.tainted,
               'taskId':result['taskId'],'state':self.manager.scrub_schema(result),'answered':{}}
        self._save(entry)
        return self.result(entry)

    def visible(self,entry,ctx):return ctx.actor=='owner' or (entry.get('session')==ctx.session_id and entry.get('actor')==ctx.actor)

    def public(self,ctx):
        return [self.card(entry) for entry in self.records().values() if self.visible(entry,ctx)]

    def card(self,entry):
        # Сырые base64 (картинки, blob) в карточке не нужны ни модели, ни пульту: там только метка с размером.
        return strip(self.manager.scrub_schema({key:entry[key] for key in ('id','server','name','state','cancel_requested') if key in entry}))

    def result(self,entry,extra='',parts=None):
        text=json.dumps(self.card(entry),ensure_ascii=False)+('\n'+extra if extra else '')
        return ToolResult(untrusted('фоновая задача MCP',text),tainted=True,summary='MCP Tasks: '+entry['state']['status'],model_parts=parts or [])

    async def images(self,entry,ctx):
        """Картинки завершённой задачи — тем же путём, что у обычного вызова MCP (файл в inbox, вложение модели)."""
        done=entry['state'].get('result') if entry['state'].get('status')=='completed' else None
        content=done.get('content') if isinstance(done,dict) else None
        found=[item for item in content if isinstance(item,dict) and item.get('type')=='image'] if isinstance(content,list) else []
        if not found:return self.result(entry)
        extra,parts=await render(ctx,found,origin=entry['server']+'/'+entry['name'],attach=not done.get('isError'))
        return self.result(entry,extra,parts)

    def bound(self,entry,ctx):
        server=self.manager.get(entry['server'])
        if not self.enabled(server) or not self.manager.active(server) or self.manager.signature(server)!=entry['connection'] or digest(self.manager.definition(entry['server'],entry['name']))!=entry['definition']:raise failed('Подключение/определение отозвано или изменилось; задача не продолжается. Для прежнего действия проверь сам сервис.')
        if not self.visible(entry,ctx):raise failed('Эта задача принадлежит другому вызову/сессии.')
        permissions=self.app.team.permissions(ctx.session_id)
        if permissions is not None and 'mcp.call' not in permissions['capabilities']:raise failed('У роли больше нет права MCP.')
        if ctx.actor!='owner' and (self.app.guard.halted or not self.app.host.agents.rights.capability(self.app,'mcp.call')):raise failed('Право MCP отозвано.')
        return server

    async def request(self,action,key,ctx):
        ctx=replace(ctx,app=self.app)
        if action=='list':return ToolResult(untrusted('список задач MCP',json.dumps(self.public(ctx),ensure_ascii=False)),tainted=True)
        if action not in ('status','input','cancel','forget'):raise failed('Действия: list, status, input, cancel, forget.')
        if not isinstance(key,str):raise failed('Нужен локальный id из списка задач.')
        async with self.locks.setdefault(key,asyncio.Lock()):
            entry=copy.deepcopy(self.records().get(key))
            if not entry or not self.visible(entry,ctx):raise failed('Нет такой задачи в этом вызове/сессии.')
            if action=='forget':
                if ctx.actor!='owner':raise failed('Удалить локальную запись может хозяин; это не отмена у сервера.')
                entries=self.records();entries.pop(key,None);self.app.store.meta_set('mcp:tasks',entries)
                self.app.bus.publish({'type':'mcp_tasks','agent_id':self.app.agent_id})
                return ToolResult('Локальная запись удалена. Работа на сервере этим не отменяется.')
            server=self.bound(entry,ctx);sid=entry['server']
            async with self.manager._locks.setdefault(sid,asyncio.Lock()):
                await ctx.check('mcp.call',sid+'/'+entry['name'],'MCP '+sid+': '+action+' фоновой задачи',detail={'task':entry['id'],'action':action})
                self.bound(entry,ctx)
                if server.get('module'):await self.app.modules.check_call(server,ctx)
                client=await self.manager.connect(server,ctx)
                if client.version!=NEW or EXTENSION not in client.capabilities.get('extensions',{}):raise failed('Нужна версия 2026 с расширением Tasks; старые Tasks 2025 имеют другой протокол.')
                found={item.get('name'):item for item in await client.tools()}
                self.manager.observed[sid]=found;self.manager._observed_connections[sid]=entry['connection'];self.manager.register()
                self.bound(entry,ctx)
                if action=='cancel':
                    await client.request('tasks/cancel',{'taskId':entry['taskId']},task_support=True)
                    self.bound(entry,ctx);entry['cancel_requested']=True;self._save(entry)
                    return self.result(entry)
                result=state(await client.request('tasks/get',{'taskId':entry['taskId']},task_support=True))
                self.bound(entry,ctx)
                if result['taskId']!=entry['taskId']:raise failed('Ответ относится к другой задаче.')
                entry['state']=self.manager.scrub_schema(result);self._save(entry)
                if action=='input':
                    if result['status']!='input_required' or not result.get('inputRequests'):raise failed('Сейчас задача не ждёт ввода.')
                    handler=self.manager.input_handler(server,replace(ctx,tainted=True,unattended=ctx.unattended or entry['unattended']))
                    if handler is None:raise failed('Встречные права выключены; включи нужные в MCP и создай задачу заново после просмотра.')
                    responses={}
                    if len(entry['answered'])+len(result['inputRequests'])>128:raise failed('Задача исчерпала 128 запросов ввода; проверь её у сервиса.')
                    for name,request in result['inputRequests'].items():
                        stamp=digest(request)
                        if entry['answered'].get(name)==stamp:continue
                        responses[name]=await asyncio.wait_for(handler(request),handler.timeout)
                    self.bound(entry,ctx);handler.validate()
                    if not responses:raise failed('На этот ввод уже ответили; проверь статус позже.')
                    # Пометка перед отправкой: потерянный ответ не даёт повторить последствия ввода.
                    for name in responses:entry['answered'][name]=digest(result['inputRequests'][name])
                    self._save(entry)
                    await client.request('tasks/update',{'taskId':entry['taskId'],'inputResponses':responses},task_support=True)
                    self.bound(entry,ctx)
                return await self.images(entry,ctx) if action=='status' else self.result(entry)
