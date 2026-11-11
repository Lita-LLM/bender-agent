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
