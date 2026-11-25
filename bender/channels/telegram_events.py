"""Факты Telegram в контексте: команды и кнопки без самостоятельного хода; правки хозяина — было → стало."""
from contextvars import ContextVar
import hashlib
import time
from .telegram_rich_in import body

REPLIES=ContextVar('telegram_command_replies',default=None)
WAKE_WINDOW=30*60  # правка будит ход (edits=wake), только если исправленному не больше 30 минут
# Ответ без модели виден агенту: состояние и переключения. Закрыты recall/files (недоверенный текст),
# report/backup/settings (пути и подробности, агенту ни к чему) и всё неизвестное.
SAFE_REPLIES={'effort','status','fuel','live','voice','whoami','model','models','compact','style','sleep','heart','stop','resume','new','remember'}


def digest(text):return hashlib.sha256(text.encode('utf-8','replace')).hexdigest()


SHORT=300   # до стольких знаков правка показывается целиком: было/стало
HUNKS=5     # длинный текст — не больше стольких мест правки
CONTEXT=40  # знаков вокруг места правки
EDIT_TAIL='Учитывай новую версию; уже сделанное по старой не переделывай без его просьбы.'


def _cut(value,limit):
    return value if len(value)<=limit else value[:limit-1].rstrip()+'…'


def _tail(value):
    """Последние CONTEXT знаков по границе слова."""
    if len(value)<=CONTEXT:return value.strip()
    value=value[-CONTEXT:]
    return '…'+(value.split(' ',1)[1] if ' ' in value else value).strip()


def hunks(old,new,limit=HUNKS):
    """Места правки длинного текста: слова сравнивает difflib, вокруг каждого места — немного контекста."""
    import difflib
    import re
    a=re.findall(r'\S+|\s+',old);b=re.findall(r'\S+|\s+',new)
    groups=list(difflib.SequenceMatcher(None,a,b,autojunk=False).get_grouped_opcodes(8))
    lines=[]
    for group in groups[:limit]:
        changes=[op for op in group if op[0]!='equal']
        if not changes:continue
        first=changes[0]
        anchor=''.join(a[group[0][1]:first[1]])
        anchor=_tail(anchor) if anchor.strip() else ''
        if anchor and group[0][1]>0 and not anchor.startswith('…'):anchor='…'+anchor
        where=' после «'+anchor+'»' if anchor else ' в начале' if first[1]==0 else ''
        kinds={op[0] for op in changes}
        if kinds=={'insert'}:
            lines.append('• добавлено «'+_cut(''.join(b[changes[0][3]:changes[-1][4]]).strip(),240)+'»'+where);continue
        if kinds=={'delete'}:
            lines.append('• удалено «'+_cut(''.join(a[changes[0][1]:changes[-1][2]]).strip(),240)+'»'+where);continue
        left=''.join(a[group[0][1]:group[-1][2]]);right=''.join(b[group[0][3]:group[-1][4]])
        before=group[0][1]>0;after=group[-1][2]<len(a)  # текст есть и за пределами этого места
        prefix=0
        while prefix<min(len(left),len(right)) and left[prefix]==right[prefix]:prefix+=1
        suffix=0
        while suffix<min(len(left),len(right))-prefix and left[-1-suffix]==right[-1-suffix]:suffix+=1
        start=max(0,prefix-CONTEXT)
        def part(value):
            end=len(value)-max(0,suffix-CONTEXT)
            return ('…' if start or before else '')+_cut(value[start:end].strip(),240)+('…' if end<len(value) or after else '')
        lines.append('• «'+part(left)+'» → «'+part(right)+'»')
    if len(groups)>limit:lines.append('• … и ещё мест правки: '+str(len(groups)-limit))
    return lines


def when(ts,now=None):
    """Когда было отправлено исправленное: «10:05», «21.11 10:05» или с годом."""
    from ..core.dates import local
    try:value=local(ts);current=local(now)
    except (OSError,OverflowError,ValueError,TypeError):return ''
    if value.date()==current.date():return value.strftime('%H:%M')
    return value.strftime('%d.%m %H:%M' if value.year==current.year else '%d.%m.%Y %H:%M')


