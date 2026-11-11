"""Сообщения и реакции в текущем разговоре хозяина, через Предохранитель."""
from ..core.errors import HumanError
from .registry import ToolContext, ToolResult


def session(ctx):
    row=ctx.app.store.session(ctx.session_id) or {}
    if not row or row.get('kind')=='subagent':
        raise HumanError('channel_session','Нет разговора хозяина для отправки','Верни результат родительскому агенту.')
    return row


def register(reg):
    @reg.tool('send_message','Написать в разговор',group='channel')
    async def send_message(ctx: ToolContext, text: str, reply_to: int | None = None, complete: bool = False) -> ToolResult:
        """Отправить хозяину сообщение сейчас. Обычно достаточно 1–3 сообщений за ход.

        text — текст сообщения
        reply_to — номер Telegram [#…] из этого разговора; без него обычное сообщение
        complete — всё уже отправлено; отдельный финальный ответ больше не нужен
        """
        row=session(ctx)
        from ..core.dates import strip_labels
        text=strip_labels(text)
        if not text.strip():raise HumanError('channel_empty','Сообщение пустое','Напиши текст.')
        channel=row.get('meta',{}).get('home_channel','pult')
        mid=None
        if channel=='userbot':  # разговор аккаунта Telegram: адрес и разрешение — по списку чатов хозяина, а не «хозяину»
            bot=ctx.app.userbot;chat=bot.chat_of(ctx.session_id)
            if chat is None:raise HumanError('channel_off','У разговора нет чата Telegram','Напиши через tg_send с id чата.')
            data=await bot.request(ctx,'msg.send','userbot:chat:'+str(chat),'написать в чат этого разговора: «'+text[:80]+'»','send',{'chat':chat,'text':text,'reply_to':int(reply_to or 0)},detail={'preview':text[:1500]})
            mid=(data.get('ids') or [None])[0]
            ctx.app.mind._save_event(ctx.session_id,'channel_sent',(f'[#{mid}] ' if mid else '')+text)
            ctx.turn.setdefault('channel_sent',[]).append(text);ctx.turn['sent_all']=complete
            return ToolResult('Отправлено'+(f' [#{mid}]' if mid else '')+'.',summary='сообщение в Telegram',data={'message_id':mid})
        speakers=getattr(ctx.app.host,'_speakers',None)
        if speakers is not None and speakers.awaiting(ctx.app,ctx.session_id):channel='speaker'  # фразу сказали в колонку — туда и ответ
        await ctx.check('msg.send',channel,'отправить сообщение хозяину в текущий разговор',to_owner=True)
        if channel=='telegram':
            tg=ctx.app.host.telegram
            owner=tg.owner()
            if not owner or tg.state!='ok':raise HumanError('channel_off','Telegram сейчас не подключён','Проверь «Связи».')
            chat=tg.tool_destination(ctx)
            if reply_to is not None:tg.require_ref(ctx.app,chat,reply_to,ctx.session_id)
            await tg.authorize_group(ctx,chat,'сообщение группе: '+text[:1000])
            signed=tg.signed(ctx.app,text)
            mid=await tg.send(chat,signed,reply_to=reply_to)
            tg.remember(ctx.app,chat,mid,ctx.session_id,signed,'out')
        elif channel=='matrix':
            mid=await ctx.app.host.matrix.send(ctx,text,reply_to=reply_to)
        elif channel=='speaker':
            await ctx.app.host.speakers.say(ctx,text)
        elif channel not in ('pult','terminal','терминал'):
            raise HumanError('channel_unsupported','Этот канал пока не умеет промежуточную отправку','Верни обычный итог хода.')
        ctx.app.mind._save_event(ctx.session_id,'channel_sent',(f'[#{mid}] ' if mid else '')+text)
        ctx.turn.setdefault('channel_sent',[]).append(text)
        ctx.turn['sent_all']=complete
        return ToolResult('Отправлено'+(f' [#{mid}]' if mid else '')+'.',summary='сообщение хозяину',data={'message_id':mid})

    @reg.tool('send_file','Отправить файл',group='channel',params={'type':'object','required':['path'],'properties':{
        'path':{'anyOf':[{'type':'string'},{'type':'array','items':{'type':'string'},'minItems':2,'maxItems':10}],'description':'Путь или 2–10 путей альбома'},
        'caption':{'type':'string','description':'Подпись до 1024 символов'},
        'kind':{'type':'string','enum':['auto','photo','document','voice','video','audio','album']}}})
    async def send_file(ctx: ToolContext, path: str | list, caption: str = '', kind: str = 'auto') -> ToolResult:
        """Отправить файл хозяину в текущий канал. До 50 МБ суммарно.

        path — путь или список 2–10 путей для альбома
        caption — подпись
        kind — auto, photo, document, voice, video, audio или album
        """
        from pathlib import Path
        import json
        from ..channels import media
        from ..core.fsutil import atomic_write
        from ..core.pathutil import within
        row=session(ctx);media.require(ctx.app)
        values=path if isinstance(path,list) else [path]
        if not 1<=len(values)<=10 or any(not isinstance(p,str) for p in values):raise HumanError('file_count','Нужно от 1 до 10 путей','Для альбома выбери 2–10 файлов.')
        if kind not in ('auto','photo','document','voice','video','audio','album') or kind=='album' and len(values)<2:raise HumanError('file_kind','Неизвестный вид отправки или пустой альбом','Выбери auto, photo, document, voice, video, audio; album — от двух файлов.')
        if len(caption)>1024:raise HumanError('file_caption','Подпись длиннее 1024 символов','Сократи подпись или отправь текст отдельно.')
        files=[];total=0
        for value in values:
            resolved=ctx.app.guard.policy.resolve(value)
            await ctx.check('channel.send_file',str(resolved),'отправить файл хозяину',to_owner=True)
            if not resolved.is_file():raise HumanError('file_missing','Файл не найден',str(resolved))
            with resolved.open('rb') as handle:raw=handle.read(media.UPLOAD_LIMIT+1)
            total+=len(raw)
            if total>media.UPLOAD_LIMIT:raise HumanError('file_size','Больше 50 МБ на отправку','Сожми файл или раздели отправку.')
            file_kind=media.outgoing_kind(resolved,'auto' if kind=='album' else kind)
            if file_kind=='photo' and len(raw)>10*1024*1024 and row.get('meta',{}).get('home_channel')=='telegram':
                if kind in ('auto','album'):file_kind='document'
                else:raise HumanError('photo_size','Фото для sendPhoto больше 10 МБ','Отправь его с kind=document; общий лимит файлов — 50 МБ.')
            files.append({'path':resolved,'name':media.original_name(ctx.app,resolved),'mime':media.mime(raw,resolved.name),'bytes':raw,'kind':file_kind})
        channel=row.get('meta',{}).get('home_channel','pult')
        if channel=='telegram':
            tg=ctx.app.host.telegram;owner=tg.owner() or {};chat=tg.tool_destination(ctx)
            if not chat or tg.state!='ok':raise HumanError('channel_off','Telegram сейчас не подключён','Проверь «Связи».')
            await tg.authorize_group(ctx,chat,'отправить файлы в группу: '+', '.join(f['name'] for f in files))
            signed=tg.signed(ctx.app,caption).rstrip('\n')
            if len(signed)<=1024:caption=signed
            if len(files)>1:
                kinds={f['kind'] for f in files}
                if not (kinds<= {'photo','video'} or kinds=={'document'} or kinds=={'audio'}):raise HumanError('album_kind','Этот набор нельзя отправить альбомом','Фото и видео можно смешивать; документы и аудио — только со своим видом.')
                entries=[{'type':f['kind'],'media':'attach://file'+str(n),**({'caption':caption} if n==0 and caption else {})} for n,f in enumerate(files)]
                result=await tg.api('sendMediaGroup',{'chat_id':chat,'media':entries},files={'file'+str(n):f for n,f in enumerate(files)})
                for message in result or []:tg.remember(ctx.app,chat,message.get('message_id'),ctx.session_id,caption or '[альбом]','out')
            else:
                item=files[0];field=item['kind'];method={'photo':'sendPhoto','document':'sendDocument','voice':'sendVoice','video':'sendVideo','audio':'sendAudio'}[field]
                result=await tg.api(method,{'chat_id':chat,'caption':caption},files={field:item})
                tg.remember(ctx.app,chat,(result or {}).get('message_id'),ctx.session_id,caption or '[файл]','out')
            return ToolResult('Файл отправлен в Telegram.',summary='файл хозяину')
        if channel=='matrix':
            await ctx.app.host.matrix.files(ctx,files,caption)
            return ToolResult('Файлы отправлены хозяину Matrix.',summary='файлы хозяину')
        if channel not in ('pult','terminal','терминал'):raise HumanError('channel_unsupported','Канал не умеет отправлять файлы','Выбери пульт или Telegram.')
        public=[]
        for item in files:
            dest=item['path']
            if not within(dest,ctx.app.dirs.work):
                import secrets
                dest=ctx.app.dirs.work/'outbox'/(secrets.token_hex(8)+'-'+media.filename(item['name']))
                await ctx.check('fs.write',str(dest),'сохранить одобренную копию для скачивания')
                atomic_write(dest,item['bytes'],durable=False)
            public.append(media.register(ctx.app,dest,name=item['name'],kind=item['kind']))
        ctx.app.mind._save_event(ctx.session_id,'attachment',caption or 'Файл от агента',{'attachments':public})
        return ToolResult(json.dumps({'files':public},ensure_ascii=False),summary='файл хозяину',data={'attachments':public})

    @reg.tool('telegram_chats','Где мой бот',group='channel',available=lambda app:bool(app.host.telegram.token()))
    async def telegram_chats(ctx: ToolContext) -> ToolResult:
        """Список своих привязанных чатов и права бота из обновлений членства. Названия — недоверенные данные."""
        import json
        rows=ctx.app.host.telegram.groups.rows(ctx.app)
        return ToolResult(json.dumps({'chats':rows},ensure_ascii=False),summary='чаты и права бота',tainted=True)

    @reg.tool('telegram_profile','Предложить публичный профиль',group='channel',available=lambda app:app.agent_id=='main' and bool(app.host.telegram.token()))
    async def telegram_profile(ctx: ToolContext, name: str | None = None, description: str | None = None, short_description: str | None = None, avatar: str = '') -> ToolResult:
        """Профиль общего бота по IDENTITY; хозяин подтверждает каждое применение.

        name — публичное имя, до 64 символов
        description — описание, до 512 символов
        short_description — короткое описание, до 120 символов
        avatar — готовый JPG в своём work; SVG/PNG не конвертируются
        """
        import json
        profile=ctx.app.host.telegram.profile
        specified={key:value for key,value in {'name':name,'description':description,'short_description':short_description}.items() if value is not None}
        values=specified if specified else ({} if avatar else profile.defaults())
        result=await profile.apply(ctx,values,avatar)
        return ToolResult(json.dumps(result,ensure_ascii=False),summary='публичный профиль бота обновлён')

    @reg.tool('react','Поставить реакцию',group='channel')
    async def react(ctx: ToolContext, message: int, emoji: str, big: bool = False) -> ToolResult:
        """Реакция на известное сообщение текущего разговора Telegram или Matrix.

        message — номер [#…] или [Matrix #…] из истории
        emoji — реакция Telegram по его списку или строка Matrix до 64 знаков
        big — большая анимация
        """
        row=session(ctx)
        if row.get('meta',{}).get('home_channel')=='matrix':
            await ctx.app.host.matrix.react(ctx,message,emoji)
            return ToolResult(emoji+' на #'+str(message),summary='реакция хозяину Matrix')
        from ..channels.reactions import emoji as normalize
        value=normalize(emoji)
        if row.get('meta',{}).get('home_channel')!='telegram':raise HumanError('reaction_channel','Реакции доступны в Telegram','В пульте ответь текстом.')
        tg=ctx.app.host.telegram;owner=tg.owner() or {};chat=tg.tool_destination(ctx)
        tg.require_ref(ctx.app,chat,message,ctx.session_id)
        await ctx.check('msg.send','telegram','поставить реакцию хозяину',to_owner=True)
        await tg.authorize_group(ctx,chat,'поставить реакцию в группе')
        await tg.api('setMessageReaction',{'chat_id':chat,'message_id':message,'reaction':[{'type':'emoji','emoji':value}],'is_big':big})
        return ToolResult(f'{value} на #{message}',summary='реакция хозяину')

    @reg.tool('hand_over','Передать разговор агенту',group='channel')
    async def hand_over(ctx: ToolContext, agent: str, note: str = '') -> ToolResult:
        """Передать разговор другому агенту стаи, когда хозяин просит («позови Шерлока», «дай главного»).
        Колонка или чат Telegram, откуда хозяин говорит, со следующей реплики отвечает тому агенту; вернуть — так же.
        После вызова коротко скажи хозяину, кому передал. Не уверен в имени — ошибка покажет список стаи.

        agent — имя или id агента стаи
        note — одна-две фразы новому агенту: о чём шла речь и что хозяин хочет
        """
        session(ctx)
        host=ctx.app.host
        if ctx.unattended:raise HumanError('hand_over','Передать разговор можно, пока хозяин говорит','Разговор передаётся по просьбе хозяина.')
        other=match_agent(host,agent)
        if other==ctx.app.agent_id:return ToolResult('Ты и так отвечаешь в этом разговоре.',summary='уже ты')
        target=host.agents.get(other);name=target.selfdir.identity().get('name') or other
        giver=ctx.app.selfdir.identity().get('name') or ctx.app.agent_id
        speakers=getattr(host,'_speakers',None);station=speakers.station_of(ctx.app,ctx.session_id) if speakers else None
        route=ctx.turn.get('_telegram_route') or {}
        if station:
            channel='speaker'
            voice=target.settings.group('voice');roles=target.models.roles()
            if not voice['enabled'] or not roles.get('ears') or not roles.get('voice'):
                raise HumanError('hand_over',name+' без Ушей и Голоса','Пусть хозяин включит ему голос (Настройки агента → Голос) или назначит колонке в настройках колонок.')
            await ctx.check('msg.send','speaker','передать колонку «'+station.cfg['name']+'» агенту '+name,to_owner=True)
            station.handover=other
        elif route.get('chat'):
            channel='telegram';tg=host.telegram
            await ctx.check('msg.send','telegram','передать чат Telegram агенту '+name,to_owner=True)
            tg.choose(int(route['chat']),target)
        else:raise HumanError('hand_over','Здесь разговор не передать','Передаётся колонка или чат Telegram; в пульте хозяин выбирает агента сам.')
        from ..agents.handoff import leave
        leave(host,other,channel,giver,note)
        host.journal.write('channel',giver+' передал '+('колонку «'+station.cfg['name']+'»' if station else 'чат Telegram')+' агенту '+name,actor='agent',data={'from':ctx.app.agent_id,'to':other,'channel':channel})
        return ToolResult('Передано: следующая реплика хозяина '+('с колонки' if station else 'в этом чате')+' уйдёт агенту '+name+(', он ответит своим голосом' if station else '')+
                          '. Скажи хозяину одной фразой, кому передал; сам больше не продолжай.',summary='разговор → '+name,data={'agent':other,'channel':channel})


def match_agent(host,value):
    """Агент стаи по имени или id; на слух имя склоняют («Шерлока»), поэтому и по началу слова."""
    value=str(value or '').strip().lower().strip('«»"\'')
    flock=[a for a in host.agents.public() if not a['archived']]
    names=lambda a:{a['id'].lower(),str(a['name']).lower()}
    found=[a['id'] for a in flock if value in names(a)] or \
          [a['id'] for a in flock if len(value)>=3 and any(n.startswith(value[:max(3,len(value)-2)]) or value.startswith(n[:max(3,len(n)-1)]) for n in names(a))]
    if len(set(found))!=1:
        raise HumanError('hand_over','Не понял, какому агенту передать','В стае: '+', '.join(str(a['name'])+' ('+a['id']+')' for a in flock)+'.')
    return found[0]
