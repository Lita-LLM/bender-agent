"""Маршрутизация общего бота: выбор хозяина привязан к агенту и его сессии."""
from ..core.errors import HumanError


def selected(tg,chat):
    if tg.topics.enabled(chat) and getattr(chat,'thread',None):
        row=tg.topics.lookup(chat,chat.thread)
        if row:
            ctx=tg.app.agents.get(row['agent']);session=ctx.store.session(row['session'])
            if not session or session['meta'].get('archived'):raise HumanError('telegram_session','Разговор темы закрыт','Создай /new в общем потоке.')
            return ctx,row['session']
    state=tg.app.store.meta_get('telegram:selection',{}).get(str(chat),{})
    try:ctx=tg.app.agents.get(state.get('agent','main'))
    except HumanError:ctx=tg.app
    sid=None if tg.topics.enabled(chat) else state.get('session')
    row=ctx.store.session(sid) if sid else None
    if not row or row['meta'].get('archived') or row['kind'] not in ('main','chat','imported'):sid=ctx.main_session()
    return ctx,sid


FALLBACKS={'agent_archived','flock_off','agent_missing','temporary_done','session_closed'}  # о них хозяину — короткое уведомление
_CODES={'agent_archived':'agent_archived','flock_off':'flock_off','agent_missing':'agent_missing','agent_temporary_done':'temporary_done'}
OWN_KINDS=('main','chat','imported')


def _info(ctx,sid,quoted=None):
    return {'quoted':quoted,'routed':False,'selected_agent':getattr(ctx,'agent_id',None),'selected_session':sid,'fallback':None,
            'agent':None,'name':None,'temporary':None}


def _record(tg,agent):
    try:return tg.app.agents.record(agent)
    except HumanError:return None


def _load(tg,agent):
    """(контекст, код отказа): архив, выключенная стая, удалённый или завершённый временный агент."""
    try:return tg.app.agents.get(agent),None
    except HumanError as error:return None,_CODES.get(error.code,'agent_missing')


def route_reply(tg,chat,msg,ctx,sid):
    """Ответ цитатой на сообщение другого агента стаи (или другого разговора) уходит туда, откуда сообщение, а не выбранному агенту.
    Выбор хозяина не меняется: это адрес одного ответа. → (контекст, разговор, info).

    info: quoted — номер процитированного; routed — адресат не выбранный агент/разговор; selected_agent/selected_session — выбор;
    fallback — почему ответ остался у выбранного (agent_archived, flock_off, agent_missing, temporary_done, no_route) или ушёл
    в главный разговор агента (session_closed); agent/name — чьё сообщение; temporary — {'agent','name','task'}, если сообщение
    временного помощника: ответ уходит его родителю.
    ctx=None — выбор недоступен (закрытая тема): годится только действующий адресат цитаты, иначе (None, None, info)."""
    quote=msg.get('reply_to_message') or {}
    info=_info(ctx,sid,quote.get('message_id'))
    from .telegram_forward import forwarded
    if not quote.get('message_id') or msg.get('external_reply') or forwarded(msg):return ctx,sid,{**info,'quoted':None}
    if not tg.settings().get('reply_routing',True):return ctx,sid,info
    from .telegram_refs import route as known_route
    route=known_route(tg.app,chat,quote['message_id']) or {}
    agent=route.get('agent')
    if not isinstance(agent,str):return ctx,sid,{**info,'fallback':'no_route'}
    record=_record(tg,agent)
    info.update(agent=agent,name=(record or {}).get('name') or agent)
    target=route.get('session')
    if record and record.get('temporary'):
        # Временный помощник работает в разговоре задачи (subagent): хозяину там не отвечают — ответ получает родитель.
        data=record.get('data') or {}
        info['temporary']={'agent':agent,'name':info['name'],'task':data.get('task')}
        parent=data.get('parent') if isinstance(data.get('parent'),str) else 'main'
        info.update(agent=parent,name=(_record(tg,parent) or {}).get('name') or parent)
        other,code=_load(tg,parent)
        if other is None:return ctx,sid,{**info,'fallback':code}
        helper=tg.app.agents.loaded.get(agent)
        row=helper.store.session(target) if helper is not None and isinstance(target,str) else None
        target=((row or {}).get('meta') or {}).get('parent')
        if not isinstance(target,str) and data.get('task'):
            try:target=(other.team.get(data['task']) or {}).get('parent_session')
            except Exception:target=None  # noqa: BLE001 — нет задачи: главный разговор родителя
        if record.get('archived'):info['fallback']='temporary_done'
    else:
        other,code=_load(tg,agent)
        if other is None:return ctx,sid,{**info,'fallback':code}
    row=other.store.session(target) if isinstance(target,str) else None
    if not row or row['meta'].get('archived') or row['kind'] not in OWN_KINDS:
        # Закрыт разговор с хозяином (в архиве или удалён) — скажем; служебный (пульс, расписание) — молча в главный.
        if isinstance(target,str) and not info['fallback'] and (row is None or row['kind'] in OWN_KINDS):info['fallback']='session_closed'
        target=other.main_session()
    info['routed']=ctx is None or (other.agent_id,target)!=(ctx.agent_id,sid)
    return other,target,info