def edit_text(number,sent,old,new,*,now=None,media_note='',command=False):
    """Правка хозяина для модели: новая версия важна, но сделанное по старой не переделывается без просьбы."""
    stamp=when(sent,now) if sent else ''
    head='Хозяин исправил '+('команду' if command else 'своё сообщение')+' #'+str(number)+(' (от '+stamp+')' if stamp else '')
    if command:return '['+head+'; новая версия не выполнялась.]'
    if media_note:head+=': '+media_note
    lines=['['+head+'. '+EDIT_TAIL]
    if old!=new:
        if len(old)<=SHORT and len(new)<=SHORT:lines+=['было: '+old,'стало: '+new]
        else:lines+=['Изменения:',*hunks(old,new)]
    return '\n'.join(lines)+']'


def capture(chat,text):
    """Ответ команды в её чат: текст — в историю, список номеров (None — не команда) — для маршрута ответа цитатой."""
    record=REPLIES.get()
    if record is not None and int(chat)==record['chat'] and getattr(chat,'thread',None)==record['thread']:
        from ..mind.passport import clean
        record['reply']=clean(record['ctx'],str(text),stream=True)[:2000]
        return record['ids']
    return None


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
    captured={'chat':int(chat),'thread':getattr(chat,'thread',None),'reply':'','ctx':ctx,'ids':[]}
    token=REPLIES.set(captured)
    try:handled=await commands.dispatch(ctx,chat,sid,command,arg)
    finally:REPLIES.reset(token)
    if handled:
        name=command if command in {name for name,_ in COMMANDS} else 'неизвестная-команда'
        reply=captured['reply'] if command in SAFE_REPLIES else ''
        record(ctx,sid,chat,'хозяин вызвал /'+name,reply=reply,meta={'telegram_command':name})
        from ..core.errors import HumanError
        try:selected,new_sid=commands.tg.selected(chat)
        except HumanError:selected,new_sid=None,None
        # Ответ цитатой на вывод команды (/status, /load…) — агенту, с которым хозяин говорит после неё:
        # тому же, или новому после /agent. Иначе он ушёл бы тому, кто выбран в момент ответа.
        if captured['ids']:commands.tg._route_only(selected or ctx,chat,list(dict.fromkeys(captured['ids'])),new_sid if selected else sid)
        if selected is None:return handled
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
            kind=state['kind'];label='хозяин нажал кнопку: '+{'agent':'выбор агента','chat':'выбор разговора','correspondence_view':'просмотр переписки','model':'выбор модели','profile':'публичный профиль','control':'управление запуском','file':'отправка файла','report':'технический отчёт','topic_route':'выбор темы','new_message':'сообщение во время хода'}.get(kind,'выбор')
    elif len(parts)>=3 and parts[0] in ('ap','q'):
        pending=(tg.app.guard.pending if parts[0]=='ap' else tg.app.questions).get(parts[1],{})
        card=pending.get('card',{})
        if card:
            ctx=tg.app.agents.get(card.get('agent_id','main'));sid=card.get('session') or ctx.main_session()
        label='хозяин нажал кнопку: '+('одобрение — '+{'once':'один раз','task':'в разговоре','always':'всегда','deny':'отказ'}.get(parts[2],'устарело') if parts[0]=='ap' else 'ответ на вопрос')
    elif parts[0]=='nm' and len(parts)==2:
        target=tg._status_messages.get((int(chat),(cq.get('message') or {}).get('message_id')))
        if target:
            from ..core.errors import HumanError
            try:ctx,sid=tg.app.agents.get(target[0]),target[1]
            except HumanError:pass
        label='хозяин нажал кнопку: своё сообщение во время хода — '+{'interrupt':'прервать и ответить','queue':'после хода','insert':'вставить в ход'}.get(parts[1],'устарело')
    elif parts[0]=='sk':
        label='хозяин нажал кнопку: убрать навык'
        notice=tg.app.store.meta_get('skill.notice.'+data[3:])
        if notice:
            ctx=tg.app.agents.get(notice.get('agent_id','main'));sid=ctx.main_session()
    return ctx,sid,chat,label


def observe_edit(ctx,sid,chat,text,*,tainted=False,meta=None,content=None):
    """Строка правки в истории: не служебный «факт», а поправка хозяина; идёт ход — встаёт в него."""
    ctx.mind.observe(sid,text,tainted=tainted,continue_turn=True,content=content,
        meta={**(meta or {}),'telegram_route':{'chat':int(chat),'thread':getattr(chat,'thread',None)}})


