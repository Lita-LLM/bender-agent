"""Выбор контекста закреплён за HTTP-запросом/подпиской, а не глобальным кликом."""
from .runtime import ACTIVE


class ContextProxy:
    def __init__(self, host): object.__setattr__(self,'host',host)
    def context(self): return ACTIVE.get() or self.host
    def __getattr__(self, name): return getattr(self.context(),name)
    def __setattr__(self, name, value): setattr(self.context(),name,value)


def routes(router, host, J, body):
    from ..net.http_server import Response
    from ..tools.registry import ToolContext
    async def check():await ToolContext(host,actor='owner').check('settings','agents','управление стаей хозяином')

    @router.route('GET','/api/agents')
    @J
    async def listing(req):return {'agents':host.agents.public()}

    @router.route('GET','/api/agent-templates')
    @J
    async def templates(req):
        from ..team.roles import public
        ctx=ACTIVE.get() or host
        return {'roles':{k:public(v) for k,v in ctx.team.roles().items() if v.enabled}}

    @router.route('PATCH','/api/agents/{agent}/api-rights')
    @J
    async def api_rights(req):
        await check();ctx=host.agents.get(req.params['agent']);data=body(req)
        ctx.settings.set('api_rights',data,by='owner')
        return host.agents.rights.public(ctx)

    @router.route('POST','/api/agents')
    @J
    async def create(req):
        await check();b=body(req)
        if b.get('role'):return host.agents.create_role(ACTIVE.get() or host,b['role'],name=b.get('name',''),awaken=b.get('awaken',True))
        return host.agents.create(b.get('name',''),agent_id=b.get('id',''),awaken=b.get('awaken',True),profile=b.get('profile','helper'))

    @router.route('PATCH','/api/agents/{agent}')
    @J
    async def update(req):
        await check();b=body(req)
        return await host.agents.update(req.params['agent'],name=b.get('name'),archived=b.get('archived'),description=b.get('description'))

    @router.route('DELETE','/api/agents/{agent}')
    @J
    async def delete(req):
        await check()
        return await host.agents.delete(req.params['agent'],confirmed=body(req).get('confirmed') is True)

    @router.route('GET','/api/agents/{agent}/export')
    @J
    async def export(req):
        await check()
        return Response(host.agents.export(req.params['agent']),headers={'Content-Disposition':'attachment; filename="agent.zip"'},content_type='application/zip')

    @router.route('POST','/api/agents/import')
    @J
    async def import_agent(req):
        await check()
        return host.agents.import_archive(req.body,name=req.q('name',''))

    @router.route('GET','/api/chats')
    @J
    async def chats(req):return {'sessions':[{**row,'read_only':row['kind']=='correspondence'} for row in (ACTIVE.get() or host).store.sessions(limit=300)]}

    @router.route('GET','/api/agents/{agent}/rights')
    @J
    async def rights(req):return host.agents.rights.public(host.agents.get(req.params['agent'],archived=True))

    @router.route('GET','/api/correspondence')
    @J
    async def correspondence(req):
        return {'settings':host.settings.group('agent_chat'),'agents':host.agents.public(),'letters':host.agents.correspondence.feed(),'assignments':host.agents.assignments.listing(active=False,limit=60)}

    @router.route('POST','/api/assignments/{ident}/cancel')
    @J
    async def cancel_assignment(req):
        await check();row=host.agents.assignments.update('owner',req.params['ident'],'cancelled',str(body(req).get('note','') or 'Отменил хозяин'))
        host.journal.write('agents','Хозяин отменил поручение '+row['id'],actor='owner');return {'assignment':row}

    @router.route('PATCH','/api/correspondence')
    @J
    async def set_correspondence(req):
        await check();host.settings.set('agent_chat',body(req),by='owner')
        host.journal.write('agents','Хозяин изменил матрицу общения',actor='owner')
        return {'settings':host.settings.group('agent_chat')}

    @router.route('POST','/api/correspondence/link')
    @J
    async def decide_link(req):
        await check();b=body(req)
        return {'settings':host.agents.correspondence.set_link(str(b.get('sender','')),str(b.get('recipient','')),str(b.get('mode','')))}

    @router.route('PATCH','/api/agents/{agent}/rights')
    @J
    async def set_rights(req):
        await check();ctx=host.agents.get(req.params['agent'],archived=True)
        ctx.settings.set('agent_rights',body(req),by='owner')
        ctx.journal.write('guard','Хозяин изменил потолок прав агента',actor='owner')
        host.bus.publish({'type':'agents','agent_id':'*'})
        return host.agents.rights.public(ctx)

    @router.route('POST','/api/import/rights-reviewed')
    @J
    async def reviewed_import(req):
        await check();ctx=ACTIVE.get() or host
        if body(req).get('full') is True:
            from .rights import TEMPLATES
            ctx.settings.set('agent_rights',{'profile':'full',**TEMPLATES['full']},by='owner')
            ctx.journal.write('guard','Хозяин выбрал Полный для перенесённого агента',actor='owner')
        ctx.store.meta_set('import:rights_reviewed',True)
        return {'ok':True}

    @router.route('POST','/api/chats')
    @J
    async def create_chat(req):
        ctx=ACTIVE.get() or host
        await ToolContext(ctx,actor='owner').check('settings','chats','новый разговор хозяина')
        sid=ctx.store.new_session('chat',str(body(req).get('title') or 'Новый чат')[:200])
        return {'session':ctx.store.session(sid)}

    @router.route('PATCH','/api/chats/{sid}')
    @J
    async def change_chat(req):
        from ..core.errors import HumanError
        ctx=ACTIVE.get() or host;sid=req.params['sid'];row=ctx.store.session(sid)
        if not row:raise HumanError('session','Такого чата у агента нет','',status=404)
        if row['kind']=='correspondence':raise HumanError('correspondence_readonly','Переписка агентов — только для чтения','Открой обычный чат для разговора.',status=403)
        await ToolContext(ctx,actor='owner').check('settings','chats','изменить разговор хозяина')
        b=body(req);ctx.store.update_session(sid,title=b.get('title'),meta={'archived':bool(b.get('archived',row['meta'].get('archived',False)))})
        if b.get('title') is not None and host.telegram.state=='ok':await host.telegram.topics.rename(ctx,sid)
        return {'session':ctx.store.session(sid)}
