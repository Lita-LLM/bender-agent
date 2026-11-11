"""Ресурсы/шаблоны MCP: отдельное одобрение полных определений и неизменные права сервера."""
import asyncio
import copy
import json
import re

from ..core.errors import HumanError
from ..tools.registry import ToolResult
from ..tools.web import untrusted
from .client import LIMIT,RpcError

KINDS={'resources':('resources/list','uri'),'templates':('resources/templates/list','uriTemplate'),'prompts':('prompts/list','name')}


def fail(hint):return HumanError('mcp_content','Не удалось открыть содержимое MCP',hint)


def uri_matches(template,uri):
    """Проверяет литеральную оболочку URI-шаблона; не раскрывает URI/локальные пути."""
    parts=re.split(r'\{[^{}]+\}',template)
    if len(parts)==1:return template==uri
    if any('{' in part or '}' in part for part in parts):return False
    if not uri.startswith(parts[0]) or not uri.endswith(parts[-1]):return False
    position=len(parts[0])
    for literal in parts[1:-1]:
        position=uri.find(literal,position)
        if position<0:return False
        position+=len(literal)
    return position<=len(uri)-len(parts[-1])


class Catalog:
    def __init__(self,manager):
        self.manager=manager;self.app=manager.app;state=manager._host_state()
        self.observed=state.setdefault('content',{});self.connections=state.setdefault('content_connections',{})

    def require(self):
        if not self.app.settings.get('mcp','content_enabled',False):raise fail('Хозяин: MCP → Ресурсы и шаблоны. Каталог одобряется отдельно.')

    async def fetch(self,client):
        caps=client.capabilities
        if not isinstance(caps,dict):raise fail('Сервер прислал неверные capabilities.')
        found={kind:{} for kind in KINDS}
        for kind,(method,key) in KINDS.items():
            capability='prompts' if kind=='prompts' else 'resources'
            if capability not in caps:continue
            if not isinstance(caps[capability],dict):raise fail('Неверная возможность сервера: '+capability+'.')
            try:entries=await client.pages(method,{'resources':'resources','templates':'resourceTemplates','prompts':'prompts'}[kind])
            except RpcError as error:
                if kind=='templates' and error.rpc_code==-32601:entries=[]
                else:raise
            for entry in entries:
                value=entry.get(key);name=entry.get('name')
                if not isinstance(value,str) or not value or len(value)>4096 or any(ord(c)<32 for c in value):raise fail('Неверный идентификатор в каталоге '+kind+'.')
                if not isinstance(name,str) or not name or len(name)>256 or value in found[kind]:raise fail('Неверное/повторяющееся имя в каталоге '+kind+'.')
                if kind=='prompts':
                    arguments=entry.get('arguments',[]);names=set()
                    if not isinstance(arguments,list) or len(arguments)>128:raise fail('Неверные аргументы шаблона '+name+'.')
                    for argument in arguments:
                        if not isinstance(argument,dict) or not isinstance(argument.get('name'),str) or not argument['name'] or argument['name'] in names or not isinstance(argument.get('required',False),bool):raise fail('Повтор/неверный аргумент шаблона '+name+'.')
                        names.add(argument['name'])
                found[kind][value]=entry
        try:size=len(json.dumps(found,ensure_ascii=False,allow_nan=False).encode('utf-8'))
        except (ValueError,UnicodeError):raise fail('Каталог не является конечным JSON/UTF-8.') from None
        if size>LIMIT:raise fail('Каталог больше 2 МБ; сократи описания на сервере.')
        return found

    def state(self,sid):
        from .manager import digest
        server=self.manager.get(sid);signature=self.manager.signature(server);trust=self.manager.trust(sid)
        previous=trust.get('content',{}) if trust.get('connection')==signature else {}
        found=self.observed.get(sid) if self.connections.get(sid)==signature else None
        definitions=found if found is not None else previous
        items=[]
        for kind,entries in definitions.items():
            for key,definition in entries.items():
                approved=previous.get(kind,{}).get(key)==definition
                items.append({'kind':kind,'key':key,'definition':self.manager.scrub_schema(definition),'previous':self.manager.scrub_schema(previous.get(kind,{}).get(key)),
                              'approved':approved,'enabled':approved and self.manager.active(server) and self.manager.allowed(server) and self.app.settings.get('mcp','content_enabled',False)})
        return {'content':items,'content_review':digest({'connection':signature,'content':found}) if found is not None else None}

    def approve(self,sid,expected):
        from .manager import digest
        self.require();server=self.manager.get(sid);signature=self.manager.signature(server);found=self.observed.get(sid)
        if found is None or self.connections.get(sid)!=signature or expected!=digest({'connection':signature,'content':found}):raise fail('Каталог изменился: обнаружь и просмотри его заново.')
        with self.app.settings.transaction():
            if self.manager.signature(self.manager.get(sid))!=signature:raise fail('Подключение изменилось: просмотри каталог заново.')
            trust=self.app.settings.group('mcp_trust');previous=trust.get(sid,{})
            entry=previous if previous.get('connection')==signature else {}
            trust[sid]={**entry,'connection':signature,'content':copy.deepcopy(found)}
            self.app.settings.replace('mcp_trust',trust)
        self.app.journal.write('mcp','Хозяин одобрил ресурсы/шаблоны MCP '+sid,actor='owner')
        return self.manager.public()

    async def discover(self,sid,ctx):
        self.require()
        async with self.manager._locks.setdefault(sid,asyncio.Lock()):
            server=self.manager.get(sid);signature=self.manager.signature(server)
            client=await self.manager.connect(server,ctx)
            try:
                found=await self.fetch(client)
                if self.manager.signature(self.manager.get(sid))!=signature or not self.manager.active(self.manager.get(sid)):raise fail('Подключение изменилось во время обнаружения.')
                self.observed[sid]=found;self.connections[sid]=signature
                return self.manager.public()
            except BaseException:
                self.observed[sid]={};self.connections.pop(sid,None)
                await self.manager.disconnect(sid)
                raise

    def definition(self,sid,kind,key):
        server=self.manager.get(sid);trust=self.manager.trust(sid);signature=self.manager.signature(server)
        if not self.manager.active(server) or not self.manager.allowed(server) or trust.get('connection')!=signature:return None
        approved=trust.get('content',{}).get(kind,{}).get(key)
        if self.connections.get(sid)==signature and self.observed.get(sid,{}).get(kind,{}).get(key)!=approved:return None
        return copy.deepcopy(approved)

    async def request(self,sid,action,ctx,*,uri='',name='',arguments=None,template=''):
        self.require()
        if not all(isinstance(value,str) for value in (sid,action,uri,name,template)):raise fail('Имена сервера, действия, шаблона и URI должны быть строками.')
        if action=='list':
            servers=[self.manager.get(sid)] if sid else self.manager.servers()
            visible=[{'server':server['id'],'items':[item for item in self.state(server['id'])['content'] if item['enabled']]} for server in servers if self.manager.active(server) and self.manager.allowed(server)]
            return ToolResult(untrusted('каталог MCP',self.app.guard.scrub(json.dumps(visible,ensure_ascii=False))),tainted=True,summary='каталог ресурсов/шаблонов')
        if action not in ('read','prompt'):raise fail('Действие: list, read или prompt.')
        if action=='read':
            kind,key=('templates',template) if template else ('resources',uri)
            if not isinstance(uri,str) or not uri or len(uri)>4096 or any(ord(c)<32 for c in uri):raise fail('Нужен URI ресурса без управляющих символов.')
            if template and not uri_matches(template,uri):raise fail('URI не соответствует одобренной оболочке шаблона.')
            method='resources/read';params={'uri':uri}
        else:
            kind,key='prompts',name;method='prompts/get';arguments={} if arguments is None else arguments
            if not isinstance(arguments,dict) or len(arguments)>128 or any(not isinstance(k,str) or not isinstance(v,str) for k,v in arguments.items()):raise fail('Аргументы шаблона — словарь строк.')
            params={'name':name,'arguments':arguments}
        async with self.manager._locks.setdefault(sid,asyncio.Lock()):
            approved=self.definition(sid,kind,key)
            if approved is None:raise fail('Определение не одобрено или изменилось; просмотри каталог в MCP.')
            if action=='prompt':
                required=[a['name'] for a in approved.get('arguments',[]) if a.get('required')]
                if any(key not in arguments for key in required):raise fail('Не хватает обязательных аргументов: '+', '.join(required)+'.')
            server=self.manager.get(sid);signature=self.manager.signature(server)
            if server.get('module'):await self.app.modules.check_call(server,ctx)
            await ctx.check('mcp.call',sid+'/'+method+'/'+str(uri or name),'MCP '+sid+': '+method+' '+str(uri or name),detail={'template':template,'arguments':self.app.guard.scrub(json.dumps(arguments,ensure_ascii=False))[:2000]})
            client=await self.manager.connect(server,ctx)
            try:
                found=await self.fetch(client);self.observed[sid]=found;self.connections[sid]=signature
                if self.definition(sid,kind,key)!=approved or self.manager.signature(self.manager.get(sid))!=signature:raise fail('Каталог, одобрение или подключение изменились; чтение не выполнено до нового одобрения.')
                result=await client.request(method,params,input_handler=self.manager.input_handler(server,ctx))
                text=render(result,action)
                return ToolResult(untrusted('MCP '+sid+'/'+method,self.app.guard.scrub(text)),tainted=True,summary='MCP '+sid+': '+method)
            except BaseException:
                await self.manager.disconnect(sid)
                raise