def mark_edited(ctx,sid,chat,mid,info):
    """«изменено» у исходной строки — только meta для пульта: текст, который видела модель, не переписывается."""
    from ..core.store import dumps
    ctx.store.x("UPDATE messages SET meta=json_set(meta,'$.edited',json(?)) WHERE session_id=? AND role='user' AND json_extract(meta,'$.telegram_chat_id')=? "
                "AND (json_extract(meta,'$.telegram_message_id')=? OR EXISTS (SELECT 1 FROM json_each(meta,'$.telegram_message_ids') WHERE value=?))",
                (dumps(info),sid,int(chat),mid,mid))
    ctx.mind.publish(sid,{'type':'message_meta','telegram_chat_id':int(chat),'telegram_message_id':mid,'edited':info})


def latest(ctx,sid,mid):
    """Исправлено последнее сообщение хозяина в этом разговоре?"""
    import json
    for row in ctx.store.q("SELECT meta FROM messages WHERE session_id=? AND role='user' ORDER BY id DESC LIMIT 30",(sid,)):
        meta=json.loads(row['meta'] or '{}')
        if meta.get('service_event') or meta.get('actor')!='owner':continue
        # Правка, которая уже будила ход (edits=wake), — тоже это сообщение: повторная правка будит снова.
        return meta.get('telegram_message_id')==mid or mid in (meta.get('telegram_message_ids') or []) or meta.get('telegram_edit')==mid
    return False


async def unknown(tg,msg):
    """Правка сообщения, которого Бендер не помнит (старое, до подключения): в выбранный разговор лички."""
    from ..core.errors import HumanError
    chat=msg['chat'];mid=msg.get('message_id')
    try:
        address=tg.topics.address(chat['id'],msg.get('message_thread_id'))
        ctx,sid=tg.selected(address)
    except HumanError:return
    if ctx.settings.get('telegram','edits','show')=='off' or not ctx.store.session(sid):return
    new=body(msg)
    if not isinstance(new,str) or not new.strip():return
    from ..mind.passport import clean
    stamp=when(msg.get('date')) if msg.get('date') else ''
    where='#'+str(mid)+(' (от '+stamp+')' if stamp else '')
    command=new.lstrip().startswith('/')
    if command:text='[Хозяин исправил старую команду '+where+'; новая версия не выполнялась.]'
    else:text='[Хозяин исправил старое сообщение '+where+'; прежний текст неизвестен. '+EDIT_TAIL+'\nТеперь: '+clean(ctx,new,stream=True,urls=False)[:4000]+']'
    shown=clean(ctx,new,stream=True,urls=False).strip()
    observe_edit(ctx,sid,address,text,meta={'telegram_edit':mid,'edit_date':msg.get('edit_date'),'display':'✏️ Исправлено старое сообщение #'+str(mid)+('' if command else ': стало «'+_cut(shown,160)+'»')})
    # Дальше это сообщение известно: следующая правка покажет было → стало. Команда — без аргументов, как обычно.
    from .telegram_forward import media
    if isinstance(mid,int):
        stored='[команда Telegram]' if command else new
        tg.remember(ctx,address,mid,sid,stored,'in',source_text=stored,command=command,items={mid:{'media':media(msg)}})
        if command:
            from . import telegram_refs
            telegram_refs.update_ref(ctx,address,mid,source_hash=digest(new))  # та же версия ещё раз — не новая правка
    ctx.journal.write('channel','Telegram: хозяин исправил сообщение, которого Бендер не помнит',actor='owner',data={'message':mid})


