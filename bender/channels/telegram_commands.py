"""Меню и команды хозяина: без обращения к модели, если не запрошено явно."""
from __future__ import annotations
import asyncio
import hashlib
import json
import re
import time
from ..core.errors import HumanError
from ..tools.registry import ToolContext
from . import telegram_ops as ops

# Порядок меню — порядок подсказок Telegram: частое сверху, имя-префикс раньше длинного.
COMMANDS=(
 ('new','Новый разговор'),('chats','Выбрать разговор'),('agent','Кто отвечает: агенты стаи'),
 ('model','Модели: роли и выбор главной'),('effort','Усилия моделей этого агента'),
 ('stop','Прервать работу агента'),('members','Группа: кого бот видит (список людей)'),('compact','Сжать разговор (можно с указанием)'),
 ('status','Самочувствие'),('fuel','Топливо и квоты'),('tasks','Задачи и результаты'),
 ('memory','Своя память'),('remember','Запомнить факт: /remember текст'),('recall','Найти в памяти: /recall слова'),
 ('skills','Активные навыки'),('files','Последние вложения'),
 ('live','Живой ответ: full, text, off; message, draft'),('voice','Голос: on, off, reply'),('style','Манера речи агента'),
 ('heart','Сердце и расписание'),('sleep','Состояние сна'),
 ('settings','Настройки'),('backup','Бекапы: снимок, проверка, починка'),('update','Обновление: проверить и поставить'),
 ('doctor','Самопроверка'),('journal','Бортжурнал: последние записи'),('load','Нагрузка: кто что делает'),('report','Технический отчёт без секретов'),
 ('profile','Публичное лицо бота'),('avatar','JPG аватара бота'),('whoami','Кто я и с кем говорю'),
 ('estop','Стоп-кран: остановить всё'),('resume','Снять стоп-кран'),
 ('restart','Перезапустить Бендера'),('shutdown','Выключить Бендера'),('help','Команды по разделам'))
GROUP=('stop','estop','resume','status','agent','chats','members','help')
SCOPES=('default','all_private_chats','all_group_chats','all_chat_administrators')
# models и agents — прежние имена: теперь это /model и /agent.
ALIASES={'start':'help','стоп':'stop','продолжить':'resume','новая':'new','models':'model','agents':'agent'}
# Кнопки этих выборов срабатывают один раз; остальные можно нажимать снова, срока у них нет.
ONE_SHOT=('control','tidy','report','profile','topic_route','update')


def skills(ctx):
    return [(row['id'],'Навык: '+row['id']) for row in ctx.skills.catalog() if row.get('enabled') and ctx.skills.command('/'+row['id'])]


def menu(ctx,group=False):
    entries=[(name,title) for name,title in COMMANDS if not group or name in GROUP]
    if not group:
        used={name for name,_ in entries}|set(ALIASES)
        entries += [(name,title) for name,title in skills(ctx) if name not in used and re.fullmatch(r'[a-z0-9_]{1,32}',name)]
    return [{'command':name,'description':title} for name,title in entries[:100]]


def help_text(ctx):
    return ('Я — Бендер. Пиши как обычно. Команды хозяина:\n\n'
      'Разговор\n/new — новый чат; /chats — выбрать; /agent — стая и кто отвечает; /model — модели по ролям и выбор главной (по провайдерам; /model часть названия — поиск); /effort — усилия моделей; /stop — прервать работу агента (писать дальше можно сразу); /compact — сжать сейчас, после команды можно написать, как пересказать.\n\n'
      'Состояние\n/status; /fuel; /tasks.\n\n'
      'Память\n/memory; /remember Купил Pi 5; /recall Raspberry; /skills; /files.\n\n'
      'Канал\n/live full|text|off; /voice on|off|reply (reply — отвечать голосом); /style — манера речи (/style off — свой голос).\n\n'
      'Фон\n/heart; /sleep; /sleep tidy — уборка памяти с подтверждением.\n\n'
      'Обслуживание\n/settings; /backup — снимок, проверка, починка; /update — проверить и поставить (в личке, можно без бекапа один раз); /doctor — самопроверка; /journal — бортжурнал (/journal 20 или /journal alarm); /load — нагрузка и кто что делает; /report; /profile; /avatar; /whoami.\n\n'
      'Управление\n/estop — стоп-кран: всё остановить и не давать действовать до /resume; /restart; /shutdown (с подтверждением); /help.\n\n'
      'Навыки — слэш-команды\n'+ ('\n'.join('/'+name for name,_ in skills(ctx)) or 'Пока нет активных команд навыков.')+
      '\n/научись текст — сохранить навык; /убрать-навык имя — убрать из активных.')


