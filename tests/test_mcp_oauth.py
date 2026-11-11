"""Векторы PKCE, discovery, challenge и точная проверка издателя OAuth."""
import unittest
import asyncio
import ssl
import base64
import hashlib
from pathlib import Path
from urllib.parse import parse_qs,urlsplit,urlencode
from unittest import mock

from bender.core.errors import HumanError
from bender.mcp.oauth import authorization_metadata,challenge,metadata_urls,pkce,resource_metadata,scopes,secure_url,validate_issuer
from bender.mcp.oauth_flow import OAuth,callback
from bender.mcp.oauth_tokens import public_document
from bender.core.app import App
from bender.core.settings import NeedsOwner
from bender.net.http_server import HttpServer,Router,JSONResponse,Response
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase,async_test,owner_web,owner_headers


class OAuthValidationTest(unittest.TestCase):
    def test_pkce_rfc7636_vector_and_issuer_rfc9207(self):
        verifier='dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk'
        with mock.patch('bender.mcp.oauth.secrets.token_urlsafe',return_value=verifier):
            self.assertEqual(pkce(),(verifier,'E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM'))
        validate_issuer('https://auth.example/a','https://auth.example/a',True)
        validate_issuer('https://auth.example/a',None,False)
        for received,required in ((None,True),('https://AUTH.example/a',False),('https://auth.example/a/',True),('https://other.example',False)):
            with self.assertRaises(HumanError):validate_issuer('https://auth.example/a',received,required)

    def test_challenge_and_well_known_order(self):
        self.assertEqual(challenge('Basic realm="a,b", Bearer realm="c", resource_metadata="https://mcp.example/.well-known/oauth-protected-resource", scope="read write"'),
                         {'realm':'c','resource_metadata':'https://mcp.example/.well-known/oauth-protected-resource','scope':'read write'})
        for value in ('Bearer scope="a", scope="b"','Bearer realm="a", Bearer realm="b"'):
            with self.assertRaises(HumanError):challenge(value)
        self.assertEqual(challenge('Basic realm="a"'),{})
        self.assertEqual(metadata_urls('https://auth.example/tenant'),['https://auth.example/.well-known/oauth-authorization-server/tenant','https://auth.example/.well-known/openid-configuration/tenant','https://auth.example/tenant/.well-known/openid-configuration'])
        self.assertEqual(metadata_urls('https://mcp.example/path/mcp',resource=True),['https://mcp.example/.well-known/oauth-protected-resource/path/mcp','https://mcp.example/.well-known/oauth-protected-resource'])
        self.assertEqual(scopes('read write read'),'read write')
        for value in ('read\nwrite','только_чтение','read"key'):
            with self.assertRaises(HumanError):scopes(value)

    def test_resource_and_authorization_server_binding(self):
        resource={'resource':'https://mcp.example/mcp','authorization_servers':['https://auth.example/a'],'scopes_supported':['read','write']}
        self.assertEqual(resource_metadata(resource,'https://mcp.example/mcp')['issuers'],['https://auth.example/a'])
        with self.assertRaises(HumanError):resource_metadata(resource,'https://other.example/mcp')
        metadata={'issuer':'https://auth.example/a','authorization_endpoint':'https://auth.example/authorize','token_endpoint':'https://auth.example/token','code_challenge_methods_supported':['S256'],'authorization_response_iss_parameter_supported':True,'token_endpoint_auth_methods_supported':['none','client_secret_basic']}
        self.assertTrue(authorization_metadata(metadata,'https://auth.example/a')['iss_required'])
        for changes in ({'issuer':'https://other.example'},{'token_endpoint':'https://other.example/token'},
                        {'code_challenge_methods_supported':None},{'code_challenge_methods_supported':'S256'},{'code_challenge_methods_supported':['plain']}):
            with self.assertRaises(HumanError):authorization_metadata({**metadata,**changes},'https://auth.example/a')
        for url in ('http://auth.example','https://u:p@auth.example','https://auth.example/#f','https://auth.example:0','https://auth.example/\npath','https://auth.example:99999'):
            with self.assertRaises(HumanError):secure_url(url)


