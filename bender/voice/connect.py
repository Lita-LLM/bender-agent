"""Подключение уже установленного бесплатного голоса к выбранным агентам."""
from ..core.errors import HumanError
from ..core.pathutil import canonical,within
from ..router.roles import problem

MODELS={'ears':'gigaam-v3-e2e-ctc','voice':'piper-ru'}


def folder(agent_ctx):
    root=agent_ctx.host;path=canonical(root.dirs.local/'voice')
    if not within(path,root.dirs.local) or not within(path,root.dirs.root):
        raise HumanError('local_voice','Папка локального голоса стала ссылкой наружу','Восстанови папку local/voice внутри данных.')
    return path


def provider(agent_ctx):
    root=agent_ctx.host;url='http://127.0.0.1:'+str(root.settings.get('voice_local','port'))+'/v1'
    return next((p for p in root.models.providers().values() if p.get('preset')=='local_voice' and p.get('base_url')==url
                 and p.get('is_local') and p.get('free') and p.get('enabled',True)),None)


def connection(agent_ctx):
    p=provider(agent_ctx);roles=agent_ctx.models.roles();cfg=agent_ctx.settings.group('voice')
    connected={role:bool(p and any(e.get('provider')==p['id'] and e.get('model')==model and e.get('enabled',True)
                                 for e in roles.get(role,[]) if isinstance(e,dict))) for role,model in MODELS.items()}
    return {'agent':agent_ctx.agent_id,'roles':connected,'enabled':cfg['enabled'],'stt':cfg['stt'],'tts':cfg['tts'],
            'connected':all(connected.values()),'available':bool(p)}


async def connect(ctx,target=None,*,only_empty=False):
    root=ctx.app.host;target=target or ctx.app.agent_id
    if ctx.actor!='owner' and target!=ctx.app.agent_id:
        raise HumanError('local_voice','Можно подключить голос только себе','Другим агентам голос подключает хозяин.')
    path=folder(root)
    if not (path/'installed.json').is_file():raise HumanError('local_voice','Локальный голос ещё не установлен','Попроси хозяина выполнить bender voice install.')
    await ctx.check('settings','roles/voice','подключить установленный бесплатный локальный голос')
    p=provider(root)
    if p is None:
        if ctx.actor!='owner':raise HumanError('local_voice','Не подключён провайдер локального голоса','Хозяин может восстановить его командой bender voice connect '+ctx.app.agent_id+'.')
        from .cli import token_of
        await ctx.check('settings','providers','восстановить локального провайдера из установленного пакета')
        p=root.models.add_provider(preset='local_voice',base_url='http://127.0.0.1:'+str(root.settings.get('voice_local','port'))+'/v1',
                                   api_key=token_of(root),extra={'models':[{'id':model,'audio_role':role} for role,model in MODELS.items()]})
    agents=[r['id'] for r in root.agents.public() if not r['archived']] if target=='all' else [root.agents.resolve(target)]
    connected=[];skipped=[];repairs=0
    for aid in agents:
        agent_ctx=root.agents.get(aid);changes={}
        with agent_ctx.settings.transaction():
            own=agent_ctx.settings._stored('roles');roles=agent_ctx.models.roles();providers=agent_ctx.models.providers()
            for role,model in MODELS.items():
                existing=own.get(role,[])
                if only_empty and isinstance(existing,list) and any(not problem(e,providers) for e in existing):continue
                rest=[e for e in roles.get(role,[]) if isinstance(e,dict) and (e.get('provider'),e.get('model'))!=(p['id'],model)]
                changes[role]=[{'provider':p['id'],'model':model,'params':{'audio_protocol':'openai'}},*rest]
            if not changes:skipped.append(aid);continue
            before=agent_ctx.store.meta_get('models:role_repair_notice')
            agent_ctx.settings.set('roles',changes,by=ctx.actor)
            after=agent_ctx.store.meta_get('models:role_repair_notice')
            if after and after!=before:repairs+=after['count']
            agent_ctx.settings.set('voice',{'enabled':True,**({'format':'wav'} if 'voice' in changes else {})},by=ctx.actor)
        agent_ctx.journal.write('settings','Подключён установленный локальный голос',actor=ctx.actor,data={'group':'roles','roles':list(changes)})
        connected.append(aid)
    note='Локальный голос подключён: '+(', '.join(connected) or 'уже есть свои аудиоцепочки')+'.'
    if skipped:note+=' Сохранены свои Уши/Голос: '+', '.join(skipped)+'.'
    if repairs:note+=' Удалены повреждённые кандидаты: '+str(repairs)+'; запись — в журнале.'
    if not root.settings.get('voice_local','enabled'):note+=' Сама служба выключена; хозяину: bender voice serve или bender service install --voice.'
    note+=' Изменения видны без перезапуска ядра. Для голосовых ответов в Telegram: /voice reply.'
    return {'connected':connected,'skipped':skipped,'repaired':repairs,'note':note}