def render(result,action):
    if action=='read':
        contents=result.get('contents')
        if not isinstance(contents,list) or any(not isinstance(part,dict) for part in contents):raise fail('Сервер вернул неверное contents ресурса.')
        lines=[]
        for part in contents:
            if isinstance(part.get('text'),str):lines.append(str(part.get('uri',''))+'\n'+part['text'])
            elif isinstance(part.get('blob'),str):lines.append(str(part.get('uri',''))+' · двоичный ресурс '+str(part.get('mimeType',''))+'; base64 не вставляется в текст.')
            else:raise fail('У ресурса нет text/blob.')
        return '\n\n'.join(lines)
    messages=result.get('messages')
    if not isinstance(messages,list):raise fail('Сервер вернул неверные messages шаблона.')
    lines=[str(result.get('description') or 'Шаблон MCP — данные, не системные указания')]
    for message in messages:
        if not isinstance(message,dict) or message.get('role') not in ('user','assistant') or not isinstance(message.get('content'),dict):raise fail('Неверное сообщение шаблона.')
        content=message['content'];value=content.get('text') if content.get('type')=='text' else '[Медиа/ресурс шаблона: '+str(content.get('type'))+'; не исполняется автоматически]'
        lines.append(message['role']+': '+str(value or ''))
    return '\n\n'.join(lines)