class OAuthFlowTest(TempDirCase):
    async def setup(self):
        fixture=Path(__file__).parent/'fixtures'
        self.trust=ssl.create_default_context(cafile=str(fixture/'mcp-oauth-cert.pem'))
        serving=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);serving.load_cert_chain(str(fixture/'mcp-oauth-cert.pem'),str(fixture/'mcp-oauth-key.pem'))
        self.app=App(self.tmp/'data');self.seen=[];self.meta_changes={};self.codes={};self.token_posts=[];self.registrations=[];router=Router()
        @router.route('GET','/mcp')
        async def resource(req):
            self.seen.append((req.path,req.headers.get('authorization')))
            return Response(b'',401,{'WWW-Authenticate':'Bearer resource_metadata="'+self.base+'/.well-known/oauth-protected-resource/mcp", scope="read"'})
        @router.route('GET','/.well-known/oauth-protected-resource/mcp')
        async def metadata(req):
            self.seen.append((req.path,req.headers.get('authorization')))
            return JSONResponse({'resource':self.base+'/mcp','authorization_servers':[self.base+'/issuer'],'scopes_supported':['read','write']})
        @router.route('GET','/.well-known/oauth-authorization-server/issuer')
        async def issuer(req):
            self.seen.append((req.path,req.headers.get('authorization')))
            return JSONResponse({'issuer':self.base+'/issuer','authorization_endpoint':self.base+'/authorize','token_endpoint':self.base+'/token','registration_endpoint':self.base+'/register','code_challenge_methods_supported':['S256'],'authorization_response_iss_parameter_supported':True,'token_endpoint_auth_methods_supported':['none'],**self.meta_changes})
        @router.route('POST','/register')
        async def register(req):
            data=req.json();self.registrations.append(data)
            return JSONResponse({'client_id':'registered-'+str(len(self.registrations)),'token_endpoint_auth_method':data['token_endpoint_auth_method']},201)
        @router.route('GET','/authorize')
        async def authorize(req):
            client=req.q('client_id')
            if client.startswith('https:'):
                response=await self.app.http.get(client);document=await response.json()
                if document.get('client_id')!=client or req.q('redirect_uri') not in document.get('redirect_uris',[]):return Response(b'',400)
            code='test-code-'+str(len(self.codes)+1);self.codes[code]={key:req.q(key) for key in ('code_challenge','client_id','resource','redirect_uri')}
            params={'code':code,'state':req.q('state'),'iss':self.base+'/issuer'}
            return Response(b'',302,{'Location':req.q('redirect_uri')+'?'+urlencode(params)})
        @router.route('GET','/mcp-client/{name}')
        async def client_document(req):
            document=public_document(self.app,req.params['name'].removesuffix('.json'),req.host)
            return JSONResponse(document) if document else Response(b'',404)
        @router.route('POST','/token')
        async def token(req):
            data=parse_qs(req.body.decode());self.token_posts.append((data,req.headers.get('authorization')))
            if data.get('grant_type')==['authorization_code']:
                original=self.codes.pop(data.get('code',[''])[0],None)
                verifier=data.get('code_verifier',[''])[0]
                encoded=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip('=')
                if not original or original['code_challenge']!=encoded or original['resource']!=data.get('resource',[''])[0] or original['redirect_uri']!=data.get('redirect_uri',[''])[0]:return JSONResponse({'error':'invalid_grant'},400)
            elif data.get('grant_type')!=['refresh_token']:return JSONResponse({'error':'unsupported_grant_type'},400)
            number=len(self.token_posts)
            return JSONResponse({'token_type':'Bearer','access_token':'test-access-'+str(number),'refresh_token':'test-refresh-'+str(number),'expires_in':3600,'scope':'read'})
        self.server=HttpServer(router)
        self.server._server=await asyncio.start_server(self.server._accept,'127.0.0.1',0,ssl=serving)
        self.server.host,self.server.port=self.server._server.sockets[0].getsockname()[:2]
        self.base=f'https://127.0.0.1:{self.server.port}'
        self.app.settings.set('mcp',{'oauth_enabled':True},by='owner')
        await self.app.mcp.save({'id':'remote','transport':'http','url':self.base+'/mcp','enabled':True,'headers':{'Authorization':'Bearer не-передавать-в-discovery'}})
        self.oauth=self.app.mcp.oauth;self.ctx=ToolContext(self.app,actor='owner',session_id='oauth-test')

    async def finish(self):await self.app.close();await self.server.stop()

    async def prepare(self,*,origin='http://127.0.0.1:8770'):
        first=await self.oauth.discover('remote',self.ctx)
        return await self.oauth.choose_issuer(first['id'],'remote',self.base+'/issuer',origin,self.ctx)

    async def login(self,plan,*,mode='pre',**kwargs):
        begun=await self.oauth.authorize(plan['id'],'remote',{'review':plan['review'],'mode':mode,'client_id':'client-public','auth_method':'none',**kwargs},self.ctx)
        response=await self.app.http.get(begun['url'])
        async with response:
            self.assertEqual(response.status,302)
            location=response.headers.get('location')
        return parse_qs(urlsplit(location).query)

    @async_test
    async def test_https_discovery_owner_choice_without_credentials(self):
        await self.setup()
        try:
            with mock.patch('bender.net.http_client._ssl_default',self.trust):
                first=await self.oauth.discover('remote',self.ctx)
                self.assertEqual(first['stage'],'issuer')
                self.assertEqual([path for path,_ in self.seen],['/mcp','/.well-known/oauth-protected-resource/mcp'])
                ready=await self.oauth.choose_issuer(first['id'],'remote',self.base+'/issuer','http://127.0.0.1:8770',self.ctx)
                self.assertEqual(ready['stage'],'ready');self.assertEqual(ready['scope'],'read')
                self.assertEqual(ready['redirect_uri'],'http://127.0.0.1:8770/api/mcp/oauth/callback')
                self.assertTrue(all(auth is None for _,auth in self.seen))
                self.assertEqual(len(self.seen),3)
        finally:await self.finish()

    @async_test
    async def test_https_ca_is_required_and_changed_server_rejected(self):
        await self.setup()
        try:
            with mock.patch('bender.net.http_client._ssl_default',None):
                with self.assertRaises(HumanError):await self.oauth.discover('remote',self.ctx)
            self.assertEqual(self.seen,[])
            with mock.patch('bender.net.http_client._ssl_default',self.trust):
                first=await self.oauth.discover('remote',self.ctx)
                with self.assertRaises(HumanError):await self.oauth.choose_issuer(first['id'],'remote','https://other.invalid','http://127.0.0.1:8770',self.ctx)
                await self.app.mcp.save({'id':'remote','url':self.base+'/other'})
                with self.assertRaises(HumanError):await self.oauth.choose_issuer(first['id'],'remote',self.base+'/issuer','http://127.0.0.1:8770',self.ctx)
                self.assertEqual(len(self.seen),2)
        finally:await self.finish()

    @async_test
    async def test_owner_only_no_insecure_redirect_and_metadata_checks(self):
        await self.setup()
        try:
            with self.assertRaises(HumanError):await self.oauth.discover('remote',ToolContext(self.app))
            with self.assertRaises(HumanError):self.app.settings.set('mcp_oauth',{'servers':{}},by='agent')
            self.app.settings.set('mcp',{'oauth_enabled':False})
            with self.assertRaises(NeedsOwner):self.app.settings.set('mcp',{'oauth_enabled':True},by='agent')
            with self.assertRaises(HumanError):await self.oauth.discover('remote',self.ctx)
            self.app.settings.set('mcp',{'oauth_enabled':True},by='owner')
            with mock.patch('bender.net.http_client._ssl_default',self.trust):
                first=await self.oauth.discover('remote',self.ctx)
                self.meta_changes={'code_challenge_methods_supported':['plain']}
                with self.assertRaises(HumanError):await self.oauth.choose_issuer(first['id'],'remote',self.base+'/issuer','http://127.0.0.1:8770',self.ctx)
                self.meta_changes={}
                with self.assertRaises(HumanError):await self.oauth.choose_issuer(first['id'],'remote',self.base+'/issuer','http://192.168.1.2:8770',self.ctx)
        finally:await self.finish()

    @async_test
    async def test_code_pkce_and_resource_once_tokens_only_in_vault(self):
        await self.setup()
        try:
            with mock.patch('bender.net.http_client._ssl_default',self.trust):
                plan=await self.prepare();query=await self.login(plan)
                result=await callback(self.app,query)
                self.assertTrue(result['ok']);self.assertEqual(len(self.token_posts),1)
                body,_=self.token_posts[0];self.assertEqual(body['resource'],[self.base+'/mcp'])
                self.assertEqual(body['client_id'],['client-public']);self.assertIn('code_verifier',body)
                with self.assertRaises(HumanError):await callback(self.app,query)
                self.assertEqual(len(self.token_posts),1)
                stored=self.app.settings.group('mcp_oauth');self.assertEqual(stored['servers']['remote']['issuer'],self.base+'/issuer')
                raw=str(stored)+str(self.app.settings.group('mcp'))
                self.assertNotIn('test-access-1',raw);self.assertNotIn('test-refresh-1',raw)
                self.assertEqual(self.app.dirs.secrets.stat().st_mode&0o777,0o600)
                server=self.app.mcp.get('remote');value,_=self.app.guard.expand_secrets(server['headers']['Authorization'],host='127.0.0.1',tool='mcp:remote')
                self.assertEqual(value,'Bearer test-access-1')
                self.assertNotIn('test-access-1',self.app.guard.scrub('test-access-1'))
                self.assertTrue(self.oauth.status('remote')['can_refresh'])
        finally:self.oauth.close();await self.finish()

    @async_test
    async def test_wrong_or_missing_issuer_and_changed_config_never_exchange_code(self):
        await self.setup()
        try:
            with mock.patch('bender.net.http_client._ssl_default',self.trust):
                for alteration in ({'iss':[self.base+'/OTHER']},{'iss':None}):
                    plan=await self.prepare();query=await self.login(plan)
                    if alteration['iss'] is None:query.pop('iss')
                    else:query.update(alteration)
                    with self.assertRaises(HumanError):await callback(self.app,query)
                    self.assertEqual(self.token_posts,[])
                plan=await self.prepare();query=await self.login(plan)
                await self.app.mcp.save({'id':'remote','headers':{'Authorization':'Bearer изменено-хозяином'}})
                with self.assertRaises(HumanError):await callback(self.app,query)
                self.assertEqual(self.token_posts,[])
        finally:self.oauth.close();await self.finish()

    @async_test
    async def test_refresh_is_explicit_rotates_once_and_forget_is_local(self):
        await self.setup()
        try:
            with mock.patch('bender.net.http_client._ssl_default',self.trust):
                query=await self.login(await self.prepare());await callback(self.app,query)
                previous=self.oauth.status('remote')['revision']
                results=await asyncio.gather(self.oauth.refresh('remote',previous,self.ctx),self.oauth.refresh('remote',previous,self.ctx),return_exceptions=True)
                self.assertEqual(sum(isinstance(result,HumanError) for result in results),1)
                self.assertEqual(len(self.token_posts),2)
                self.assertEqual(self.token_posts[1][0]['refresh_token'],['test-refresh-1'])
                self.assertEqual(self.token_posts[1][0]['resource'],[self.base+'/mcp'])
                with self.assertRaises(HumanError):await self.oauth.refresh('remote',previous,self.ctx)
                self.assertEqual(len(self.token_posts),2)
                now=self.oauth.status('remote')['revision']
                await self.oauth.forget('remote',now,self.ctx)
                self.assertFalse(self.oauth.status('remote')['configured'])
                self.assertNotIn('Authorization',self.app.mcp.get('remote')['headers'])
                self.assertEqual(len(self.token_posts),2)
        finally:self.oauth.close();await self.finish()

    @async_test
    async def test_dcr_and_public_metadata_without_credentials_in_document(self):
        await self.setup()
        try:
            with mock.patch('bender.net.http_client._ssl_default',self.trust):
                query=await self.login(await self.prepare(),mode='register');await callback(self.app,query)
                self.assertEqual(len(self.registrations),1)
                self.assertEqual(self.registrations[0]['token_endpoint_auth_method'],'none')
                self.meta_changes={'client_id_metadata_document_supported':True}
                plan=await self.prepare(origin=self.base)
                query=await self.login(plan,mode='metadata_local');await callback(self.app,query)
                book=self.app.settings.group('mcp_oauth');key=next(iter(book['documents']))
                book['documents'][key]['private_future']='НЕ-ПУБЛИКОВАТЬ';self.app.settings.replace('mcp_oauth',book)
                public=public_document(self.app,key,urlsplit(self.base).netloc)
                self.assertEqual(public['client_id'],self.base+'/mcp-client/'+key+'.json')
                self.assertNotIn('private_future',public)
                self.assertIsNone(public_document(self.app,key,'other.invalid'))
                self.assertEqual(len(self.registrations),1)
        finally:self.oauth.close();await self.finish()

    @async_test
    async def test_scoped_secret_reuse_does_not_expand_existing_acl(self):
        await self.setup()
        try:
            self.app.secrets.set('old-reference','same-test-value',allow={'hosts':['elsewhere.invalid'],'tools':[]})
            value=self.oauth._secret('same-test-value','127.0.0.1','access')
            self.assertEqual(self.app.secrets.allow('old-reference')['hosts'],['elsewhere.invalid'])
            self.assertNotEqual(value,'{{secret:old-reference}}')
            self.assertEqual(self.oauth._secret('same-test-value','127.0.0.1','access'),value)
        finally:self.oauth.close();await self.finish()

    @async_test
    async def test_owner_api_and_cookie_free_one_use_callback(self):
        await self.setup();web=HttpServer(owner_web(self.app,allowed_hosts={'127.0.0.1','localhost'}));await web.start('127.0.0.1',0)
        try:
            base=f'http://127.0.0.1:{web.port}';api=base+'/api/mcp/remote/oauth/'
            headers=owner_headers(**{'X-Bender':'1'})
            response=await self.app.http.post(api+'discover',json={},headers={'X-Bender':'1'})
            self.assertEqual(response.status,401);await response.read();self.assertEqual(self.seen,[])
            with mock.patch('bender.net.http_client._ssl_default',self.trust):
                response=await self.app.http.post(api+'discover',json={},headers=headers);plan=await response.json();self.assertTrue(response.ok,plan)
                response=await self.app.http.post(api+'issuer',json={'flow':plan['id'],'issuer':self.base+'/issuer','return_origin':'https://elsewhere.invalid'},headers=headers)
                self.assertEqual(response.status,400);await response.read()
                response=await self.app.http.post(api+'issuer',json={'flow':plan['id'],'issuer':self.base+'/issuer','return_origin':base},headers=headers)
                plan=await response.json();self.assertTrue(response.ok,plan)
                response=await self.app.http.post(api+'authorize',json={'flow':plan['id'],'review':plan['review'],'mode':'pre','client_id':'client-public','auth_method':'none'},headers=headers)
                entry=await response.json();self.assertTrue(response.ok,entry)
                response=await self.app.http.get(entry['url']);location=response.headers.get('location');await response.read()
                response=await self.app.http.get(location)
                page=await response.text();self.assertEqual(response.status,200,page)
                self.assertIn('Вход MCP сохранён',page);self.assertIn('no-store',response.headers.get('cache-control'))
                self.assertNotIn('bender-test-owner-token',page)
                response=await self.app.http.get(location);self.assertEqual(response.status,400);await response.read()
                self.assertEqual(len(self.token_posts),1)
                response=await self.app.http.get(base+'/api/mcp');self.assertEqual(response.status,401);await response.read()
        finally:await web.stop();self.oauth.close();await self.finish()

    @async_test
    async def test_public_document_is_anonymous_but_other_owner_data_is_not(self):
        await self.setup();web=HttpServer(owner_web(self.app,allowed_hosts={'127.0.0.1','localhost'}));await web.start('127.0.0.1',0)
        try:
            with mock.patch('bender.net.http_client._ssl_default',self.trust):
                self.meta_changes={'client_id_metadata_document_supported':True}
                plan=await self.prepare(origin=f'https://127.0.0.1:{web.port}')
                identifier=self.oauth._publish_client_document(self.oauth.plan(plan['id'],'remote'))
                path=urlsplit(identifier).path
                response=await self.app.http.get(f'http://127.0.0.1:{web.port}'+path)
                document=await response.json();self.assertEqual(response.status,200)
                self.assertEqual(set(document),{'client_id','client_name','redirect_uris','grant_types','response_types','token_endpoint_auth_method'})
                response=await self.app.http.get(f'http://127.0.0.1:{web.port}/api/mcp/inputs/fake');self.assertEqual(response.status,401);await response.read()
                self.app.settings.set('mcp',{'oauth_enabled':False})
                response=await self.app.http.get(f'http://127.0.0.1:{web.port}'+path);self.assertEqual(response.status,404);await response.read()
        finally:await web.stop();self.oauth.close();await self.finish()

    @async_test
    async def test_confidential_client_basic_and_changed_secret_never_swapped(self):
        await self.setup()
        try:
            with mock.patch('bender.net.http_client._ssl_default',self.trust):
                self.meta_changes={'token_endpoint_auth_methods_supported':['none','client_secret_basic','client_secret_post']}
                query=await self.login(await self.prepare(),client_id='confidential',auth_method='client_secret_basic',client_secret='секрет-клиента')
                await callback(self.app,query)
                body,header=self.token_posts[0]
                self.assertNotIn('client_secret',body)
                self.assertTrue(header.startswith('Basic '))
                decoded=base64.b64decode(header.split(' ',1)[1]).decode()
                self.assertIn('confidential:',decoded);self.assertIn('%D1',decoded)
                self.assertNotIn('секрет-клиента',str(self.app.settings.group('mcp_oauth')))
                plan=await self.prepare();query=await self.login(plan,mode='existing')
                registration=self.oauth._registration(self.oauth.plan(plan['id'],'remote'))
                name=registration['secret'][9:-2]
                self.app.secrets.set(name,'другой-секрет')
                with self.assertRaises(HumanError):await callback(self.app,query)
                self.assertEqual(len(self.token_posts),1)
        finally:self.oauth.close();await self.finish()

    @async_test
    async def test_callback_keeps_selected_agent_after_request_context_changed(self):
        await self.setup();web=HttpServer(owner_web(self.app,allowed_hosts={'127.0.0.1','localhost'}));await web.start('127.0.0.1',0)
        try:
            self.app.agents.create('Другой',agent_id='other',awaken=False)
            other=self.app.agents.get('other');base=f'http://127.0.0.1:{web.port}';api=base+'/api/mcp/remote/oauth/'
            headers=owner_headers(**{'X-Bender':'1','X-Bender-Agent':'other'})
            with mock.patch('bender.net.http_client._ssl_default',self.trust):
                response=await self.app.http.post(api+'discover',json={},headers=headers);plan=await response.json();self.assertTrue(response.ok,plan)
                response=await self.app.http.post(api+'issuer',json={'flow':plan['id'],'issuer':self.base+'/issuer','return_origin':base},headers=headers);plan=await response.json();self.assertTrue(response.ok,plan)
                response=await self.app.http.post(api+'authorize',json={'flow':plan['id'],'review':plan['review'],'mode':'pre','client_id':'client-public','auth_method':'none'},headers=headers)
                entry=await response.json();self.assertTrue(response.ok,entry)
                response=await self.app.http.get(entry['url']);location=response.headers.get('location');await response.read()
                response=await self.app.http.get(location);self.assertEqual(response.status,200,await response.text())
                records=[record for record in other.journal.recent() if record['kind']=='mcp' and 'токен сохранён' in record['summary']]
                self.assertEqual(len(records),1);self.assertEqual(records[0]['data']['agent_id'],'other')
                self.assertEqual(len(self.token_posts),1)
        finally:await web.stop();self.oauth.close();await self.finish()
