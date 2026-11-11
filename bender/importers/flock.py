"""Переезд по умолчанию в новых агентов; предпросмотр не меняет их или main."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import tempfile
from contextlib import contextmanager
from pathlib import Path

from ..core.errors import HumanError
from ..core.store import dumps


def mode(value):
    value=value or 'new'
    if value not in ('new','current'):raise HumanError('import_destination','Неизвестное назначение переезда','Выбери нового или текущего агента.')
    return value


@contextmanager
def target(ctx, destination):
    if destination=='current':
        yield ctx
        return
    from ..core.app import App
    from ..agents.settings import AgentSettings
    from ..router.config import ModelsConfig
    with tempfile.TemporaryDirectory(prefix='import-preview-',dir=ctx.host.dirs.local) as folder:
        preview=App(Path(folder)/'data')
        try:
            preview.settings=AgentSettings(ctx.host.settings,preview.store,preview._settings_changed)
            preview.secrets=ctx.host.secrets
            preview.models=ModelsConfig(preview.settings,preview.secrets)
            preview._import_source_guard=ctx
            yield preview
        finally:asyncio.run(preview.close())


def normalized(value, root, main_session=None):
    if isinstance(value,Path):value=str(value)
    if isinstance(value,str):return '@главная' if main_session and value==main_session else value.replace(str(root),'@назначение')
    if isinstance(value,bytes):return {'bytes':base64.b64encode(value).decode('ascii')}
    if isinstance(value,dict):return {str(k):normalized(v,root,main_session) for k,v in value.items()}
    if isinstance(value,(list,tuple)):return [normalized(v,root,main_session) for v in value]
    if value is None or isinstance(value,(bool,int,float)):return value
    raise HumanError('import_format','Неизвестные данные в плане переезда','Обнови Бендера; источник не изменён.',detail=type(value).__name__)


def sources(ctx, kind, path, agent='', destination='new'):
    if kind=='openclaw':
        from . import openclaw as oc
        installs=oc.find_installs(path or None,app=ctx)
        if not installs:raise HumanError('no_openclaw','OpenClaw не найден','Укажи папку установки.')
        install=installs[0]
        agents=install.agents if agent in ('','*') and destination=='new' else [install.agent(agent if agent!='*' else None)]
        if destination=='current' and agent=='*' and len(install.agents)>1:raise HumanError('import_destination','Для замены выбери одного агента','Нескольких переноси в новых агентов.')
        from .sourceguard import check
        for a in agents:
            for p in (a.workspace,a.agent_dir,a.db):
                if p is not None:check(ctx,p)
            if a.agent_dir:
                for filename in ('auth-profiles.json','models.json'):
                    p=a.agent_dir/filename
                    if p.exists():check(ctx,p,a.agent_dir)
        return [(a.id,a.name,str(install.root)) for a in agents]
    from . import portable
    root=portable.source(ctx,kind,path)
    return [(kind,portable.KINDS[kind],str(root))]


def make_plan(ctx, kind, path=None, agent='', destination='new', profiles=None):
    from . import openclaw as oc,portable
    destination=mode(destination);entries=[];items=[];warnings=[];fingerprints=[]
    profiles=profiles or {}
    if not isinstance(profiles,dict) or any(v not in ('full','helper','observer','reader') for v in profiles.values()):
        raise HumanError('import_profile','Неизвестный шаблон переезда','Выбери Полный, Помощник, Наблюдатель или Чтец.')
    entries_source=sources(ctx,kind,path,agent,destination)
    for number,(source_id,name,root) in enumerate(entries_source):
        with target(ctx,destination) as dest:
            if kind=='openclaw':
                raw=oc.make_plan(dest,root,source_id);public=raw.public()
                payload=[[i.id,i.kind,i.action,i.selected,i.payload] for i in raw.items]
                identity=raw.item('self:identity')
                if identity:name=identity.payload.get('fields',{}).get('name') or name
            else:
                raw=portable.make_plan(dest,kind,root);public=portable.public(raw);payload=raw['items']
            fingerprints.append(normalized(payload,dest.dirs.root,dest.store.meta_get('main_session') if destination=='new' else None))
        profile=profiles.get(source_id,'full' if kind=='openclaw' and public['agent'].get('default') else 'helper')
        prefix=str(number)+'::' if len(entries_source)>1 else ''
        for item in public['items']:
            items.append({**item,'id':prefix+item['id'],'title':(name+' · ' if prefix else '')+item['title'],'source_agent':source_id})
        warnings.extend(public.get('warnings',[]))
        entries.append({'source_agent':source_id,'name':name,'path':root,'destination':destination,'prefix':prefix,'profile':profile})
    token=hashlib.sha256(json.dumps([kind,destination,entries,fingerprints],sort_keys=True,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
    return {'source':{'path':entries[0]['path'],'version':public.get('source',{}).get('version','')},
            'agent':{'name':entries[0]['name'] if len(entries)==1 else str(len(entries))+' агента'},
            'destinations':entries,'mode':destination,'hash':token,'items':items,'warnings':warnings}


def telegram_candidate(ctx, plan, selected, with_secrets):
    from ..channels.telegram import TOKEN_SECRET
    if not with_secrets:return None
    matches=[i for i in plan.items if i.kind=='secret' and i.id in selected and i.payload.get('name','').startswith('openclaw:channels.telegram') and re.search(r'(bot)?token$',i.payload['name'],re.I)]
    if not matches:return None
    token=matches[0].payload['value']
    secret=ctx.secrets.reuse(matches[0].payload['name'],token,note='бот из OpenClaw',allow={'hosts':['api.telegram.org'],'tools':[]})
    same=token==ctx.host.secrets.get(TOKEN_SECRET)
    channels=plan.item('archive:channels')
    value={'secret':secret,'same_bot':same,'state':'offer_topic' if same else 'saved',
           'owner_ids':list(channels.payload.get('telegram_owner_ids') or []) if channels else []}
    ctx.store.meta_set('telegram:import_candidate',value)
    return value


async def apply(ctx, kind, path, **options):
    jobs=getattr(ctx.host,'_import_jobs',None)
    if jobs is None:jobs=set();ctx.host._import_jobs=jobs
    job=asyncio.current_task();jobs.add(job)
    try:return await _apply(ctx,kind,path,**options)
    finally:jobs.discard(job)


async def _apply(ctx, kind, path, *, agent='', destination='new', expected=None, items=None, secrets=True, confirmed=False, allow_running=False, profiles=None):
    from . import openclaw as oc,portable
    destination=mode(destination)
    if destination=='current' and not confirmed:raise HumanError('import_confirm','Подтверди замену текущего агента','Выбери её явно в окне переезда.',status=409)
    if destination=='current' and not allow_running and ctx.mind.running(ctx.main_session()):raise HumanError('busy','Текущий агент работает','Дождись конца хода перед заменой.',status=409)
    plan=await asyncio.to_thread(make_plan,ctx,kind,path,agent,destination,profiles)
    if expected!=plan['hash']:raise HumanError('import_changed','План или источник изменился','Построй новый предпросмотр и выбери пункты снова.',status=409)
    available={i['id'] for i in plan['items']}
    if not isinstance(items,list) or any(i not in available for i in items):raise HumanError('import_selection','Нужен явный выбор пунктов плана','Отметь нужное в предпросмотре.')
    selected=set(items);result={'done':[],'skipped':[],'errors':[],'warnings':[],'counts':{},'agents':[],'dir':''}
    for entry in plan['destinations']:
        only={i[len(entry['prefix']):] for i in selected if not entry['prefix'] or i.startswith(entry['prefix'])}
        if not only:continue
        if destination=='new':
            record=ctx.host.agents.create(entry['name'],awaken=False,profile=entry['profile'],start=False)
            dest=ctx.host.agents.get(record['id'])
            ctx.host.store.x('UPDATE flock_agents SET archived=1 WHERE id=?',(dest.agent_id,))
        else:dest=ctx
        dest._importing=True
        def run():
            dest._import_source_guard=ctx
            try:return transfer()
            finally:del dest._import_source_guard
        def transfer():
            if kind=='openclaw':
                actual=oc.make_plan(dest,entry['path'],entry['source_agent'])
                rep=oc.apply(actual,dest,only=only,secrets=secrets,adopt_channels=destination=='current',deduplicate=True).public()
                if destination=='new':
                    candidate=telegram_candidate(dest,actual,only,secrets)
                    if candidate:rep['warnings'].append('Telegram уже подключён: предложена отдельная тема новому агенту.' if candidate['same_bot'] else 'Токен Telegram сохранён для агента; подключение выбирает хозяин в «Связи».')
                return rep
            actual=portable.make_plan(dest,kind,entry['path'])
            return portable.apply(dest,actual,actual['hash'],sorted(only)).public()
        job=asyncio.create_task(asyncio.to_thread(run))
        try:report=await asyncio.shield(job)
        except BaseException:
            while not job.done():
                try:await asyncio.shield(job)
                except asyncio.CancelledError:continue
                except Exception:break
            if not job.cancelled():job.exception()
            raise
        finally:dest._importing=False
        for key in ('done','skipped','errors','warnings'):result[key].extend(report.get(key,[]))
        for key,value in report.get('counts',{}).items():
            if isinstance(value,(int,float)):result['counts'][key]=result['counts'].get(key,0)+value
        result['dir']=report.get('dir','');result['agents'].append({'id':dest.agent_id,'name':dest.selfdir.identity().get('name') or entry['name'],'dir':result['dir']})
        if destination=='new':
            ctx.host.store.x('UPDATE flock_agents SET archived=? WHERE id=?',(int(bool(report['errors'])),dest.agent_id))
            if report['errors']:result['warnings'].append('Частично перенесённый агент оставлен в архиве; проверь ошибки перед пробуждением.')
            elif getattr(ctx.host,'flock_started',False) and not ctx.host.safe and not getattr(ctx.host,'_closed',False):dest.heart.start()
    result['counts']['agents']=len(result['agents'])
    ctx.host.bus.publish({'type':'agents','agent_id':'*'})
    return result
