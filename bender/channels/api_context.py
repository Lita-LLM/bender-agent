"""Контекст API: те же данные агента, отдельные Разум/тулы и пересечение потолков канала."""
import asyncio
import copy
import weakref

from ..agents.rights import TEMPLATES
from ..agents.runtime import Router as AgentRouter,Bus
from ..core.app import AgentContext
from ..core.errors import HumanError
from ..guard import Guard
from ..mcp.manager import digest
from ..mcp.server import McpLoop
from ..mind.loop import Mind
from ..router.config import ModelsConfig
from ..team import Team
from ..tools import register_all
from ..tools.registry import Registry


PROFILES={key:value for key,value in {'helper':'Помощник','observer':'Наблюдатель','reader':'Чтец','full':'Полный'}.items()}


def signature(source):
    return digest({'api':source.settings.group('api_rights'),'rights':source.host.agents.rights.effective(source),
                   'guard':source.settings.group('guard'),'tools':source.settings.group('tools')})


def ceiling(source):
    actual=source.host.agents.rights.effective(source);profile=source.settings.get('api_rights','profile','helper')
    if profile not in PROFILES:raise HumanError('api_rights','Неизвестный профиль API','Выбери действующий профиль в Правах агента.')
    preset=TEMPLATES[profile]
    result=copy.deepcopy(actual)
    result['profile']=actual['profile'] if profile=='full' else profile
    result['capabilities']=[cap for cap in actual['capabilities'] if cap in preset['capabilities']]
    left,right=actual['groups'],preset['groups']
    result['groups']=list(right if '*' in left else left if '*' in right else [group for group in left if group in right])
    for key in ('network','ssh','team_enabled'):result[key]=bool(actual.get(key) and preset.get(key))
    result['require_isolation']=bool(actual.get('require_isolation') or preset.get('require_isolation'))
    values=[limit for limit in (actual.get('team_max',0),preset.get('team_max',0)) if limit]
    result['team_max']=min(values) if values else 0
    if profile!='full':result['work_folders']=[];result['guard_profile']='careful'
    return result


class SettingsView:
    def __init__(self,source,ctx):self.source=source;self.ctx=ctx
    def __getattr__(self,name):return getattr(self.source.settings,name)
    def group(self,group):return ceiling(self.source) if group=='agent_rights' else self.source.settings.group(group)
    def get(self,group,key,default=None):return self.group(group).get(key,default)
    def set(self,group,changes,*,by='owner'):
        if by!='owner':self.source.host.agents.rights.settings(self.ctx,group,changes)
        return self.source.settings.set(group,changes,by=by)


class ApiGuard(Guard):
    @property
    def halted(self):return self._api_halted or self.app.source.guard.halted
    @halted.setter
    def halted(self,value):self._api_halted=value


class ApiTeam(Team):
    def permissions(self,session_id):
        if self.app._closed:return {'tools':(),'capabilities':()}
        return super().permissions(session_id)


class ApiContext(AgentContext):
    """Без новой личности/базы/агента: свой исполняющий контекст поверх тех же данных."""
    def __init__(self,source):
        self.__dict__.update(source.__dict__)
        self.source=source;self._api_closed=False;self._api_signature=signature(source);self._api_channel=True
        self.host=source.host;self.bus=Bus(self.host.bus,self.agent_id);self.settings=SettingsView(source,self)
        for name in ('_mcp','_skills','_search','_processes','_modules','_versions','_updater','_workshop','_backups','_voice','_inspector'):setattr(self,name,None)
        self.guard=ApiGuard(self);self.tools=Registry(self);register_all(self.tools)
        self.mind=Mind(self);self.mind.turns=source.mind.turns;self.team=ApiTeam(self);self.heart=source.heart
        self.models=ModelsConfig(self.settings,self.secrets);self.router=AgentRouter(self.host.router,self)
        self._cli_bridge=McpLoop(self);self.chat_locks=weakref.WeakValueDictionary()
        if not self.safe and self.settings.get('mcp','servers',[]):_=self.mcp

    @property
    def _closed(self):
        return self._api_closed or getattr(self.source,'_closed',False) or getattr(self.source,'_importing',False) or getattr(self.host,'_closed',False) or not self.host.settings.get('api','enabled',False) or not self.source.settings.get('api_rights','enabled',True) or signature(self.source)!=self._api_signature

    def revoke(self):
        self._api_closed=True
        for sid,turn in list(self.mind.turns.items()):
            if turn.context is self:self.mind.stop(sid)

    async def close(self):
        self.revoke()
        turns=[turn.task for turn in self.mind.turns.values() if turn.context is self and turn.task and turn.task is not asyncio.current_task()]
        if turns:await asyncio.gather(*turns,return_exceptions=True)
        await self.team.stop()
        background=list(self.mind.background)
        for task in background:task.cancel()
        if background:await asyncio.gather(*background,return_exceptions=True)
        for name in ('_mcp','_skills','_processes'):
            obj=getattr(self,name,None)
            if obj:await obj.close()
        if self._workshop:self._workshop.stop()
        await self._cli_bridge.stop()
        self.bus.dispose()


class Runtime:
    def __init__(self,host):self.host=host;self.contexts={};self.closing=set()

    def context(self,source):
        current=self.contexts.get(source.agent_id)
        if current and not current._closed:return current
        if current:self._retire(current)
        current=ApiContext(source);self.contexts[source.agent_id]=current;return current

    def _retire(self,ctx):
        ctx.revoke()
        task=asyncio.create_task(ctx.close(),name='api-context-close');self.closing.add(task);task.add_done_callback(self.closing.discard)

    def invalidate(self,agent_id=None):
        for key,ctx in list(self.contexts.items()):
            if agent_id is None or key==agent_id:self.contexts.pop(key,None);self._retire(ctx)

    async def close(self):
        contexts=list(self.contexts.values());self.contexts.clear()
        if contexts:await asyncio.gather(*(ctx.close() for ctx in contexts),return_exceptions=True)
        if self.closing:await asyncio.gather(*list(self.closing),return_exceptions=True)
