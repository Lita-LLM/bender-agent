"""Потолки хозяина: проверяются до правил/грантов, наследуются только пересечением."""
from __future__ import annotations

import fnmatch
from urllib.parse import urlsplit
from ..core.errors import HumanError
from ..core.pathutil import canonical, within
from ..core.settings import NeedsOwner, OWNER_GROUPS, SCHEMA
from ..guard.policy import CAPS

PROFILES = {'full':'Полный','helper':'Помощник','observer':'Наблюдатель','reader':'Чтец','custom':'Свой'}
SAFE = {'audio.transcribe','audio.speak','system.restart','telegram.profile','channel.send_file','agent.send','fs.read','fs.write','fs.delete','net.read','memory.core','schedule','settings','team.spawn','msg.send','skill.learn','manner','agent.describe','agent.link_request'}
TEMPLATES = {
 'full': {'capabilities':list(CAPS),'groups':['*'],'guard_profile':'inherit','network':True,'work_folders':None,'team_enabled':True,'team_max':0,'ssh':True,'require_isolation':False},
 'helper': {'capabilities':sorted(SAFE),'groups':['files','web','memory','heart','self','team','skills','agents','channel'],'guard_profile':'careful','network':True,'work_folders':[],'team_enabled':True,'team_max':2},
 'observer': {'capabilities':['fs.read','net.read'],'groups':['files','web','memory','self','agents'],'guard_profile':'careful','network':True,'work_folders':[],'team_enabled':False,'team_max':0},
 'reader': {'capabilities':[],'groups':[],'guard_profile':'careful','network':False,'work_folders':[],'team_enabled':False,'team_max':0},
 'custom': {'capabilities':[],'groups':[],'guard_profile':'careful','network':False,'work_folders':[],'team_enabled':False,'team_max':0},
}
for _name, _template in TEMPLATES.items():
    _template.setdefault('ssh', False)
    _template.setdefault('require_isolation', True)


def validate(values):
    for key in ('capabilities','groups','tools','work_folders','network_hosts'):
        value=values.get(key)
        if value is not None and (not isinstance(value,list) or len(value)>256 or any(not isinstance(x,str) or len(x)>2000 for x in value)):
            raise HumanError('agent_rights','Не удалось сохранить права',f'{key}: нужен список строк или null для шаблона.')
    if any(c not in CAPS for c in values.get('capabilities') or []):
        raise HumanError('agent_rights','Неизвестная способность','Выбери её из списка прав.')
    for path in values.get('work_folders') or []:canonical(path)
    for host in values.get('network_hosts') or []:
        if not host or any(c in host for c in '/:@?#\\'):
            raise HumanError('agent_rights','Нужны имена узлов сети','Пример: example.org или *.example.org, без пути и порта.')


