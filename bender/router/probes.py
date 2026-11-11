"""Маленькие проверки зрения/слуха только по явному нажатию хозяина, без его истории."""
import base64
import json
import re
import secrets
import struct
import zlib

from ..core.errors import HumanError
from .capabilities import rejected
from .errors import ProviderError

# Принял картинку, но говорит, что её нет: шлюз к текстовой модели молча выбросил вложение.
UNSEEN=re.compile(r"не\s+вижу|не\s+могу\s+(?:у|про|рас)?(?:видеть|смотреть|сматривать)|не\s+(?:был[аио]?\s+)?(?:приложен|прикреплен|передан|загружен|получ)\w*\s+(?:\w+\s+)?(?:картинк|изображен|фото|снимк|picture|image|photo)|нет\s+(?:никак\w*\s+)?(?:картинк|изображен|фото|снимк|picture|image|photo)|(?:картинк|изображен|фото|снимк|picture|image|photo)\w*\s+(?:нет|отсутств|не\s+(?:был[аио]?\s+)?(?:приложен|прикреплен|передан|загружен|получен))|\bno\s+(?:\w+\s+)?(?:картинк|изображен|фото|снимк|picture|image|photo)|\b(?:can'?t|cannot|can\s+not|unable\s+to|not\s+able\s+to|don'?t|do\s+not)\s+(?:see|view|process|access|read)\b|(?:картинк|изображен|фото|снимк|picture|image|photo)\w*\s+(?:is\s+|was\s+)?(?:not\s+(?:attached|provided|included|visible)|missing)")
UNSEEN_CLI='Claude Code принял картинку, но ответил, что не видит её. Зрение у него задаёт сам клиент: проверь, какая модель выбрана в Claude Code.'
UNSEEN_MESSAGE='Модель приняла картинку, но говорит, что не видит её. Поставь «Нет» — картинки будет описывать Зрение.'


def unseen(answer):
    return bool(UNSEEN.search(answer.lower().replace('’',"'").replace('ё','е')))


COLORS=(((230,20,20),'красный',('красн','red')),((20,40,230),'синий',('син','blue')),((20,190,40),'зелёный',('зел','green')))


def png(rgb):
    def chunk(name,data):return struct.pack('!I',len(data))+name+data+struct.pack('!I',zlib.crc32(name+data)&0xffffffff)
    raw=(b'\0'+bytes(rgb)*16)*16
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!2I5B',16,16,8,2,0,0,0))+chunk(b'IDAT',zlib.compress(raw))+chunk(b'IEND',b'')


def audio_available(agent_ctx):
    from ..voice.connect import folder
    try:return bool((folder(agent_ctx)/'installed.json').is_file() and agent_ctx.host.settings.get('voice_local','enabled',False))
    except HumanError:return False


