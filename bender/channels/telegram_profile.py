"""Публичное лицо бота: данные из IDENTITY, применение только решением хозяина.

Методы/лимиты и InputProfilePhotoStatic: telegram-bot-api-10.3.md, разделы setMy*.
SVG в JPG ядро не рендерит: готовый JPG берём от хозяина или из своего work.
"""
from __future__ import annotations
import hashlib
from ..core.errors import HumanError
from ..core.pathutil import within

LIMITS={'name':64,'description':512,'short_description':120}
METHODS={'name':'setMyName','description':'setMyDescription','short_description':'setMyShortDescription'}


class Profile:
    def __init__(self,telegram):self.tg=telegram;self.host=telegram.app

    def defaults(self):
        identity=self.host.selfdir.identity()
        description=' · '.join(str(identity.get(k) or '').strip() for k in ('creature','vibe') if identity.get(k))
        return {'name':str(identity.get('name') or 'Бендер')[:64], 'description':description[:512],
                'short_description':str(identity.get('vibe') or identity.get('creature') or 'Бендер — личный агент.')[:120]}

    def plan(self,values=None):
        values=self.defaults() if values is None else values
        if not isinstance(values,dict) or any(key not in LIMITS for key in values):
            raise HumanError('telegram_profile','Неизвестное поле публичного профиля','Имя, описание и короткое описание.')
        for key,value in values.items():
            if not isinstance(value,str) or len(value)>LIMITS[key]:
                raise HumanError('telegram_profile','Слишком длинное или неверное поле '+key,f'До {LIMITS[key]} символов.')
        return dict(values)

    async def avatar(self,ctx,path):
        if not path:return None
        resolved=ctx.app.guard.policy.resolve(path)
        if not within(resolved,ctx.app.dirs.work):
            raise HumanError('telegram_avatar','Нужен JPG из своей рабочей папки','Пришли фото хозяином с подписью /avatar или сохрани JPG в work.')
        await ctx.check('fs.read',str(resolved),'прочитать предложенный JPG для профиля бота')
        if not resolved.is_file():raise HumanError('telegram_avatar','Файл аватара не найден','Выбери готовый JPG.')
        with resolved.open('rb') as handle:raw=handle.read(10*1024*1024+1)
        if len(raw)>10*1024*1024:raise HumanError('telegram_avatar','JPG аватара больше 10 МБ','Уменьши его перед отправкой.')
        if not raw.startswith(b'\xff\xd8\xff'):
            raise HumanError('telegram_avatar','Профиль принимает готовый JPG','SVG/PNG ядро не конвертирует: пришли фотографию в Telegram или готовый JPG.')
        return {'name':resolved.name,'bytes':raw,'mime':'image/jpeg','sha256':hashlib.sha256(raw).hexdigest()}

    async def apply(self,ctx,values=None,avatar='',*,reviewed=False):
        if not self.tg.token() or not self.tg.settings().get('enabled'):raise HumanError('telegram_profile','Бот Telegram ещё не подключён или выключен','Добавь токен и включи канал в «Связи».')
        if ctx.actor!='owner' and ctx.app.agent_id!='main':
            raise HumanError('telegram_profile','Общий бот принадлежит главному агенту','Публичное лицо предлагает его агент; хозяин может управлять ботом из «Связи».')
        values=self.plan(values)
        picture=await self.avatar(ctx,avatar)
        detail={'profile':values,'languages':['','ru'],'avatar':{k:picture[k] for k in ('name','sha256')} if picture else None}
        # Хозяин уже видит конкретный состав в интерфейсе/команде; агент всегда получает карточку.
        if ctx.actor=='owner' and not reviewed:raise HumanError('telegram_profile_review','Сначала просмотри публичные изменения','Подтверди показанный профиль или пришли своё фото с /avatar.')
        titles={'name':'Имя','description':'Описание','short_description':'Короткое описание'}
        summary='Публичный профиль бота (общий и русский):\n'+'\n'.join(titles[key]+': '+value for key,value in values.items())
        if picture:summary+='\nJPG: '+picture['name']+' · SHA256 '+picture['sha256']
        await ctx.check('telegram.profile','telegram:profile',summary,detail=detail)
        done=[]
        try:
            for key,value in values.items():
                for language in ('','ru'):
                    await self.tg.api(METHODS[key],{key:value,'language_code':language})
                    done.append(key+':'+(language or 'default'))
            if picture:
                await self.tg.api('setMyProfilePhoto',{'photo':{'type':'static','photo':'attach://avatar'}},files={'avatar':picture})
                done.append('avatar')
        except HumanError as error:
            self.host.journal.write('channel','Telegram: профиль применён не полностью',actor=ctx.actor,data={'done':done,'error':error.code})
            raise HumanError('telegram_profile_partial','Telegram не завершил изменение профиля','Уже изменено: '+(', '.join(done) or 'ничего')+'. '+error.title,data={'done':done}) from None
        previous=self.host.store.meta_get('telegram:profile',{})
        state={**previous,'values':{**previous.get('values',{}),**values},'avatar':picture['sha256'] if picture else previous.get('avatar'),'done':done}
        if self.tg.me and 'name' in values:self.tg.me['first_name']=values['name']
        self.host.store.meta_set('telegram:profile',state)
        self.host.journal.write('channel','Публичный профиль Telegram обновлён',actor=ctx.actor,data={'fields':done})
        self.host.bus.publish({'type':'channel','channel':'telegram','agent_id':'*','state':self.tg.state})
        return state