class Rights:
    def __init__(self, manager):self.manager=manager

    def effective(self, ctx):
        record=self.manager.record(ctx.agent_id)
        raw=ctx.settings.group('agent_rights')
        profile=raw.get('profile') or record['data'].get('profile','full' if ctx.agent_id=='main' else 'helper')
        result={**TEMPLATES[profile],**{k:v for k,v in raw.items() if v is not None and k!='profile' and not (k=='guard_profile' and v=='')}}
        result.update(profile=profile, tools=result.get('tools'), network_hosts=result.get('network_hosts') or [], daily_usd=float(result.get('daily_usd') or 0))
        return result

    def capability(self, ctx, cap):
        rights=self.effective(ctx)
        if cap not in rights['capabilities']:return False
        if cap.startswith('net.') and not rights['network']:return False
        return cap!='team.spawn' or rights['team_enabled']

    def tool_allowed(self, ctx, name):
        from ..team.roles import required
        rights=self.effective(ctx)
        if rights['profile']=='reader':return False
        needs=required(name)
        if needs is None and name!='about_me':return rights['profile']=='full' and set(rights['capabilities'])==set(CAPS) and rights.get('tools') is None and '*' in rights['groups']
        if any(not self.capability(ctx,c) for c in needs or []):return False
        tool=ctx.tools.tools.get(name)
        if not tool:return False
        return ('*' in rights['groups'] or tool.group in rights['groups']) and (rights['tools'] is None or name in rights['tools'])

    def _guard_mode(self, ctx):
        """Профиль из потолка прав или None — тогда действуют настройки Предохранителя самого агента.
        Шаблон прав — только значение по умолчанию: если хозяин сам выбрал профиль или выключил
        Предохранитель этому агенту, решает его выбор. Явный потолок в правах сильнее."""
        mode=self.effective(ctx).get('guard_profile')
        if mode=='inherit':return None
        if ctx.settings.group('agent_rights').get('guard_profile'):return mode
        stored=ctx.settings._stored('guard') if hasattr(ctx.settings,'_stored') else {}
        return None if ('profile' in stored or 'enabled' in stored) else mode

    def guard_settings(self, ctx):
        settings=ctx.settings.group('guard')
        mode=self._guard_mode(ctx)
        return {**settings,'profile':mode,'enabled':mode!='off'} if mode else settings

    def guard_source(self, ctx):
        rights=self.effective(ctx)
        if self._guard_mode(ctx) is None:return 'из настроек агента'
        source='задан потолком прав хозяина' if ctx.settings.get('agent_rights','guard_profile') else 'задан шаблоном '+PROFILES[rights['profile']]
        return source+' — изменить в Стая → Права'

    def guard_state(self, ctx):
        """Что действует сейчас и откуда: сохранённые настройки Предохранителя агента могут быть перекрыты потолком прав."""
        effective=self.guard_settings(ctx)
        enabled=bool(effective.get('enabled',True)) and effective.get('profile')!='off'
        return {'enabled':enabled,'profile':effective.get('profile','careful') if enabled else 'off','source':self.guard_source(ctx)}

    def denied(self, ctx, action):
        rights=self.effective(ctx)
        if not self.capability(ctx,action.capability):return f'Способность «{action.capability}» не выдана. Хозяин: Стая → Права → Способности (или шаблон Полный).'
        if action.capability in ('exec','mcp.connect','skill.run') and (action.detail.get('net') or action.detail.get('network')):
            if not rights['network']:return 'Сеть команды выключена. Хозяин: Стая → Права → Разрешить сеть.'
            if rights['network_hosts']:return 'Список узлов ограничивает сетевые тулы, но не общую сеть команды. Используй web_fetch или хозяин: Стая → Права → Разрешённые узлы сети — очистить список.'
        if action.capability.startswith('fs.') and rights['work_folders'] is not None:
            path=ctx.guard.policy.resolve(action.target)
            roots=[ctx.dirs.work,ctx.dirs.self_dir,ctx.dirs.diary,ctx.dirs.skills,*map(canonical,rights['work_folders'])]
            if action.capability=='fs.read' and getattr(ctx,'_task_agent',None):roots+=ctx._task_agent.read_dirs()
            if not any(within(path,root) for root in roots):return 'Путь вне разрешённых папок. Хозяин: Стая → Права → Дополнительные рабочие папки.'
        if action.capability.startswith('net.') and rights['network_hosts']:
            hostname=(urlsplit(action.target).hostname or '').lower()
            if not any(fnmatch.fnmatchcase(hostname,h.lower()) for h in rights['network_hosts']):return 'Узел сети не разрешён. Хозяин: Стая → Права → Разрешённые узлы сети.'
        return ''

    def settings(self, ctx, group, changes):
        """Категория настройки дополняет прежний разбор ослаблений, а не заменяет его."""
        rights=self.effective(ctx)
        if not self.capability(ctx,'settings'):raise HumanError('agent_rights','Агенту не разрешено менять настройки','Права расширяет хозяин.')
        category='owner' if group in OWNER_GROUPS else 'independence' if (not SCHEMA.get(group) or not SCHEMA[group].fields and group!='roles' or ctx.settings.weakening(group,changes)) else 'free'
        mode=rights.get('settings_'+category, 'allow' if category=='free' else 'ask' if category=='independence' else 'deny')
        if mode=='deny':raise HumanError('agent_rights','Агенту не разрешено менять эту категорию настроек','Измени её сам в пульте или расширь права агента.')
        if mode=='ask' and category!='owner':
            title=SCHEMA[group].title if group in SCHEMA else group
            raise NeedsOwner(group,changes,[f'«{title}» хозяин разрешил менять только подтверждением'])

    def check_model(self, ctx):
        if getattr(ctx,'_closed',False) or getattr(ctx.host,'_closed',False):raise HumanError('closing','Бендер останавливается','Дождись следующего запуска.')
        if getattr(ctx,'_importing',False):raise HumanError('import_busy','Агент сейчас переезжает','Дождись завершения переноса.')
        if getattr(ctx,'_task_agent',None):self.check_model(ctx._task_agent.parent)
        limit=self.effective(ctx)['daily_usd']
        if limit and self.manager.cost(ctx.agent_id)>=limit:
            raise HumanError('agent_budget','Дневной бюджет агента исчерпан','Подожди следующего дня или увеличь потолок в «Правах».')

    def check_spawn(self, ctx):
        rights=self.effective(ctx)
        if not self.capability(ctx,'team.spawn'):raise HumanError('agent_rights','Этому агенту не разрешено звать команду','Права расширяет хозяин.')
        limit=rights.get('team_max') or 0
        if limit and sum(t['status'] in ('queued','running') for t in ctx.team.all(limit=10000))>=limit:
            raise HumanError('agent_team_limit','Достигнут предел задач команды','Дождись результата или попроси хозяина изменить права.')

    def public(self, ctx):
        return {'api_rights':ctx.settings.group('api_rights'),'rights':ctx.settings.group('agent_rights'),'effective':self.effective(ctx),'profiles':PROFILES,'templates':TEMPLATES,'capabilities':CAPS,
                'groups':sorted({t.group for t in ctx.tools.tools.values()}),'tools':[{'id':t.name,'title':t.title,'group':t.group} for t in ctx.tools.tools.values()]}
