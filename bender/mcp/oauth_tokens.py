"""Регистрации OAuth разделены по издателю; токены живут только в существующем сейфе."""
import base64
import copy
import json
import math
import re
import time
from urllib.parse import parse_qsl,quote,urlencode,urlsplit,urlunsplit

from ..guard import SECRET_REF
from ..core.errors import HumanError
from ..core.ids import new_id
from .manager import digest
from .oauth import failed,scopes,secure_url


def mapping(value):return dict(value) if isinstance(value,dict) else {}


def text(value,label,*,empty=False,limit=32768):
    if not isinstance(value,str) or (not value and not empty) or len(value)>limit:raise failed('Неверное значение '+label+'.')
    try:value.encode('utf-8')
    except UnicodeError:raise failed('Неверный UTF-8 в '+label+'.') from None
    return value


class Tokens:
    def _book(self):return self.app.settings.group('mcp_oauth')

    def _registration_key(self,plan):return digest({'issuer':plan['authorization']['issuer'],'redirect_uri':plan['redirect_uri']})

    def _registration(self,plan):
        result=mapping(mapping(self._book().get('registrations')).get(self._registration_key(plan)))
        return result if result.get('issuer')==plan['authorization']['issuer'] and result.get('redirect_uri')==plan['redirect_uri'] else {}

    def _set_headers(self,sid,headers):
        # Вызывается внутри Settings.transaction; журнал менеджера пишется после COMMIT.
        servers=self.manager.servers()
        self.app.settings.set('mcp',{'servers':[{**server,'headers':headers} if server.get('id')==sid else server for server in servers]},by='owner')

    def _credential_signature(self,registration):
        names=[match.group(1) for match in SECRET_REF.finditer(registration.get('secret',''))]
        return digest({'registration':registration,'secrets':{name:(self.app.secrets.get(name),self.app.secrets.allow(name)) for name in names}})

    def _secret(self,value,host,kind):
        text(value,kind)
        # Не расширяем права уже используемого ключа до записи новой ссылки в SQLite.
        for name in self.app.secrets.names():
            if self.app.secrets.get(name)==value and host in (self.app.secrets.allow(name) or {}).get('hosts',[]):return '{{secret:'+name+'}}'
        name='mcp-oauth:'+digest(host)[:12]+':'+kind+':'+new_id()
        self.app.secrets.set(name,value,note='MCP OAuth: '+kind,allow={'hosts':[host],'tools':[]})
        return '{{secret:'+name+'}}'

    async def _register(self,plan,options,ctx):
        mode=options.get('mode');metadata=plan['authorization'];issuer=metadata['issuer'];key=self._registration_key(plan)
        old=self._registration(plan)
        if mode=='existing':
            if not old:raise failed('Для этого издателя/адреса возврата нет сохранённой регистрации.')
            return old
        identifier=options.get('client_id','');secret=options.get('client_secret','')
        method='none'
        if mode=='metadata_local':
            if 'none' not in metadata['auth_methods']:raise failed('Для публичного документа нужен способ проверки клиента none.')
            identifier=self._publish_client_document(plan)
        elif mode=='metadata_url':
            if not metadata['client_metadata_supported']:raise failed('Сервер входа не объявил поддержку Client ID Metadata Documents.')
            secure_url(identifier)
            if urlsplit(identifier).path in ('','/'):raise failed('client_id должен указывать на HTTPS-документ, а не корень сайта.')
            document=await self.json_request('GET',identifier)
            redirects=document.get('redirect_uris')
            if not isinstance(redirects,list) or any(not isinstance(uri,str) for uri in redirects) or not isinstance(document.get('client_name'),str) or not document['client_name'] or document.get('client_id')!=identifier or plan['redirect_uri'] not in redirects or document.get('token_endpoint_auth_method','none')!='none':raise failed('Документ клиента не совпал с адресом/client_id/возвратом либо требует неподдерживаемую подпись.')
        elif mode=='register':
            if not metadata.get('registration_endpoint'):raise failed('Сервер не объявил автоматическую регистрацию; укажи client_id выданного им клиента.')
            method=next((value for value in ('none','client_secret_basic','client_secret_post') if value in metadata['auth_methods']),None)
            if method is None:raise failed('Сервер требует неподдерживаемую подпись клиента; нужен публичный клиент с PKCE или client_secret.')
            request={'client_name':'Бендер','redirect_uris':[plan['redirect_uri']],'grant_types':['authorization_code','refresh_token'],'response_types':['code'],'token_endpoint_auth_method':method}
            result=await self.json_request('POST',metadata['registration_endpoint'],body=json.dumps(request).encode(),headers={'Content-Type':'application/json'})
            identifier=result.get('client_id');secret=result.get('client_secret','')
            if result.get('token_endpoint_auth_method','client_secret_basic')!=method:raise failed('Сервис зарегистрировал другой способ проверки клиента; проверь регистрацию у сервиса.')
        elif mode=='pre':
            method=options.get('auth_method','none')
        else:raise failed('Выбери сохранённого, выданного сервисом клиента, документ HTTPS или автоматическую регистрацию.')
        text(identifier,'client_id',limit=8192)
        if self.app.guard.scrub(identifier)!=identifier:raise failed('client_id — публичное имя; в нём найден секрет. Проверь поля входа.')
        if method not in ('none','client_secret_basic','client_secret_post') or method not in metadata['auth_methods']:raise failed('Выбранный способ проверки клиента не поддерживается издателем.')
        secret_ref=''
        if method!='none':
            text(secret,'секрет клиента')
            if secret==identifier:raise failed('client_id совпал с секретом; проверь ответ сервиса.')
            secret_ref=self._secret(secret,urlsplit(issuer).hostname,'client')
        registration={**old,'issuer':issuer,'redirect_uri':plan['redirect_uri'],'client_id':identifier,'method':method,'secret':secret_ref,'source':mode}
        with self.app.settings.transaction():
            self.plan(plan['id'],plan['sid'])
            book=self._book();entries=mapping(book.get('registrations'));entries[key]=registration
            self.app.settings.replace('mcp_oauth',{**book,'registrations':entries})
        return registration

    def _publish_client_document(self,plan):
        metadata=plan['authorization']
        if not metadata['client_metadata_supported']:raise failed('Сервис не поддерживает HTTPS-документ клиента.')
        redirect=secure_url(plan['redirect_uri']);parts=urlsplit(redirect)
        base=urlunsplit((parts.scheme,parts.netloc,'','',''));key=digest({'redirect_uri':redirect})[:32]
        identifier=base+'/mcp-client/'+key+'.json'
        document={'client_id':identifier,'client_name':'Бендер','redirect_uris':[redirect],'grant_types':['authorization_code','refresh_token'],'response_types':['code'],'token_endpoint_auth_method':'none'}
        with self.app.settings.transaction():
            book=self._book();documents=mapping(book.get('documents'));documents[key]=document
            self.app.settings.replace('mcp_oauth',{**book,'documents':documents})
        return identifier

    async def _exchange(self,flow,params):
        plan=flow['plan'];ctx=self.owner(flow['ctx']);sid=plan['sid']
        self._validate_pending(flow)
        await ctx.check('mcp.connect',sid+'/oauth/token/'+digest({'issuer':plan['authorization']['issuer'],'resource':plan['resource']}),'получить токен MCP '+sid,detail={'issuer':plan['authorization']['issuer'],'resource':plan['resource']})
        self._validate_pending(flow)
        registration=flow['registration'];method=registration['method'];headers={'Content-Type':'application/x-www-form-urlencoded'}
        body={**params,'resource':plan['resource']}
        if method=='none':body['client_id']=registration['client_id']
        else:
            secret,_=self.app.guard.expand_secrets(registration['secret'],host=urlsplit(plan['authorization']['issuer']).hostname,tool='mcp-oauth:'+sid,session_id=ctx.session_id)
            self._validate_pending(flow)
            if method=='client_secret_basic':
                pair=quote(registration['client_id'],safe='')+':'+quote(secret,safe='')
                headers['Authorization']='Basic '+base64.b64encode(pair.encode('ascii')).decode('ascii')
            elif method=='client_secret_post':body.update(client_id=registration['client_id'],client_secret=secret)
            else:raise failed('Неподдерживаемый способ проверки клиента.')
        return await self.json_request('POST',plan['authorization']['token_endpoint'],body=urlencode(body).encode(),headers=headers)

    def _save_grant(self,flow,result,*,refresh=False):
        self._validate_pending(flow)
        if not isinstance(result.get('token_type'),str) or result['token_type'].lower()!='bearer':raise failed('Сервис не вернул Bearer-токен.')
        access=text(result.get('access_token'),'access_token')
        if not re.fullmatch(r'[A-Za-z0-9\-._~+/]+=*',access):raise failed('Сервис вернул недопустимый Bearer-токен.')
        expires=result.get('expires_in');expires_at=None
        if expires is not None:
            if type(expires) not in (int,float) or not 0<expires<=315360000 or not math.isfinite(expires):raise failed('Сервис вернул неверный срок действия токена.')
            expires_at=time.time()+expires
        scope=scopes(result.get('scope',flow['scope']))
        plan=flow['plan'];sid=plan['sid'];issuer=plan['authorization']['issuer']
        refresh_value=result.get('refresh_token')
        if refresh_value is not None:text(refresh_value,'refresh_token')
        access_ref=self._secret(access,urlsplit(plan['resource']).hostname,'access')
        previous=mapping(mapping(self._book().get('servers')).get(sid))
        refresh_ref=self._secret(refresh_value,urlsplit(issuer).hostname,'refresh') if refresh_value else (previous.get('refresh','') if refresh else '')
        with self.app.settings.transaction():
            self._validate_pending(flow);server=self.require(sid)
            headers={key:value for key,value in server.get('headers',{}).items() if key.lower()!='authorization'}
            headers['Authorization']='Bearer '+access_ref
            self._set_headers(sid,headers)
            book=self._book();servers=mapping(book.get('servers'))
            servers[sid]={**mapping(servers.get(sid)),'issuer':issuer,'resource':plan['resource'],'authorization':copy.deepcopy(plan['authorization']),'access':access_ref,'updated_at':time.time(),
                          'registration_key':self._registration_key(plan),'redirect_uri':plan['redirect_uri'],'scope':scope,'refresh':refresh_ref,
                          'expires_at':expires_at,'connection':self.manager.signature(self.manager.get(sid))}
            self.app.settings.replace('mcp_oauth',{**book,'servers':servers})
        self.app.journal.write('mcp','OAuth MCP '+sid+': токен сохранён; определения требуют просмотра',actor='owner')
        self.app.bus.publish({'type':'mcp_oauth','agent_id':'*','server':sid})
        return self.manager.public()


def public_document(host_app,key,host):
    """Белый список полей: неизвестные/private поля словаря наружу не попадают."""
    if host_app.safe or not host_app.settings.get('mcp','enabled',True) or not host_app.settings.get('mcp','oauth_enabled',False):return None
    documents=mapping(host_app.settings.group('mcp_oauth').get('documents'));document=mapping(documents.get(key))
    if not document or not isinstance(document.get('client_id'),str):return None
    try:parts=urlsplit(secure_url(document['client_id']))
    except HumanError:return None
    if parts.netloc.lower()!=host.lower():return None
    return {name:copy.deepcopy(document[name]) for name in ('client_id','client_name','redirect_uris','grant_types','response_types','token_endpoint_auth_method') if name in document}
