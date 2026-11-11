"""Вход хозяина в MCP: метаданные сначала показываются, секреты не участвуют в discovery."""
import asyncio
import copy
import hmac
import re
import secrets
import time
from dataclasses import replace
from urllib.parse import parse_qsl,urlencode,urlsplit,urlunsplit

from ..core.ids import new_id
from ..net.http_client import NetError,Timeouts
from .manager import digest
from .oauth import authorization_metadata,challenge,failed,metadata_urls,origin,pkce,resource_metadata,scopes,secure_url,validate_issuer
from .oauth_tokens import Tokens,mapping,text

LIMIT=128*1024
LIFETIME=600


class OAuth(Tokens):
    def __init__(self,manager):
        self.manager=manager;self.app=manager.app;self.prepared={};self.closed=False

    def require(self,sid):
        if self.closed or self.app.safe or not self.app.settings.get('mcp','oauth_enabled',False):raise failed('Хозяин: включи вход OAuth на экране MCP. В безопасном режиме он запрещён.')
        server=self.manager.get(sid)
        if not self.manager.active(server):raise failed('Сначала включи MCP и это подключение; сохранение само его не запускает.')
        if server['transport'] not in ('http','sse'):raise failed('OAuth предназначен для HTTP/SSE; локальный stdio получает секреты через окружение.')
        secure_url(server['url'])
        return server

    def owner(self,ctx):
        if ctx.actor!='owner' or ctx.unattended:raise failed('Входом и токенами управляет хозяин из пульта.')
        # HTTP API использует Proxy: долгий OAuth не должен зависеть от выбранного потом агента.
        return replace(ctx,app=self.app)

    def cleanup(self):
        now=time.monotonic()
        for pid,plan in list(self.prepared.items()):
            if now-plan['created']>LIFETIME:self.prepared.pop(pid,None)

    def plan(self,pid,sid):
        if not isinstance(pid,str) or not isinstance(sid,str):raise failed('Нужны имя сервера и действующий план входа.')
        self.cleanup();plan=self.prepared.get(pid)
        if not plan or plan['sid']!=sid:raise failed('План входа закрыт или устарел; начни обнаружение заново.')
        server=self.require(sid)
        if self.manager.signature(server)!=plan['connection']:raise failed('Подключение изменилось; заново просмотри вход.')
        return plan

    async def json_request(self,method,url,*,body=None,headers=None,missing=False):
        secure_url(url)
        try:
            response=await self.app.http.request(method,url,body=body,headers={'Accept':'application/json',**(headers or {})},timeouts=Timeouts(connect=10,read=20,total=25),retry_stale=False)
            async with response:
                if missing and response.status in (404,405):return None
                if not response.ok:raise failed('Сервис входа ответил HTTP '+str(response.status)+'. Проверь адрес, client_id, способ проверки клиента и права сервиса; начни вход заново.')
                kind=(response.headers.get('content-type') or '').split(';')[0].strip().lower()
                if kind!='application/json':raise failed('Метаданные/ответ входа должны быть JSON, не страницей входа или редиректом.')
                raw=await response.read(limit=LIMIT)
            import json
            data=json.loads(raw)
            if not isinstance(data,dict):raise failed('Сервис вернул JSON другого вида.')
            return data
        except (NetError,OSError,ValueError,RecursionError):raise failed('Не удалось прочитать ответ входа. Проверь HTTPS/сертификат и доступность сервиса; запрос автоматически не повторён.') from None

    async def discover(self,sid,ctx):
        ctx=self.owner(ctx);server=self.require(sid);signature=self.manager.signature(server)
        await ctx.check('mcp.connect',sid+'/oauth/'+signature,'обнаружить вход OAuth для MCP '+sid,detail={'transport':'oauth','url':server['url']})
        if self.manager.signature(self.require(sid))!=signature:raise failed('Подключение изменилось во время подтверждения.')
        # Смотрим только заголовки: успешный старый SSE может никогда не закончить тело.
        try:
            response=await self.app.http.get(server['url'],headers={'Accept':'application/json'},timeouts=Timeouts(connect=10,read=15,total=20),retry_stale=False)
            async with response:
                values=[value for key,value in response.headers.items if key.lower()=='www-authenticate']
                auth=challenge(', '.join(values)) if response.status==401 else {}
                if 300<=response.status<400:raise failed('MCP перенаправляет запрос; укажи конечный адрес подключения.')
        except (NetError,OSError,ValueError):raise failed('Не удалось прочитать заголовки MCP; проверь HTTPS и адрес сервиса.') from None
        self.cleanup()
        if len(self.prepared)>=16:raise failed('Уже подготовлено 16 входов; закрой лишние или дождись истечения 10 минут.')
        candidates=[secure_url(auth['resource_metadata'])] if auth.get('resource_metadata') else metadata_urls(server['url'],resource=True)
        pid=new_id();plan={'id':pid,'sid':sid,'created':time.monotonic(),'connection':signature,'server_url':server['url'],'candidates':candidates,'challenge_scope':scopes(auth['scope']) if 'scope' in auth else None,'stage':'metadata'}
        self.prepared[pid]=plan
        # Иной узел метаданных требует отдельного явного выбора хозяином перед запросом.
        if origin(candidates[0])!=origin(server['url']):return self.public_plan(plan)
        return await self.read_resource(pid,sid,ctx)

    async def read_resource(self,pid,sid,ctx):
        ctx=self.owner(ctx);plan=self.plan(pid,sid)
        await ctx.check('mcp.connect',sid+'/oauth/resource/'+digest(plan['candidates']),'прочитать метаданные входа MCP '+sid,detail={'urls':plan['candidates']})
        self.plan(pid,sid);data=None;chosen=None
        for url in plan['candidates']:
            data=await self.json_request('GET',url,missing=True)
            if data is not None:chosen=url;break
        if data is None:raise failed('Сервис не публикует OAuth Protected Resource Metadata; проверь адрес у его автора.')
        self.plan(pid,sid)
        plan.update(resource_metadata(data,plan['server_url']));plan.update(stage='issuer',metadata_url=chosen)
        return self.public_plan(plan)

    async def choose_issuer(self,pid,sid,issuer,redirect_origin,ctx):
        ctx=self.owner(ctx);plan=self.plan(pid,sid)
        if plan.get('stage') not in ('issuer','ready') or issuer not in plan.get('issuers',[]):raise failed('Выбери издателя из обнаруженного списка; чужой адрес не принимается.')
        secure_url(issuer,issuer=True)
        await ctx.check('mcp.connect',sid+'/oauth/issuer/'+digest(issuer),'проверить выбранный сервер входа MCP '+sid,detail={'issuer':issuer})
        self.plan(pid,sid);metadata=None
        for url in metadata_urls(issuer):
            metadata=await self.json_request('GET',url,missing=True)
            if metadata is not None:break
        if metadata is None:raise failed('Сервер входа не публикует RFC8414/OIDC metadata.')
        selected=authorization_metadata(metadata,issuer)
        if not isinstance(redirect_origin,str):raise failed('Неверный адрес возврата.')
        try:parts=urlsplit(redirect_origin);_=parts.port
        except ValueError:raise failed('Неверный адрес возврата.') from None
        if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ('','/') or not parts.hostname or (parts.scheme!='https' and not (parts.scheme=='http' and parts.hostname in ('localhost','127.0.0.1','::1'))):raise failed('Для OAuth открой пульт по localhost/SSH-туннелю или HTTPS.')
        redirect_uri=redirect_origin.rstrip('/')+'/api/mcp/oauth/callback'
        self.plan(pid,sid)
        plan.update(stage='ready',authorization=selected,redirect_uri=redirect_uri)
        suggested=plan['challenge_scope']
        if suggested is None:suggested=' '.join(plan.get('scopes_supported',[]))
        existing=self.app.settings.group('mcp_oauth').get('servers',{}).get(sid,{})
        if existing.get('issuer')==issuer:suggested=scopes(' '.join([existing.get('scope',''),suggested]))
        plan['scope']=suggested
        plan['review']=digest({key:value for key,value in plan.items() if key not in ('review','created')})
        return self.public_plan(plan)

    def public_plan(self,plan):
        result={key:copy.deepcopy(plan[key]) for key in ('id','stage','candidates','resource','issuers','scope','scopes_supported','authorization','redirect_uri','review','metadata_url') if key in plan}
        if plan.get('stage')=='ready':
            existing=self._registration(plan)
            result['registration']={'client_id':existing.get('client_id',''),'method':existing.get('method','none'),'has_secret':bool(existing.get('secret'))} if existing else None
        return self.manager.scrub_schema(result)

    def _pending(self):
        if not hasattr(self.app.host,'_mcp_oauth_pending'):self.app.host._mcp_oauth_pending={}
        return self.app.host._mcp_oauth_pending

    def _lock(self,sid):
        if not hasattr(self.app.host,'_mcp_oauth_locks'):self.app.host._mcp_oauth_locks={}
        return self.app.host._mcp_oauth_locks.setdefault(sid,asyncio.Lock())

    def _validate_pending(self,flow):
        plan=flow['plan'];server=self.require(plan['sid'])
        if self.manager.signature(server)!=plan['connection']:raise failed('Подключение изменилось; токен не используется. Начни вход заново.')
        if flow.get('refresh'):
            current=mapping(mapping(self._book().get('servers')).get(plan['sid']))
            if digest(current)!=flow['grant_revision']:raise failed('Вход уже изменился; обнови экран перед повторением.')
        elif self.plan(plan['id'],plan['sid']).get('review')!=plan['review']:raise failed('План входа изменился; ответ не используется.')
        if self._credential_signature(self._registration(plan))!=flow['credentials']:raise failed('Регистрация или секрет клиента изменились; начни вход заново.')

    async def authorize(self,pid,sid,options,ctx):
        ctx=self.owner(ctx);plan=self.plan(pid,sid)
        if plan.get('stage')!='ready' or options.get('review')!=plan.get('review'):raise failed('Сначала просмотри выбранного издателя и права заново.')
        requested=scopes(options.get('scope',plan['scope']))
        await ctx.check('mcp.connect',sid+'/oauth/authorize/'+plan['review'],'войти в MCP '+sid,detail={'issuer':plan['authorization']['issuer'],'resource':plan['resource'],'scope':requested,'redirect_uri':plan['redirect_uri'],'registration':options.get('mode')})
        self.plan(pid,sid)
        async with self._lock(sid):
            registration=await self._register(plan,options,ctx)
            self.plan(pid,sid)
            if len(self._pending())>=16:raise failed('Уже ждут ответа 16 входов OAuth; закрой лишние.')
            verifier,code_challenge=pkce();state=secrets.token_urlsafe(32)
            plan['created']=time.monotonic()
            flow={'oauth':self,'state':state,'plan':copy.deepcopy(plan),'registration':copy.deepcopy(registration),'credentials':self._credential_signature(registration),
                  'ctx':ctx,'verifier':verifier,'scope':requested,'created':time.monotonic()}
            def expire():
                self._pending().pop(state,None);self.app.bus.publish({'type':'mcp_oauth','agent_id':'*','server':sid})
            flow['timer']=asyncio.get_running_loop().call_later(LIFETIME,expire)
            self._pending()[state]=flow
            parts=urlsplit(plan['authorization']['authorization_endpoint']);query=dict(parse_qsl(parts.query,keep_blank_values=True))
            query.update(response_type='code',client_id=registration['client_id'],redirect_uri=plan['redirect_uri'],state=state,code_challenge=code_challenge,code_challenge_method='S256',resource=plan['resource'])
            if requested:query['scope']=requested
            else:query.pop('scope',None)
            self.app.bus.publish({'type':'mcp_oauth','agent_id':'*','server':sid})
            return {'url':urlunsplit((parts.scheme,parts.netloc,parts.path,urlencode(query),'')),'expires_seconds':LIFETIME,'redirect_uri':plan['redirect_uri']}

    async def callback(self,flow,query):
        flow['timer'].cancel()
        if time.monotonic()-flow['created']>LIFETIME:raise failed('Время входа истекло; начни его заново.')
        def one(key):
            values=query.get(key)
            if values is None:return None
            if not isinstance(values,list) or len(values)!=1 or not isinstance(values[0],str) or len(values[0])>8192:raise failed('Неоднозначный ответ OAuth.')
            return values[0]
        metadata=flow['plan']['authorization']
        validate_issuer(metadata['issuer'],one('iss'),metadata['iss_required'])
        # Ошибки сервиса нельзя показывать до проверки издателя; исходные тексты не копируем.
        if one('error') is not None:raise failed('Сервис не завершил вход. Повтори его и проверь выбранного клиента/права.')
        code=one('code')
        if not code:raise failed('Сервис не прислал код входа; начни заново.')
        sid=flow['plan']['sid']
        async with self._lock(sid):
            self._validate_pending(flow)
            result=await self._exchange(flow,{'grant_type':'authorization_code','code':code,'redirect_uri':flow['plan']['redirect_uri'],'code_verifier':flow['verifier']})
            self._save_grant(flow,result)
        self.prepared.pop(flow['plan']['id'],None)
        return {'ok':True,'server':sid}

    def status(self,sid):
        server=self.manager.get(sid);grant=mapping(mapping(self._book().get('servers')).get(sid))
        signature=self.manager.signature(server);bound=bool(grant) and grant.get('connection')==signature
        expires=grant.get('expires_at');expired=isinstance(expires,(int,float)) and expires<=time.time()
        pending=any(flow['oauth'] is self and flow['plan']['sid']==sid for flow in self._pending().values())
        return {'authorized':bound,'configured':bool(grant),'expired':expired,'expires_at':expires,'issuer':grant.get('issuer'),'scope':grant.get('scope',''),
                'can_refresh':bound and bool(grant.get('refresh')),'revision':digest(grant) if grant else None,'pending':pending}

    async def refresh(self,sid,revision,ctx):
        ctx=self.owner(ctx);self.require(sid)
        async with self._lock(sid):
            grant=mapping(mapping(self._book().get('servers')).get(sid))
            if not grant or revision!=digest(grant):raise failed('Вход уже изменился; обнови экран перед повторным обновлением токена.')
            if grant.get('connection')!=self.manager.signature(self.require(sid)):raise failed('Подключение изменилось; начни новый вход вместо обновления прежнего.')
            if not grant.get('refresh'):raise failed('Сервис не выдал refresh_token; нужен новый вход хозяина.')
            plan={'sid':sid,'connection':grant['connection'],'resource':grant['resource'],'authorization':copy.deepcopy(grant['authorization']),'redirect_uri':grant['redirect_uri']}
            secure_url(plan['resource']);secure_url(plan['authorization']['token_endpoint'])
            if origin(plan['resource'])!=origin(self.require(sid)['url']) or origin(plan['authorization']['token_endpoint'])!=origin(plan['authorization']['issuer']):raise failed('Привязка сохранённого входа повреждена; войди заново.')
            registration=self._registration(plan)
            if not registration:raise failed('Регистрация клиента больше не существует; начни вход заново.')
            flow={'plan':plan,'registration':registration,'credentials':self._credential_signature(registration),'ctx':ctx,'scope':grant.get('scope',''),'refresh':True,'grant_revision':revision}
            self._validate_pending(flow)
            value,_=self.app.guard.expand_secrets(grant['refresh'],host=urlsplit(plan['authorization']['issuer']).hostname,tool='mcp-oauth:'+sid,session_id=ctx.session_id)
            result=await self._exchange(flow,{'grant_type':'refresh_token','refresh_token':value,**({'scope':flow['scope']} if flow['scope'] else {})})
            return self._save_grant(flow,result,refresh=True)

    async def forget(self,sid,revision,ctx):
        self.owner(ctx);self.cancel(sid)
        async with self._lock(sid):
            server=self.manager.get(sid);grant=mapping(mapping(self._book().get('servers')).get(sid))
            if not grant or revision!=digest(grant):raise failed('Вход уже изменился; обнови экран перед удалением.')
            with self.app.settings.transaction():
                headers={key:value for key,value in server.get('headers',{}).items() if not (key.lower()=='authorization' and value=='Bearer '+grant.get('access',''))}
                if headers!=server.get('headers',{}):self._set_headers(sid,headers)
                book=self._book();servers=mapping(book.get('servers'));servers.pop(sid,None)
                self.app.settings.replace('mcp_oauth',{**book,'servers':servers})
            self.app.bus.publish({'type':'mcp_oauth','agent_id':'*','server':sid})
            return self.manager.public()

    def cancel(self,sid=None):
        for pid,plan in list(self.prepared.items()):
            if sid is None or plan['sid']==sid:self.prepared.pop(pid,None)
        for state,flow in list(self._pending().items()):
            if flow['oauth'] is self and (sid is None or flow['plan']['sid']==sid):
                flow['timer'].cancel();self._pending().pop(state,None)

    def close(self):self.closed=True;self.cancel()


async def callback(host_app,query):
    values=query.get('state',[])
    if not isinstance(values,list) or len(values)!=1 or not isinstance(values[0],str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}',values[0]):raise failed('Ссылка входа недействительна; начни его из пульта.')
    pending=getattr(host_app,'_mcp_oauth_pending',{})
    flow=pending.pop(values[0],None)
    if not flow or not hmac.compare_digest(flow['state'],values[0]):raise failed('Вход закрыт, истёк или уже использован; начни заново из пульта.')
    try:return await flow['oauth'].callback(flow,query)
    finally:flow['oauth'].app.bus.publish({'type':'mcp_oauth','agent_id':'*','server':flow['plan']['sid']})
