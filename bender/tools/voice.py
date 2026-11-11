"""Бытовое подключение бесплатного установленного голоса и ответ в текущий разговор."""
from .registry import ToolContext,ToolResult


def register(reg):
    @reg.tool('voice_connect','Подключить локальный голос',group='self')
    async def voice_connect(ctx:ToolContext)->ToolResult:
        """Подключить себе уже установленный бесплатный локальный голос (Уши/Голос).
        Не устанавливает пакеты, не запускает службу и не включает передачу сырого звука главной модели.
        """
        from ..voice.connect import connect
        result=await connect(ctx)
        return ToolResult(result['note'],summary='подключение локального голоса',data=result)

    @reg.tool('speak','Ответить голосом',group='channel')
    async def speak(ctx:ToolContext,text:str,complete:bool=False)->ToolResult:
        """Озвучить текст хозяину в текущем разговоре; адресата задать нельзя.
        В Telegram настоящий OGG/Opus; при недоступной озвучке — текст с причиной.
        text — текст ответа, до 20 000 знаков
        complete — всё отправлено, отдельный финальный ответ больше не нужен
        """
        from ..core.errors import HumanError
        from ..net.http_client import NetError
        from .channel import session
        row=session(ctx)
        if row.get('kind') not in ('main','chat'):raise HumanError('voice_session','Это не разговор с хозяином','Верни ответ родительскому агенту или отправителю письма.')
        if not isinstance(text,str) or not text.strip() or len(text)>20000:raise HumanError('voice_text','Нужен текст до 20 000 знаков','Сократи ответ.')
        channel=row.get('meta',{}).get('home_channel','pult');tg=ctx.app.host.telegram;chat=None
        if channel=='telegram':
            owner=tg.owner() or {};private=owner.get('chat_id') or owner.get('user_id')
            route=ctx.turn.get('_telegram_route') or {}
            if route.get('chat') and route['chat']!=private:raise HumanError('voice_channel','Голосовой тул не отправляет сообщения в группу','Ответь там обычным текстом.')
            chat=tg.tool_destination(ctx)
            if not private or tg.state!='ok' or chat!=private:raise HumanError('voice_channel','Голосовой тул доступен только в разговоре хозяина','В группе ответь обычным текстом; настройки Telegram — в «Связи».')
        elif channel not in ('pult','terminal','терминал'):raise HumanError('voice_channel','Этот канал не принимает голос','Ответь обычным текстом.')
        await ctx.check('msg.send',channel,'отправить голосовой ответ в текущий разговор хозяина',to_owner=True)
        failure='';result=None
        try:result=await ctx.app.voice.speak(text,session_id=ctx.session_id,format='opus' if channel=='telegram' else 'wav',context=ctx)
        except (HumanError,NetError) as error:failure=str(error)
        mid=None;attachments=[]
        if channel=='telegram':
            if result:
                try:mid=await tg.send_voice(chat,result['audio'])
                except HumanError as error:failure=str(error)
            if failure:mid=await tg.send(chat,tg.signed(ctx.app,text+'\n\nГолос недоступен, отвечаю текстом: '+failure))
            tg.remember(ctx.app,chat,mid,ctx.session_id,text,'out')
            ctx.app.mind._save_event(ctx.session_id,'channel_sent','[Голосовой ответ'+(' заменён текстом: '+failure if failure else '')+'] '+text)
        elif result:
            import secrets
            from ..channels import media
            from ..core.fsutil import atomic_write
            from ..core.pathutil import within
            path=ctx.app.guard.policy.resolve('outbox/voice-'+secrets.token_hex(8)+'.wav')
            if not within(path,ctx.app.dirs.work):raise HumanError('voice_path','Папка outbox стала ссылкой наружу','Восстанови outbox внутри своей рабочей папки.')
            await ctx.check('fs.write',str(path),'сохранить голосовой ответ хозяину')
            atomic_write(path,result['audio'],durable=False)
            attachments=[media.register(ctx.app,path,name=path.name,kind='audio')]
            ctx.app.mind._save_event(ctx.session_id,'attachment',text,{'attachments':attachments})
        else:ctx.app.mind._save_event(ctx.session_id,'channel_sent',text+'\nГолос недоступен: '+failure)
        ctx.turn.setdefault('channel_sent',[]).append(text);ctx.turn['sent_all']=complete
        return ToolResult('Отправлено '+('текстом: '+failure if failure else 'голосом')+'.',summary='голосовой ответ хозяину',data={'message_id':mid,'attachments':attachments,'fallback':bool(failure)})