async def run(ctx,provider_id,model,feature):
    if ctx.actor!='owner':raise HumanError('model_probe','Проверку запускает только хозяин','Кнопки — в карточке модели.')
    if feature not in ('vision','audio_input'):raise HumanError('model_probe','Неизвестная проверка','Выбери зрение или слух.')
    if not isinstance(provider_id,str) or not isinstance(model,str) or not model.strip() or len(model)>500:raise HumanError('model_probe','Не выбраны провайдер и модель для проверки','Укажи их в карточке роли.')
    agent_ctx=ctx.app;p=agent_ctx.models.providers().get(provider_id)
    if not p:raise HumanError('no_provider','Провайдер уже удалён','Открой «Модели» заново.')
    if p.get('kind')=='codex_cli' or p.get('kind')=='claude_code' and feature!='vision':
        raise HumanError('model_probe','CLI-движок не принимает это вложение','Claude Code проверяется только на зрение; для остального выбери API-модель.')
    if agent_ctx.body.economy() and not agent_ctx.router.is_free(p,model):raise HumanError('model_probe','Дневной бюджет исчерпан','Для платной проверки дождись сброса или измени бюджет.')
    from ..tools.registry import ToolContext
    session='model-probe-'+secrets.token_hex(12);target=p.get('base_url','') or p.get('kind','')
    from ..guard.policy import Action
    rule=agent_ctx.guard.policy._rule_for(Action('net.send',target,actor='owner',tool='model_probe'))
    if not (rule and rule.get('verdict')=='deny'):agent_ctx.guard.grant(session,[('net.send',target)])
    try:
        await ToolContext(agent_ctx,actor='owner',session_id=session,tool='model_probe').check('net.send',target,
            'проверить '+('зрение PNG 16×16' if feature=='vision' else 'слух коротким WAV')+' без истории разговоров')
    finally:agent_ctx.guard.revoke(session,[('net.send',target)])
    if feature=='vision':
        rgb,expected,markers=secrets.choice(COLORS)
        part={'type':'image','data':base64.b64encode(png(rgb)).decode(),'media_type':'image/png'}
        question='Назови одним словом по-русски цвет квадрата на картинке. Не угадывай по тексту запроса.'
    else:
        if p.get('kind','openai') not in ('openai','chatgpt'):raise HumanError('model_probe','В этом адаптере не подтверждён протокол нативного аудио','Уши работают отдельно; для пробы выбери OpenAI-совместимый API или ChatGPT.')
        if not audio_available(agent_ctx):raise HumanError('model_probe','Нет включённого локального Piper для проверочного WAV','Установи и запусти локальный голос; чужая запись для пробы не используется.')
        from ..voice.cli import request
        await ctx.check('audio.speak','local_voice','синтезировать одно проверочное слово локальным Piper',to_owner=True)
        expected=secrets.choice(('берёза','кошка'));markers=('берёз','берез') if expected=='берёза' else ('кошк',)
        audio=await request(agent_ctx,'/v1/audio/speech',json.dumps({'model':'piper-ru','input':expected,'response_format':'wav'},ensure_ascii=False).encode())
        import io,wave
        try:
            with wave.open(io.BytesIO(audio)) as sound:
                if not 0<sound.getnframes()/sound.getframerate()<=5 or len(audio)>2*1024*1024:raise ValueError('Слишком длинный пробный звук')
        except (wave.Error,ValueError,EOFError):raise HumanError('model_probe','Piper не вернул короткий WAV','Проверь локальную голосовую службу.') from None
        part={'type':'audio','format':'wav','data':base64.b64encode(audio).decode()}
        question='Напиши одним словом по-русски, какое слово произнесено в записи. Не угадывай по тексту запроса.'
    messages=[{'role':'user','content':[{'type':'text','text':question},part]}]
    from ..mind.passport import clean
    try:
        result=await agent_ctx.router.call(messages,target=(provider_id,model),params={'max_tokens':24},single_attempt=True)
    except ProviderError as error:
        raw=(error.raw or error.message).encode('utf-8','replace')[:2048].decode('utf-8','ignore')
        reason=clean(agent_ctx,raw,stream=True).encode('utf-8')[:2048].decode('utf-8','ignore')
        unsupported=feature in rejected(error,messages)
        if unsupported:agent_ctx.models.set_capability(provider_id,model,feature,False,source='probe',reason=reason)
        agent_ctx.store.add_call(session_id=None,role='probe',provider=error.provider,model=model,usage={},cost=0,seconds=0,notice='Проверка медиа',error=error.to_dict(),request={'probe':feature})
        return {'ok':False,'accepted':False,'value':False if unsupported else None,'reason':reason,'message':'Проверка не прошла. '+error.title}
    answer=clean(agent_ctx,result.text or '')[:1000];words=re.findall('[a-zа-яё]+',answer.lower())
    ok=bool(len(words)<=8 and not {'не','not','no'}&set(words) and any(word.startswith(marker) for word in words for marker in markers))
    # Назвала верный цвет — видит, даже если рядом «не вижу» («Красный, текста не вижу»)
    named=any(word.startswith(marker) for word in words for marker in markers)
    blind=not ok and not named and feature=='vision' and unseen(answer)
    # Принятый, но неверно распознанный стимул не доказывает отсутствие возможности: «нет» ставит хозяин кнопкой.
    if blind:agent_ctx.models.set_capability(provider_id,model,feature,None,source='probe_unseen',reason='Запрос принят, но модель ответила, что не видит картинку: '+answer)
    else:agent_ctx.models.set_capability(provider_id,model,feature,True if ok else None,source='probe',reason='' if ok else 'Запрос принят, ответ не совпал с проверочным: '+answer)
    agent_ctx.store.add_call(session_id=None,role='probe',provider=result.provider_name,model=result.model,usage=result.usage.to_dict(),cost=result.cost,seconds=result.seconds,
                            notice='Проверка '+('зрения' if feature=='vision' else 'слуха'),request={'probe':feature,'matched':ok})
    agent_ctx.journal.write('models','Проверка '+('зрения' if feature=='vision' else 'слуха')+': '+('ответ совпал' if ok else 'модель не видит картинку' if blind else 'ответ не совпал'),actor='owner',data={'provider':provider_id,'model':model,'ok':ok,'unseen':blind})
    if blind:return {'ok':False,'accepted':True,'unseen':True,'value':None,'source':'probe_unseen','answer':answer,'expected':expected,'message':UNSEEN_CLI if p.get('kind')=='claude_code' else UNSEEN_MESSAGE}
    return {'ok':ok,'accepted':True,'value':True if ok else None,'answer':answer,'expected':expected,'message':'Проверка пройдена.' if ok else 'Запрос принят, ответ не совпал. Возможность пока неизвестна.'}