def replied(tg,chat,msg,ctx,sid):
    """Прежний интерфейс route_reply: только адресат."""
    other,target,_info_=route_reply(tg,chat,msg,ctx,sid)
    return other,target


def _name(ctx,value):
    """Имя для служебной строки: без секретов, переносов и скобок рамки — служебный факт, не текст."""
    import re
    from ..mind.passport import clean
    value=clean(ctx,str(value or ''),stream=False)
    value=re.sub(r'[\[\]«»<>\n\r\t`]+',' ',value)
    return ' '.join(value.split())[:60] or '?'


def agent_name(ctx):
    try:return ctx.selfdir.identity().get('name') or ctx.host.agents.record(ctx.agent_id)['name']
    except (HumanError,AttributeError,KeyError):return getattr(ctx,'agent_id','main')


def notice_text(reason,who,selected,helper=None):
    """Текст уведомления хозяину по причине route_reply; None — уведомлять не о чем."""
    if reason not in FALLBACKS:return None
    who=who or 'агент';selected=selected or 'выбранному агенту'
    if reason=='session_closed':return '«'+who+'»: тот разговор закрыт — ответ передан в главный разговор «'+who+'».'
    if reason=='temporary_done':return 'Временный помощник «'+(helper or who)+'» уже завершён — ответ передан «'+(who if helper else selected)+'».'
    head={'agent_archived':'«'+who+'» в архиве','flock_off':'Стая выключена, «'+who+'» не отвечает','agent_missing':'Агента «'+who+'» больше нет'}[reason]
    return head+' — ответ передан «'+selected+'».'


def fallback_notice(tg,info):
    """Короткое уведомление хозяину: процитированный агент недоступен — кому ушёл ответ. None — уведомлять не о чем."""
    if info.get('fallback') not in FALLBACKS:return None
    selected=info.get('selected_agent')
    selected=((_record(tg,selected) or {}).get('name') or selected) if selected else None
    return notice_text(info['fallback'],info.get('name'),selected,(info.get('temporary') or {}).get('name'))


def note_text(what,where):
    """Служебная строка адресату цитаты: что процитировано и что выбрано у хозяина."""
    return ('[Telegram · служебно: хозяин ответил цитатой на '+what+', пока '+where+'; твой ответ уйдёт в тот же чат цитатой на его сообщение. '
            'Следующее его сообщение без цитаты уйдёт выбранному агенту.]')


def routed_note(tg,ctx,sid,info,known=None):
    """Служебная строка адресату цитаты, пока у хозяина выбран другой агент или разговор. Не команда, ход не заражает."""
    if not info.get('routed'):return ''
    mid=info.get('quoted')
    selected=None
    if info.get('selected_agent'):
        try:selected=tg.app.agents.get(info['selected_agent'],archived=True)
        except HumanError:selected=None
    if selected is None:where='тема, из которой он пишет, закрыта'
    elif selected.agent_id==ctx.agent_id:where='у него выбран другой твой разговор'
    else:where='у него выбран «'+_name(ctx,agent_name(selected))+'»'
    helper=info.get('temporary')
    if helper:
        what='сообщение твоего временного помощника «'+_name(ctx,helper.get('name'))+'»'+(' (задача '+_name(ctx,helper.get('task'))+')' if helper.get('task') else '')+' #'+str(mid)
    elif known and known.get('direction')=='in':what='своё сообщение #'+str(mid)+' из твоего разговора'
    else:what='твоё сообщение #'+str(mid)
    if info.get('fallback')=='session_closed':what+=' из уже закрытого разговора — его слова пришли в твой главный разговор'
    return note_text(what,where)


def choose(tg,chat,ctx,sid=None):
    sid=sid or ctx.main_session();row=ctx.store.session(sid)
    if not row or row['meta'].get('archived') or row['kind'] not in ('main','chat','imported'):
        raise HumanError('telegram_session','Этот разговор недоступен','Выбери действующий разговор агента.')
    if tg.topics.enabled(chat) and getattr(chat,'thread',None):
        return tg.topics.bind(chat,chat.thread,ctx,sid,tg.topics.name(ctx,sid))
    states=tg.app.store.meta_get('telegram:selection',{})
    states[str(chat)]={'agent':ctx.agent_id,'session':sid}
    tg.app.store.meta_set('telegram:selection',states)
    bind(tg,chat,ctx,sid)
    return row


def bind(tg,chat,ctx,sid):
    routes=tg.app.store.meta_get('telegram:sessions',{})
    key=ctx.agent_id+':'+sid
    if routes.get(key)!=chat:
        routes[key]=chat
        tg.app.store.meta_set('telegram:sessions',routes)


def destination(tg,ctx,sid):
    topic=tg.topics.for_session(ctx,sid)
    if topic:return topic
    owner=tg.owner() or {}
    return (tg.app.store.meta_get('telegram:sessions',{}).get(ctx.agent_id+':'+sid) if isinstance(sid,str) else None) or owner.get('chat_id') or owner.get('user_id')


def key(ctx,sid):return (ctx.agent_id,sid)
