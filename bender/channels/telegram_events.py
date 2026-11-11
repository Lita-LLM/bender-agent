"""Факты Telegram в контексте: команды, кнопки и правки, без самостоятельного хода."""
from contextvars import ContextVar
import hashlib
from .telegram_rich_in import body

REPLIES=ContextVar('telegram_command_replies',default=None)
# Ответ без модели виден агенту: состояние и переключения. Закрыты recall/files (недоверенный текст),
# report/backup/settings (пути и подробности, агенту ни к чему) и всё неизвестное.
SAFE_REPLIES={'effort','status','fuel','live','voice','whoami','model','models','compact','style','sleep','heart','stop','resume','new','remember'}


def digest(text):return hashlib.sha256(text.encode('utf-8','replace')).hexdigest()


def change(old,new,limit):
    """Короткая цитата вокруг изменения, в том числе в конце длинного сообщения."""
    position=next((i for i,(left,right) in enumerate(zip(old,new)) if left!=right),min(len(old),len(new)))
    start=max(0,position-limit//3)
    def excerpt(value):return ('…' if start else '')+value[start:start+limit]+('…' if len(value)>start+limit else '')
    return excerpt(old)+' → '+excerpt(new)


def capture(chat,text):
    record=REPLIES.get()
    if record is not None and int(chat)==record['chat'] and getattr(chat,'thread',None)==record['thread']:
        from ..mind.passport import clean
        record['reply']=clean(record['ctx'],str(text),stream=True)[:2000]


def record(ctx,sid,chat,label,*,reply='',meta=None,tainted=False,continue_turn=False):
    mode=ctx.settings.get('telegram','history_events','brief')
    if mode=='off' or not ctx.store.session(sid):return
    from ..mind.passport import clean
    label=clean(ctx,label,stream=True)[:1000]
    text='[Telegram · '+label
    if reply:text+=' · ответ без модели: '+clean(ctx,reply,stream=True).replace('\n',' ')[:180 if mode=='brief' else 700]
    text+=' · служебный факт, не новое поручение]'
    ctx.mind.observe(sid,text,tainted=tainted,continue_turn=continue_turn,
        meta={**(meta or {}),'telegram_route':{'chat':int(chat),'thread':getattr(chat,'thread',None)}})


async def dispatch(commands,ctx,chat,sid,command,arg):
    from .telegram_commands import ALIASES,COMMANDS
    command=ALIASES.get(command,command)
    captured={'chat':int(chat),'thread':getattr(chat,'thread',None),'reply':'','ctx':ctx}
    token=REPLIES.set(captured)
    try:handled=await commands.dispatch(ctx,chat,sid,command,arg)
    finally:REPLIES.reset(token)
    if handled:
        name=command if command in {name for name,_ in COMMANDS} else 'неизвестная-команда'
        reply=captured['reply'] if command in SAFE_REPLIES else ''
        record(ctx,sid,chat,'хозяин вызвал /'+name,reply=reply,meta={'telegram_command':name})
        from ..core.errors import HumanError
        try:selected,new_sid=commands.tg.selected(chat)
        except HumanError:return handled
        if (selected.agent_id,new_sid)!=(ctx.agent_id,sid):record(selected,new_sid,chat,'хозяин выбрал этот разговор командой /'+name)
    return handled


def button(tg,cq):
    """Адрес берётся до удаления карточки; непрозрачные токены и ответы не копируются."""
    chat=(cq.get('message') or {}).get('chat',{}).get('id')
    if chat is None:return None
    owner=tg.owner() or {}
    if not owner or (cq.get('from') or {}).get('id')!=owner.get('user_id'):return None
    data=str(cq.get('data') or '');parts=data.split(':')
    ctx,sid=tg.selected(chat);label='хозяин нажал неизвестную или устаревшую кнопку'
    state=None
    if len(parts)>=3 and parts[0]=='cmd':
        state=tg.commands.picks.get(parts[1])
        if state and state['chat']==chat and state.get('thread')==getattr(chat,'thread',None):
            ctx=tg.app.agents.get(state['agent']);sid=state['session']
            kind=state['kind'];label='хозяин нажал кнопку: '+{'agent':'выбор агента','chat':'выбор разговора','correspondence_view':'просмотр переписки','model':'выбор модели','profile':'публичный профиль','control':'управление запуском','file':'отправка файла','report':'технический отчёт','topic_route':'выбор темы'}.get(kind,'выбор')
    elif len(parts)>=3 and parts[0] in ('ap','q'):
        pending=(tg.app.guard.pending if parts[0]=='ap' else tg.app.questions).get(parts[1],{})
        card=pending.get('card',{})
        if card:
            ctx=tg.app.agents.get(card.get('agent_id','main'));sid=card.get('session') or ctx.main_session()
        label='хозяин нажал кнопку: '+('одобрение — '+{'once':'один раз','task':'в разговоре','always':'всегда','deny':'отказ'}.get(parts[2],'устарело') if parts[0]=='ap' else 'ответ на вопрос')
    elif parts[0]=='sk':
        label='хозяин нажал кнопку: убрать навык'
        notice=tg.app.store.meta_get('skill.notice.'+data[3:])
        if notice:
            ctx=tg.app.agents.get(notice.get('agent_id','main'));sid=ctx.main_session()
    return ctx,sid,chat,label


async def edited(tg,msg):
    owner=tg.owner() or {};frm=msg.get('from') or {};chat=msg.get('chat') or {}
    if not owner or frm.get('is_bot') or frm.get('id')!=owner.get('user_id') or chat.get('id') is None:return
    key=str(chat['id'])+':'+str(msg.get('message_id'))
    route=tg.app.store.meta_get('telegram:message_routes',{}).get(key)
    if not route:return
    from ..core.errors import HumanError
    try:ctx=tg.app.agents.get(route['agent'])
    except HumanError:return
    refs=ctx.store.meta_get('telegram:refs',{});ref=refs.get(key)
    if not ref or ref.get('direction')!='in' or ref.get('session')!=route['session']:return
    if 'thread' in ref and ref['thread']!=msg.get('message_thread_id'):return
    if chat.get('type')!='private' and not tg.groups.allowed(chat['id']):return
    old=ref.get('source_text',ref.get('text',''));new=body(msg)
    if not isinstance(new,str):return
    fingerprint=digest(new)
    if ref.get('source_hash')==fingerprint or (not ref.get('source_hash') and old==new):return
    if ref.get('command'):
        ref.update(source_hash=fingerprint,edit_date=msg.get('edit_date'))
        refs[key]=ref;ctx.store.meta_set('telegram:refs',refs)
        address=tg.topics.address(chat['id'],msg.get('message_thread_id'))
        record(ctx,route['session'],address,'хозяин исправил команду #'+str(msg.get('message_id'))+'; новая версия не выполнялась',meta={'telegram_edit':msg.get('message_id')})
        return
    ref.update(text=new[:4000],source_text=new[:4096],source_hash=fingerprint,edit_date=msg.get('edit_date'))
    refs[key]=ref;ctx.store.meta_set('telegram:refs',refs)
    mode=ctx.settings.get('telegram','history_events','brief');limit=180 if mode=='brief' else 450
    from ..mind.passport import clean
    old=clean(ctx,old,stream=True);new=clean(ctx,new,stream=True)
    address=tg.topics.address(chat['id'],msg.get('message_thread_id'))
    tainted=bool(ref.get('tainted') or any(msg.get(k) for k in ('forward_origin','external_reply')))
    record(ctx,route['session'],address,'хозяин исправил #'+str(msg.get('message_id'))+': '+change(old,new,limit),
        tainted=tainted,continue_turn=True,meta={'telegram_edit':msg.get('message_id'),'edit_date':msg.get('edit_date')})