class Commands:
    def __init__(self,telegram):
        self.tg=telegram;self.host=telegram.app;self.lock=asyncio.Lock();self.task=None;self.pending=False
        saved=self.host.store.meta_get('telegram:picks',{})
        # Выборы переживают перезапуск: кнопка под старым сообщением работает, пока не вытеснена новыми.
        self.picks=saved if isinstance(saved,dict) else {}

    def remember_picks(self):
        self.picks=dict(list(self.picks.items())[-30:])
        self.host.store.meta_set('telegram:picks',self.picks)

    def languages(self):
        from .telegram_languages import LANGUAGES
        configured=self.host.settings.get('telegram','command_languages',[])
        if not isinstance(configured,list) or any(not isinstance(code,str) or not re.fullmatch(r'[a-z]{2}',code) for code in configured):
            raise HumanError('telegram_languages','Для языков нужен список двухбуквенных кодов','Например ["ru", "en"], пустой список — все ISO 639-1.')
        known=self.host.store.meta_get('telegram:command_languages',[])
        return sorted({'','ru','en',*(configured or LANGUAGES),*known})

    def fingerprint(self):
        content=[menu(self.host),menu(self.host,True),self.languages(),self.tg.settings().get('api_base'),self.tg.token(),1]
        return hashlib.sha256(json.dumps(content,ensure_ascii=False,sort_keys=True).encode()).hexdigest()

    def start(self):
        if getattr(self.host,'_closed',False):return
        if not self.host.settings.get('telegram','commands',True):
            self.pending=False
            if self.task and not self.task.done():self.task.cancel()
            return
        if self.task and not self.task.done():self.pending=True;return
        self.pending=False
        self.task=self.tg._bg_task(self.sync())
        def finished(_):
            if self.pending and not self.tg._stopping:self.start()
        self.task.add_done_callback(finished)

    async def sync(self):
        from .telegram import TelegramError
        async with self.lock:
            if not self.host.settings.get('telegram','commands',True) or self.tg._stopping:return False
            token=self.fingerprint()
            if self.host.store.meta_get('telegram:commands_hash')==token:return False
            await ToolContext(self.host,actor='owner').check('settings','telegram:commands','обновить команды подключённого хозяином бота')
            semaphore=asyncio.Semaphore(4);found={'','ru','en'}
            async def inspect(scope,language):
                async with semaphore:
                    if self.tg._stopping or not self.host.settings.get('telegram','commands',True):return
                    try:items=await self.tg.api('getMyCommands',{'scope':{'type':scope},'language_code':language})
                    except TelegramError as error:
                        if error.data.get('code')==400 and language:return
                        raise
                    if items:found.add(language)
            # В API нет перечисления языков. Один обход ISO, затем хеш не даёт повторять его при старте.
            probes=[asyncio.create_task(inspect(scope,language)) for scope in SCOPES for language in self.languages()]
            try:await asyncio.gather(*probes)
            finally:
                for probe in probes:
                    if not probe.done():probe.cancel()
                await asyncio.gather(*probes,return_exceptions=True)
            if self.tg._stopping or not self.host.settings.get('telegram','commands',True):return False
            for scope in SCOPES:
                for language in sorted(found):
                    if self.tg._stopping or not self.host.settings.get('telegram','commands',True):return False
                    await self.tg.api('deleteMyCommands',{'scope':{'type':scope},'language_code':language})
            for scope,short in (('default',False),('all_group_chats',True)):
                for language in ('','ru'):
                    if self.tg._stopping or not self.host.settings.get('telegram','commands',True):return False
                    await self.tg.api('setMyCommands',{'scope':{'type':scope},'language_code':language,'commands':menu(self.host,short)})
            self.host.store.meta_set('telegram:command_languages',sorted(found-{''}))
            if self.fingerprint()==token:self.host.store.meta_set('telegram:commands_hash',token)
            else:self.pending=True
            self.host.journal.write('channel','Команды бота обновлены',actor='system',data={'languages':len(found),'commands':len(menu(self.host))})
            self.host.bus.publish({'type':'channel','channel':'telegram','agent_id':'*','state':self.tg.state})
            return True

    async def setting(self,ctx,sid,group,values):
        await ToolContext(ctx,actor='owner',session_id=sid).check('settings',group,'изменить настройку по команде хозяина')
        return ctx.settings.set(group,values,by='owner')

    PAGE=8

    def page_buttons(self,token,options,page):
        """Кнопки одной страницы выбора и строка листания ◀ n/m ▶ (номера кнопок — сквозные)."""
        pages=max(1,-(-len(options)//self.PAGE));page=max(0,min(page,pages-1));start=page*self.PAGE
        buttons=[[{'text':label[:60],'callback_data':f'cmd:{token}:{n}'}] for n,(label,_) in enumerate(options[start:start+self.PAGE],start)]
        if pages>1:
            buttons.append([{'text':'◀','callback_data':f'cmd:{token}:p{(page-1)%pages}'},
                            {'text':f'{page+1} / {pages}','callback_data':f'cmd:{token}:p{page}'},
                            {'text':'▶','callback_data':f'cmd:{token}:p{(page+1)%pages}'}])
        return buttons

    async def pick(self,ctx,chat,sid,kind,title,options):
        import secrets
        token=secrets.token_hex(6);options=options[:2000]
        self.picks[token]={'agent':ctx.agent_id,'session':sid,'chat':int(chat),'thread':getattr(chat,'thread',None),'kind':kind,'options':options,'at':time.time()}
        self.remember_picks()
        if len(options)>self.PAGE:title+=f' · {len(options)} шт.'+(' Поиск: /model часть названия.' if kind in ('model','model_provider') else '')
        await self.tg.send(chat,title,buttons=self.page_buttons(token,options,0) or None)

    def models(self,ctx,only=None):
        choices=[];seen=set()
        for pid,provider in ctx.models.providers().items():
            if not provider.get('enabled',True) or (only is not None and pid!=only):continue
            entries=[m.get('id') if isinstance(m,dict) else m for m in provider.get('models',[])]
            entries += [m['model'] for chain in ctx.models.roles().values() for m in chain if m.get('provider')==pid]
            for model in entries:
                if not model or (pid,model) in seen:continue
                seen.add((pid,model));choices.append((provider.get('name',pid)+' / '+model,{'provider':pid,'model':model}))
        return choices

    def roles_text(self,ctx):
        from ..router.config import ROLES
        names={pid:p.get('name',pid) for pid,p in ctx.models.providers().items()}
        return 'Модели по ролям:\n'+'\n'.join(ROLES.get(role,(role,''))[0]+': '+(' → '.join(names.get(m['provider'],m['provider'])+' / '+m['model'] for m in chain) or 'не выбрана') for role,chain in ctx.models.roles().items())

    async def pick_models(self,ctx,chat,sid,provider=None):
        """Главная модель: сначала провайдер, потом его модели; один провайдер — сразу модели."""
        choices=self.models(ctx)
        if not choices:raise HumanError('models','Подключённых моделей пока нет','Добавь провайдера в пульте → Модели.')
        current=(ctx.models.roles().get('main') or [{}])[0]
        groups={}
        for label,value in choices:groups.setdefault(value['provider'],[]).append((label,value))
        names={pid:p.get('name',pid) for pid,p in ctx.models.providers().items()}
        mark=lambda value:'✓ ' if value.get('provider')==current.get('provider') and value.get('model')==current.get('model') else ''
        if provider is None and len(groups)>1:
            options=[(('✓ ' if pid==current.get('provider') else '')+names.get(pid,pid)+f' · {len(items)}',pid) for pid,items in groups.items()]
            await self.pick(ctx,chat,sid,'model_provider',self.roles_text(ctx)+'\n\nГлавная модель — выбери провайдера:',options);return
        pid=provider if provider in groups else next(iter(groups))
        options=[(mark(value)+value['model'],value) for _,value in groups[pid]]
        if len(groups)>1:options.append(('◀ Другие провайдеры','__providers__'))
        title=(self.roles_text(ctx)+'\n\n' if len(groups)==1 else '')+'Главная модель · '+names.get(pid,pid)
        await self.pick(ctx,chat,sid,'model',title,options)

    async def dispatch(self,ctx,chat,sid,command,arg=''):
        from .telegram import TelegramError
        command=ALIASES.get(command,command)
        names={name for name,_ in COMMANDS}
        if command not in names:
            if command in ('научись','убрать-навык') or ctx.skills.command('/'+command+(' '+arg if arg else '')):return False
            await self.tg.send(chat,'Не знаю такой команды. /help — команды и активные навыки.')
            return True
        try:await self.execute(ctx,chat,sid,command,arg)
        except (HumanError,TelegramError) as error:await self.tg.send(chat,error.title+('. '+error.hint if error.hint else ''))
        except Exception as error:
            from ..core.errors import unexpected
            problem=unexpected(error,'команда Telegram')
            self.host.journal.write('channel',problem.title,data={'code':problem.code})
            await self.tg.send(chat,problem.title+'. '+problem.hint)
        return True

    async def execute(self,ctx,chat,sid,command,arg):
        from ..core.dates import calendar
        from ..core.settings import SCHEMA
        from ..agents.rights import PROFILES
        from . import media
        from .telegram_routes import bind
        bind(self.tg,chat,ctx,sid)
        owner=ToolContext(ctx,actor='owner',session_id=sid)
        if command=='help':text=help_text(ctx)
        elif command=='stop':
            stopped=ctx.mind.stop()
            tasks=[t['id'] for t in ctx.team.all(limit=1000) if t['status'] in ('queued','running')]
            for tid in tasks:ctx.team.cancel(tid)
            text=('⏹ Остановил: ходов — '+str(stopped)+(', задач команды — '+str(len(tasks)) if tasks else '')+'. Можно писать дальше.') if stopped or tasks else 'Сейчас ничего не идёт.'
        elif command=='estop':
            self.host.halt();text='🛑 Стоп-кран: всё остановлено, агенты не действуют. /resume — снять.'
        elif command=='resume':
            for agent in self.host.agents.loaded.values():agent.guard.resume()
            text='▶ Стоп снят.'
        elif command in ('restart','shutdown'):
            private=self.tg.owner() or {}
            if int(chat)!=(private.get('chat_id') or private.get('user_id')):
                raise HumanError('control_private','Управляй запуском в личке с ботом','В группе перезапуск и выключение недоступны.')
            title=('Перезапустить' if command=='restart' else 'Выключить')+' Бендера? Действие относится ко всей стае.'
            choices=[('Дождаться ходов и '+('перезапустить' if command=='restart' else 'выключить'),{'action':command,'policy':'wait'}),
                     ('Прервать ходы и '+('перезапустить' if command=='restart' else 'выключить'),{'action':command,'policy':'interrupt'}),('Отмена',{'cancel':True})]
            await self.pick(ctx,chat,sid,'control',title,choices);return
        elif command=='status':text=ctx.body.describe()
        elif command=='fuel':text=self.host.fuel.brief() or 'Квоты ещё не измерены. Топливо → в пульте; это не полный бак.'
        elif command=='whoami':
            name=ctx.selfdir.identity().get('name') or ctx.agent_id
            text=f"Я — {name} · {ctx.agent_id}.\nРазговор: {(ctx.store.session(sid) or {}).get('title',sid)}\nПрофиль: {PROFILES[self.host.agents.rights.public(ctx)['effective']['profile']]}.\nПишет привязанный хозяин."
        elif command=='agent':
            if arg:
                other=self.host.agents.get(self.host.agents.resolve(arg))
                await owner.check('settings','telegram:selection','выбрать агента в Telegram')
                self.tg.choose(int(chat),other)
                if self.tg.topics.enabled(chat):chat=self.tg.topics.address(int(chat))
                text='Теперь отвечает '+(other.selfdir.identity().get('name') or other.agent_id)+'. /chats — его разговоры.'
            else:
                flock=self.host.agents.public()
                archived=[a['name'] for a in flock if a['archived']]
                title='Кому пишем? Сейчас отвечает '+(ctx.selfdir.identity().get('name') or ctx.agent_id)+'.'+('\nВ архиве: '+', '.join(archived) if archived else '')
                await self.pick(ctx,chat,sid,'agent',title,[(('✓ ' if a['id']==ctx.agent_id else '')+a['name'],a['id']) for a in flock if not a['archived']]);return
        elif command=='compact':
            from ..mind.compact import manual
            await manual(ctx,sid,arg)
            text='Начало разговора пересказано'+(' по твоему указанию (оно сохранится и для следующих сжатий)' if arg else '')+'; полная история сохранена. Указание можно дописать после /compact любыми словами.'
        elif command=='chats':
            choices=[('Новый чат','__new__')]+[(s['title'],s['id']) for s in ctx.store.sessions() if s['kind'] in ('main','chat','imported') and not s['meta'].get('archived')]
            if any(s['kind']=='correspondence' for s in ctx.store.sessions(limit=300)):choices.insert(1,('Переписка (только чтение)','__correspondence__'))
            await self.pick(ctx,chat,sid,'chat','Разговоры этого агента',choices);return
        elif command=='new':
            await owner.check('settings','telegram:selection','создать новый разговор хозяина')
            fresh=ctx.store.new_session('chat',arg or 'Новый разговор')
            ctx.store.update_session(fresh,meta={'home_channel':'telegram'})
            if self.tg.topics.enabled(chat):chat=await self.tg.topics.create(ctx,chat,fresh)
            else:self.tg.choose(chat,ctx,fresh)
            text='📄 Новый разговор создан. Память агента при нём. /chats — выбрать прежний.'
        elif command=='model':
            choices=self.models(ctx)
            if not choices:raise HumanError('models','Подключённых моделей пока нет','Добавь провайдера в пульте → Модели.')
            if arg:
                match=next((value for _,value in choices if arg in (value['model'],value['provider']+'/'+value['model'])),None)
                if not match:
                    found=[c for c in choices if arg.lower() in c[0].lower()]
                    if not found:raise HumanError('model','Такой модели нет среди подключённых','Вызови /model без аргумента и листай кнопками или напиши часть названия.')
                    await self.pick(ctx,chat,sid,'model',f'Главная модель · найдено по «{arg[:40]}»',found);return
                await owner.check('settings','roles','выбрать главную модель')
                ctx.models.set_role('main',[match]);text='Главная модель: '+match['model']
            else:await self.pick_models(ctx,chat,sid);return
        elif command=='effort':
            from ..router.effort import command as effort_command
            text=await effort_command(ctx,sid,arg)
        elif command=='memory':
            await owner.check('fs.read',str(ctx.dirs.self_dir/'MEMORY.md'),'показать свою память хозяину')
            text=ctx.selfdir.read('MEMORY.md') or 'Ядро памяти пока пусто. /remember текст — запомнить.'
        elif command=='remember':
            if not arg:raise HumanError('remember','Что запомнить?','/remember Купил Raspberry Pi 5')
            result=await ctx.tools.tools['memory'].handler(owner,action='add',file='memory',text=arg)
            text=result.content
        elif command=='recall':
            if not arg:raise HumanError('recall','Что найти?','/recall Raspberry')
            await owner.check('fs.read',str(ctx.dirs.diary),'найти в памяти по команде хозяина')
            from ..memory.index import format_hits
            await asyncio.to_thread(ctx.index.sync)
            hits=await asyncio.to_thread(ctx.index.search,arg,limit=6)
            text=format_hits(hits) if hits else 'Ничего не нашлось. Попробуй другие слова.'
        elif command=='tasks':
            rows=ctx.team.all(limit=12)
            text='Задачи:\n'+('\n'.join(f"{row['id']} · { {'queued':'в очереди','running':'в работе','done':'готово','error':'ошибка','cancelled':'отменено'}.get(row['status'],row['status'])} · {row.get('goal','')}" for row in rows) or 'Пока нет задач.')
        elif command=='skills':text='Активные навыки:\n'+('\n'.join('/'+name for name,_ in skills(ctx)) or 'Пока нет активных навыков.')
        elif command=='sleep' and arg.strip().lower() in ('tidy','уборка'):
            p=ctx.sleep.tidy_pending()
            if not p:
                await ctx.sleep.propose_tidy(reason='хозяин попросил /sleep tidy')
                p=ctx.sleep.tidy_pending()
            if not p:text='Убирать нечего: в ядре памяти не нашлось лишнего.'
            else:
                rows=[]
                for it in p['items']:
                    rows.append(f"{it['k']}. {it['file']}: "+(('склеить: '+' + '.join(it['old'])+' → '+it['new']) if it['op']=='merge' else ('убрать: '+it['old'][0]+' — '+it['why'])))
                body='\n'.join(rows)
                if len(body)>3300:body=body[:3300].rsplit('\n',1)[0]+'\n… остальное — в пульте → Сны'
                await self.pick(ctx,chat,sid,'tidy','🧹 Уборка ядра памяти ('+p['reason']+'):\n'+body+'\n\nВыбрать по одной — пульт → Сны. Прежний вид сохранится в self/history/.',
                                [('✅ Применить всё',{'id':p['id'],'do':'apply'}),('❌ Отклонить',{'id':p['id'],'do':'reject'})]);return
        elif command in ('heart','sleep'):
            if arg in ('on','off'):await self.setting(ctx,sid,command,{'enabled':arg=='on'})
            elif arg:raise HumanError('schedule','Пока это состояние и выключатель','Используй /'+command+' on|off'+(' или /sleep tidy — уборка памяти' if command=='sleep' else '')+'; ручной запуск — на экране пульта.')
            enabled=ctx.settings.get(command,'enabled',True)
            if command=='heart':text='Сердце '+('включено' if enabled else 'выключено')+'.\n'+'\n'.join(job.name for job in ctx.heart.jobs.all())
            else:
                from ..sleep import TRIED
                text='Сон '+('включён' if enabled else 'выключен')+('. Выполняется.' if ctx.sleep.running else '.')+' Последний: '+(calendar(ctx.sleep.last_sleep()) if ctx.sleep.last_sleep() else 'ещё не было')
                tried=ctx.store.meta_get('sleep:last_try') or {}
                if isinstance(tried,dict) and tried.get('at',0)>ctx.sleep.last_sleep()+60 and tried.get('status') in TRIED:text+='\nПоследняя попытка '+calendar(tried['at'])+': '+TRIED[tried['status']]+'.'
                if enabled and not ctx.heart.enabled():text+='\n⚠️ Сердце выключено — без него сон не наступает. /heart on'
        elif command=='live':
            if arg:
                if arg not in ('full','text','off','message','draft'):raise HumanError('live','Режим: full, text, off; показ: message или draft','Например /live text или /live message')
                await self.setting(ctx,sid,'telegram',{'live_via' if arg in ('message','draft') else 'live':arg})
            via=ctx.settings.get('telegram','live_via','message')
            text=('Живой ответ: '+ctx.settings.get('telegram','live','full')+', показ: '+via+'. Мысли — только в живом ответе. '
                  +('Писать агенту во время работы можно.' if via=='message' else 'Пока висит черновик, Telegram не даёт писать; /live message — статус обычным сообщением.'))
        elif command=='style':
            from ..mind import manners
            if arg:
                item=None if arg.strip().lower() in ('off','выкл','свой') else manners.resolve(ctx.settings,arg)
                await owner.check('manner',item['id'] if item else 'off','выбрать манеру речи по команде хозяина')
                manners.choose(ctx,item['id'] if item else '',actor='owner')
                text='🎭 Манера: '+(f"«{item['name']}»" if item else 'свой голос')+'.'
            else:
                cur=manners.active(ctx)
                choices=[(('✓ ' if not cur else '')+'Свой голос','')]+[(('✓ ' if cur and cur['id']==m['id'] else '')+m['name'],m['id']) for m in manners.library(ctx.settings)]
                await self.pick(ctx,chat,sid,'manner','Как говорить агенту? Сейчас: '+(f"«{cur['name']}»" if cur else 'свой голос')+'. Тексты манер — в пульте → Я.',choices);return
        elif command=='voice':
            if arg:
                if arg not in ('on','off','reply'):raise HumanError('voice','Нужно on, off или reply','/voice on — включить; /voice off — выключить; /voice reply — отвечать голосом.')
                await self.setting(ctx,sid,'voice',{'enabled':arg!='off',**({'telegram_reply':True} if arg=='reply' else {})})
            text='Голос '+('включён' if ctx.settings.get('voice','enabled',False) else 'выключен')+'. Ответ голосом '+('включён' if ctx.settings.get('voice','telegram_reply',False) else 'выключен')+'. Уши/Голос подключаются в пульте → Модели; /voice on|off|reply.'
        elif command=='files':
            items=list(ctx.store.meta_get('attachments:files',{}).values())[-12:]
            await self.pick(ctx,chat,sid,'file','Последние файлы этого агента (кнопка — прислать)',[(item['name'],item['id']) for item in reversed(items)]);return
        elif command=='backup':
            action={'сейчас':'now','проверить':'verify','починить':'repair'}.get(arg.strip().lower(),arg.strip().lower())
            if action in ('now','verify','repair'):text=await self.maintenance(action)
            elif not action:
                await self.pick(ctx,chat,sid,'backup',ops.backup_text(self.host),[('💾 Сохранить сейчас','now'),('🔎 Проверить','verify'),('🛠 Починить','repair')]);return
            else:raise HumanError('backup','Что сделать с бекапом?','/backup now — снимок; /backup verify — проверка; /backup repair — починка.')
        elif command=='update':
            try:result=await self.host.updater.check()
            except HumanError as problem:
                if problem.code!='git_diverged':raise
                private=self.tg.owner() or {}
                if int(chat)!=(private.get('chat_id') or private.get('user_id')):raise
                await self.pick(ctx,chat,sid,'update',problem.title+'. '+problem.hint,[('🔧 Выровнять с сервером',{'realign':True}),('Отмена',{'cancel':True})]);return
            if not result.get('available'):text='Установлена последняя версия.'+(' Локальная ветка впереди сервера.' if result.get('ahead') else '')
            else:
                commits=result.get('commits',[])
                title=f'Доступно обновление ({len(commits)}):\n'+'\n'.join(commits[:15])+(f'\n… и ещё {len(commits)-15}' if len(commits)>15 else '')
                private=self.tg.owner() or {}
                if int(chat)!=(private.get('chat_id') or private.get('user_id')):text=title+'\nСтавить — в личке с ботом или в пульте → Версии и откат.'
                else:
                    await self.pick(ctx,chat,sid,'update',title+'\n\nУстановка: бекап, самопроверка, откат при неудаче, затем перезапуск.',
                                    [('⬆️ Обновить',{'skip':False}),('⬆️ Обновить без бекапа (один раз)',{'skip':True}),('Отмена',{'cancel':True})]);return
        elif command=='members':
            if int(chat)>0:raise HumanError('members','Список людей — у группы','Напиши /members в самой группе: /members add 12345678, /members remove 12345678, /members off.')
            row=self.tg.groups.policy(int(chat));current=list(row['members']);words=arg.split()
            if words[:1]==['import']:
                found=[i for i in (self.host.store.meta_get('telegram:group_allow_import') or []) if isinstance(i,int)]
                if not found:raise HumanError('members','Нечего импортировать','Список людей для групп переносится из OpenClaw вместе с переездом (groupAllowFrom).')
                current=list(dict.fromkeys(current+found));await self.tg.groups.configure(int(chat),{'members':current})
            elif words[:1]==['wake'] and words[1:2] in (['on'],['off']):
                await self.tg.groups.configure(int(chat),{'wake':words[1]=='on'});row=self.tg.groups.policy(int(chat))
                if row['wake'] and not current:raise HumanError('members','Включено, но список пуст','Сначала /members add НОМЕР: будить бота смогут только люди из списка.')
            elif words[:1] and words[0] in ('add','remove','off','добавить','убрать','выкл'):
                action={'добавить':'add','убрать':'remove','выкл':'off'}.get(words[0],words[0])
                if action=='off':current=[]
                else:
                    if len(words)<2 or not words[1].lstrip('-').isdigit():raise HumanError('members','Нужен номер человека в Telegram','/members add 12345678 — номер можно узнать у @userinfobot или в списке «кто писал» в пульте.')
                    number=int(words[1])
                    current=[i for i in current if i!=number]+([number] if action=='add' else [])
                await self.tg.groups.configure(int(chat),{'members':current})
            text=('Бот видит в группе только этих людей: '+', '.join(map(str,current))+'. Остальных не запоминает и в контекст не берёт.'+(' Люди из списка будят бота упоминанием или ответом (превью: ход недоверенный, без действий, ответ текстом). /members wake off — выключить.' if row['wake'] else ' /members wake on — пусть они будят бота (превью).')) if current else 'Список людей пуст: правила прежние (контекст участников — по выключателю группы). /members add НОМЕР — ограничить видимость.'
        elif command=='doctor':text=await asyncio.to_thread(ops.doctor_text,self.host)
        elif command=='journal':text=ops.journal_text(ctx,arg)
        elif command=='load':text=ops.load_text(self.host)
        elif command=='report':
            from ..diagnostics.report import preview
            text='Состав технического отчёта без личных данных:\n'+json.dumps(preview(),ensure_ascii=False,indent=2)[:3000]
            await self.pick(ctx,chat,sid,'report',text,[('Создать и прислать отчёт',True)]);return
        elif command=='profile':
            values=self.tg.profile.defaults()
            await self.pick(ctx,chat,sid,'profile','Публичное лицо общего бота (главный агент):\n'+json.dumps(values,ensure_ascii=False,indent=2),[('Применить публичный профиль',values)]);return
        elif command=='avatar':
            text='Пришли фотографию с подписью /avatar. Это явное подтверждение хозяина для общего бота. Готовый JPG из work можно предложить тулом telegram_profile или выбрать в «Связи».'
        elif command=='settings':
            if arg and arg not in SCHEMA:raise HumanError('settings','Неизвестная группа настроек','Вызови /settings для списка групп.')
            if arg:
                group=next(group for group in ctx.settings.describe() if group['name']==arg)
                text=group['title']+'\n'+'\n'.join(field['title']+': '+('скрыто' if SCHEMA[arg].field(field['key']).secret else json.dumps(field['value'],ensure_ascii=False)) for field in group['fields'])
            else:text='Настройки в пульте; /settings имя_группы — посмотреть.\n'+'\n'.join(name+' · '+group.title for name,group in SCHEMA.items())
        else:raise HumanError('command','Команда пока недоступна','Открой /help.')
        await self.tg.send(chat,text)

    async def maintenance(self,action):
        backups=self.host.backups
        result=await backups.now() if action=='now' else await backups.verify(full=False) if action=='verify' else await backups.repair()
        return ops.backup_result(action,result)

    async def install_update(self,chat,skip,realign=False):
        """Установка из Telegram — как кнопка пульта: ходы закончены, бекап (или его пропуск), самопроверка, перезапуск."""
        from ..updates import request
        if request.busy(self.host):raise HumanError('update_busy','Сначала дождись завершения ходов','Обновление перезапустит программу: пришли /stop или подожди.')
        if realign:
            result=await self.host.updater.realign()
            if result['realigned']:
                await self.tg.send(chat,result['message']+' Перезапускаюсь, вернусь через минуту.')
                return await self.host.updater.restart()
            return result['message']
        plan=await self.host.updater.check()
        if not plan.get('available'):return 'Уже установлена последняя версия.'
        result=await self.host.updater.install(plan,skip_backup=bool(skip))
        self.host.store.meta_set('updates:available',None)
        backup=result.get('backup') or {}
        text=result.get('message','Готово.')+(' Без бекапа (по твоему слову).' if backup.get('skipped') else (' Бекап: '+str(backup.get('path',''))+'.' if backup.get('path') else ''))+(' '+backup['note'] if backup.get('note') else '')
        if result.get('updated'):
            await self.tg.send(chat,text+' Перезапускаюсь, вернусь через минуту.')
            return await self.host.updater.restart()
        return text

    async def callback(self,cq):
        from .telegram import TelegramError
        data=str(cq.get('data') or '')
        if not data.startswith('cmd:'):return False
        message=cq.get('message') or {};chat=(message.get('chat') or {}).get('id')
        try:
            _,token,index=data.split(':',2)
            state=self.picks.get(token)
            if state and index[:1]=='p' and index[1:].isdigit() and state['chat']==chat:
                await self.tg.api('editMessageReplyMarkup',{'chat_id':chat,'message_id':message.get('message_id'),
                                  'reply_markup':{'inline_keyboard':self.page_buttons(token,state['options'],int(index[1:]))}})
                await self.tg.api('answerCallbackQuery',{'callback_query_id':cq.get('id')})
                return True
            if not state or not index.isdigit() or int(index)>=len(state['options']):
                raise HumanError('command_button','Этот выбор уже сделан или вытеснен новыми','Вызови команду ещё раз.')
            if state['chat']!=chat or state.get('thread')!=getattr(chat,'thread',None):
                raise HumanError('command_button','Эта кнопка относится к другому разговору','Вызови команду там, где нужно выбрать.')
            ctx=self.host.agents.get(state['agent']);sid=state['session'];kind=state['kind']
            label,value=state['options'][int(index)]
            owner=ToolContext(ctx,actor='owner',session_id=sid)
            if kind=='chat' and value=='__correspondence__':
                choices=[(s['title'],s['id']) for s in ctx.store.sessions(limit=300) if s['kind']=='correspondence']
                await self.pick(ctx,chat,sid,'correspondence_view','Переписка · только чтение',choices)
                await self.tg.api('answerCallbackQuery',{'callback_query_id':cq.get('id'),'text':'Выбери нить переписки.'})
                return True
            elif kind=='correspondence_view':
                await self.tg.authorize_group(owner,chat,'показать хозяину нить переписки')
                text=self.host.agents.correspondence.preview(ctx,value)
            elif kind in ('agent','chat'):
                await owner.check('settings','telegram:selection','выбрать разговор в Telegram')
                other=self.host.agents.get(value) if kind=='agent' else ctx
                if kind=='chat' and value=='__new__':
                    await self.execute(ctx,chat,sid,'new','')
                    await self.tg.api('answerCallbackQuery',{'callback_query_id':cq.get('id'),'text':'Новый разговор создан.'})
                    return True
                if self.tg.topics.enabled(chat):
                    if kind=='agent' or value==other.main_session():
                        self.tg.choose(int(chat),other);chat=self.tg.topics.address(int(chat))
                    else:chat=await self.tg.topics.open(other,chat,value)
                else:self.tg.choose(chat,other,None if kind=='agent' else value)
                text='Выбран '+label.removeprefix('✓ ')+'.'
            elif kind=='topic_route':
                message={**value['message']}
                if value['thread'] is None:
                    message.pop('message_thread_id',None);message.pop('is_topic_message',None)
                else:message.update(message_thread_id=value['thread'],is_topic_message=True)
                self.picks.pop(token,None);self.remember_picks()
                await self.tg._handle({'message':message})
                text='Сообщение передано в выбранный разговор.'
            elif kind=='model_provider' or (kind=='model' and value=='__providers__'):
                await self.pick_models(ctx,chat,sid,None if kind=='model' else value)
                await self.tg.api('answerCallbackQuery',{'callback_query_id':cq.get('id')})
                return True
            elif kind=='model':
                await owner.check('settings','roles','выбрать главную модель')
                ctx.models.set_role('main',[value]);text='Главная модель: '+label.removeprefix('✓ ')
            elif kind=='tidy':
                if value['do']=='apply':
                    await owner.check('settings','memory','убрать записи из ядра памяти по предложению сна')
                    result=ctx.sleep.apply_tidy(value['id']);text=f"🧹 Ядро прибрано: {result['applied']} из {result['total']}. Прежний вид — в self/history/."
                else:ctx.sleep.reject_tidy();text='Уборка отклонена, ядро не тронуто.'
            elif kind=='manner':
                from ..mind import manners
                await owner.check('manner',value or 'off','выбрать манеру речи кнопкой хозяина')
                _,item=manners.choose(ctx,value,actor='owner')
                text='🎭 Манера: '+(f"«{item['name']}»" if item else 'свой голос')+'.'
            elif kind=='profile':
                await self.tg.profile.apply(owner,value,reviewed=True)
                text='Публичное лицо общего бота обновлено.'
            elif kind=='control':
                if value.get('cancel'):text='Отменено.'
                else:
                    private=self.tg.owner() or {}
                    if int(chat)!=(private.get('chat_id') or private.get('user_id')):
                        raise HumanError('control_private','Подтверждение доступно в личке хозяина','Вызови команду заново в личном чате.')
                    notice={'chat':int(chat),'thread':getattr(chat,'thread',None),'owner':private.get('user_id')}
                    result=await self.host.lifecycle.request(owner,value['action'],policy=value['policy'],notice=notice)
                    text=result['message']
            elif kind in ('backup','update'):
                if kind=='update' and value.get('cancel'):text='Отменено.'
                else:
                    if kind=='update':
                        private=self.tg.owner() or {}
                        if int(chat)!=(private.get('chat_id') or private.get('user_id')):
                            raise HumanError('control_private','Подтверждение доступно в личке хозяина','Вызови команду заново в личном чате с ботом.')
                    # работа долгая: на кнопку отвечаем сразу, итог приходит сообщением
                    await self.tg.api('answerCallbackQuery',{'callback_query_id':cq.get('id'),'text':'Работаю…'})
                    try:text=await self.maintenance(value) if kind=='backup' else await self.install_update(chat,value.get('skip'),bool(value.get('realign')))
                    except (HumanError,TelegramError) as error:text=error.title+('. '+error.hint if error.hint else '')
                    if kind in ONE_SHOT:
                        if self.picks.pop(token,None):self.remember_picks()
                    await self.tg.send(chat,text)
                    return True
            elif kind=='file':
                from . import media
                item=media.get(ctx,value)
                ctx.store.update_session(sid,meta={'home_channel':'telegram'})
                result=await ctx.tools.tools['send_file'].handler(owner,path=item['path'],kind='auto')
                text=result.content
            elif kind=='report':
                from ..diagnostics.report import build
                from ..core.fsutil import atomic_write
                from ..core.ids import new_id
                path=ctx.dirs.work/'outbox'/('bender-report-'+new_id()+'.zip')
                await owner.check('fs.write',str(path),'сохранить технический отчёт после просмотра состава хозяином')
                raw,_=await asyncio.to_thread(build,self.host.dirs.root)
                atomic_write(path,raw,durable=False)
                ctx.store.update_session(sid,meta={'home_channel':'telegram'})
                await ctx.tools.tools['send_file'].handler(owner,path=str(path),kind='document')
                text='Технический отчёт отправлен.'
            else:raise HumanError('command_button','Неизвестный выбор','Вызови команду заново.')
            if kind in ONE_SHOT and self.picks.pop(token,None):self.remember_picks()
        except (HumanError,TelegramError) as error:text=error.title+('. '+error.hint if error.hint else '')
        except ValueError:text='Кнопка устарела. Вызови команду заново.'
        await self.tg.api('answerCallbackQuery',{'callback_query_id':cq.get('id'),'text':text[:190]})
        await self.tg.send(chat,text)
        return True
