"""OAuth MCP: действия только хозяину; два узких публичных GET без куки пульта."""
import html
import re
from urllib.parse import urlsplit

from ..core.errors import HumanError
from ..net.http_server import JSONResponse,Response
from ..tools.registry import ToolContext


def routes(router,app,J,body):
    def context(manager):return ToolContext(manager.app,actor='owner',session_id='mcp-oauth')

    @router.route('POST','/api/mcp/{sid}/oauth/discover')
    @J
    async def discover(req):
        manager=app.mcp
        return await manager.oauth.discover(req.params['sid'],context(manager))

    @router.route('POST','/api/mcp/{sid}/oauth/resource')
    @J
    async def resource(req):
        manager=app.mcp
        return await manager.oauth.read_resource(body(req).get('flow'),req.params['sid'],context(manager))

    @router.route('POST','/api/mcp/{sid}/oauth/issuer')
    @J
    async def issuer(req):
        manager=app.mcp;data=body(req);value=data.get('return_origin')
        try:
            if not isinstance(value,str) or urlsplit(value).netloc.lower()!=req.host.lower():raise ValueError()
        except ValueError:raise HumanError('mcp_oauth','Адрес возврата не принадлежит этому пульту','Открой пульт по localhost/SSH-туннелю или HTTPS.') from None
        return await manager.oauth.choose_issuer(data.get('flow'),req.params['sid'],data.get('issuer'),value,context(manager))

    @router.route('POST','/api/mcp/{sid}/oauth/authorize')
    @J
    async def authorize(req):
        manager=app.mcp;data=body(req)
        return await manager.oauth.authorize(data.get('flow'),req.params['sid'],data,context(manager))

    @router.route('POST','/api/mcp/{sid}/oauth/refresh')
    @J
    async def refresh(req):
        manager=app.mcp
        return await manager.oauth.refresh(req.params['sid'],body(req).get('revision'),context(manager))

    @router.route('POST','/api/mcp/{sid}/oauth/forget')
    @J
    async def forget(req):
        manager=app.mcp
        return await manager.oauth.forget(req.params['sid'],body(req).get('revision'),context(manager))

    @router.route('POST','/api/mcp/{sid}/oauth/cancel')
    @J
    async def cancel(req):
        manager=app.mcp;manager.get(req.params['sid']);manager.oauth.cancel(req.params['sid'])
        return manager.public()


async def public(host_app,req):
    """Вызывается после проверки Host; callback авторизован одноразовым state+PKCE."""
    headers={'Cache-Control':'no-store','Referrer-Policy':'no-referrer','Content-Security-Policy':"default-src 'none'; frame-ancestors 'none'; form-action 'none'",'X-Content-Type-Options':'nosniff'}
    if req.path.startswith('/mcp-client/'):
        if host_app.safe or not host_app.settings.get('mcp','enabled',True) or not host_app.settings.get('mcp','oauth_enabled',False):return Response(b'',404,headers)
        from ..mcp.oauth_tokens import public_document
        match=re.fullmatch(r'/mcp-client/([0-9a-f]{32})\.json',req.path)
        document=public_document(host_app,match[1],req.host) if match else None
        return JSONResponse(document,headers=headers) if document else Response(b'',404,headers)
    try:
        if host_app.safe or not host_app.settings.get('mcp','oauth_enabled',False):raise HumanError('mcp_oauth','Вход OAuth выключен','Начни его заново из пульта.')
        from ..mcp.oauth_flow import callback
        await callback(host_app,req.query)
        title='Вход MCP сохранён';message='Вернись в MCP, заново обнаружь и просмотри определения. Вызовы сами не повторяются.';status=200
    except HumanError as error:title=error.title;message=error.hint;status=400
    content='<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>'+html.escape(title)+'</title><h1>'+html.escape(title)+'</h1><p>'+html.escape(message)+'</p><p><a href="/#/mcp">Вернуться в пульт</a></p></html>'
    return Response(content.encode(),status,headers,content_type='text/html; charset=utf-8')