async def edited(tg,msg):
    owner=tg.owner() or {};frm=msg.get('from') or {};chat=msg.get('chat') or {}
    if not owner or frm.get('is_bot') or frm.get('id')!=owner.get('user_id') or chat.get('id') is None:return
    from . import telegram_refs
    from ..core.errors import HumanError
    private=chat.get('type')=='private'
    if not private and not tg.groups.allowed(chat['id']):return
    mid=msg.get('message_id')
    route=telegram_refs.route(tg.app,chat['id'],mid)
    ref=None
    if route:
        try:ctx=tg.app.agents.get(route['agent'])
        except HumanError:return
        ref=telegram_refs.ref(ctx,chat['id'],mid)
    if not ref:
        if private:await unknown(tg,msg)  # в группе неизвестное — скорее всего не обращение к боту
        return
    if ref.get('direction')!='in' or ref.get('session')!=route['session']:return
    # Номер сообщения уникален в чате; тема сверяется, только если она запомнена (в группе её нет).
    if ref.get('thread') is not None and ref['thread']!=msg.get('message_thread_id'):return
    from .telegram_forward import media,media_change
    old=ref.get('source_text',ref.get('text',''));new=body(msg)
    if not isinstance(new,str):return
    fingerprint=digest(new)
    old_media=ref.get('media');new_media=media(msg)
    replaced=isinstance(old_media,list) and [i.get('id') for i in old_media]!=[i.get('id') for i in new_media]
    if (ref.get('source_hash')==fingerprint or (not ref.get('source_hash') and old==new)) and not replaced:return
    sid=route['session'];address=tg.topics.address(chat['id'],ref.get('thread'))
    # Команда хранится без текста (аргументы не копируются) — у неё обновляется только отпечаток.
    telegram_refs.update_ref(ctx,chat['id'],mid,**({} if ref.get('command') else {'text':new[:4000],'source_text':new[:4096]}),source_hash=fingerprint,
                             edit_date=msg.get('edit_date'),media=new_media)
    mode=ctx.settings.get('telegram','edits','show')
    if mode=='off' or not ctx.store.session(sid):return
    meta={'telegram_edit':mid,'edit_date':msg.get('edit_date'),'display':'✏️ Исправлена команда #'+str(mid)}
    if ref.get('command'):
        observe_edit(ctx,sid,address,edit_text(mid,msg.get('date'),'','',command=True),meta=meta)
        return
    from ..mind.passport import clean
    raw_old,raw_new=old,new
    # Ссылки в правке хозяина видны: скрываются только известные секреты и адреса с логином.
    old=clean(ctx,old,stream=True,urls=False);new=clean(ctx,new,stream=True,urls=False)
    tainted=bool(ref.get('tainted') or any(msg.get(k) for k in ('forward_origin','external_reply')))
    info={'at':msg.get('edit_date'),'old':old[:600],'new':new[:600],**({'media':True} if replaced else {})}
    # Очередь хода держит сообщение как пришло — там и правится исходный текст хозяина.
    if not replaced and raw_old.strip() and _amend(ctx,sid,chat['id'],mid,raw_old.strip(),raw_new.strip(),info):return
    attachments=[];note=''
    if replaced:
        note=media_change(old_media,new_media)
        if new_media:
            try:attachments=await tg.receive_media(ctx,[msg],sid)
            except Exception as error:  # noqa: BLE001 — правка доходит, даже если файл не скачался
                note+=' (новый файл не скачан: '+str(getattr(error,'title','') or error)[:160]+')'
            for item in attachments:item.pop('_message',None)
            if any(item['kind']!='voice' for item in attachments):tainted=True
    text=edit_text(mid,msg.get('date'),old,new,media_note=note)
    # Пульт: короткая строка вместо служебного текста для модели.
    meta['display']='✏️ Исправлено сообщение #'+str(mid)+(': '+note if note else '')+(': было «'+_cut(old.strip(),160)+'» → стало «'+_cut(new.strip(),160)+'»' if old!=new else '')
    content=None
    if attachments:
        from . import media as files
        content=files.content(text,attachments,vision=ctx.settings.get('attachments','vision',True))
        meta['attachments']=attachments
    mark_edited(ctx,sid,chat['id'],mid,info)
    wake=(mode=='wake' and not ctx.mind.running(sid) and latest(ctx,sid,mid) and isinstance(msg.get('date'),(int,float))
          and (msg.get('edit_date') or time.time())-msg['date']<=WAKE_WINDOW)
    if wake:
        tg._bg_task(tg._owner_text(address,None,text,tainted=tainted,attachments=attachments,agent_ctx=ctx,session_id=sid,extra_meta=meta,edit_wake=True))
        return
    observe_edit(ctx,sid,address,text,tainted=tainted,meta=meta,content=content)


def _amend(ctx,sid,chat,mid,old,new,info):
    """Сообщение ещё в очереди идущего хода — модель его не видела: правится сама запись, без строки «было → стало»."""
    def match(msg,meta):
        return meta.get('telegram_chat_id')==int(chat) and (meta.get('telegram_message_id')==mid or mid in (meta.get('telegram_message_ids') or []))
    def change(msg,meta):
        value=msg.get('content')
        if isinstance(value,str):
            if old not in value:return None
            value=value.replace(old,new,1)
        elif isinstance(value,list):
            index=next((n for n,p in enumerate(value) if isinstance(p,dict) and p.get('type')=='text' and old in str(p.get('text') or '')),None)
            if index is None:return None
            value=list(value);value[index]={**value[index],'text':str(value[index]['text']).replace(old,new,1)}
        else:return None
        return {**msg,'content':value},{**meta,'edited':info}
    return ctx.mind.amend_queued(sid,match,change)
