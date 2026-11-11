"""Одноразовая самопроверка: читает данные и работает даже при неудачном старте App."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import shutil
import socket
import sqlite3
import sys
import time
from types import SimpleNamespace
from urllib.parse import quote

from .. import __version__
from ..core.errors import HumanError
from ..core.pathutil import same
from ..core.settings import Settings
from ..core.secrets import Secrets
from ..core.watchdog import load_boot


class Reader:
    def __init__(self,path):
        self.db=sqlite3.connect('file:'+quote(str(Path(path).resolve()))+'?mode=ro',uri=True,timeout=1)
        self.db.row_factory=sqlite3.Row
    def q(self,sql,params=()):return self.db.execute(sql,params).fetchall()
    def one(self,sql,params=()):return self.db.execute(sql,params).fetchone()
    def close(self):self.db.close()


class Defaults:
    def get(self,group,key,default=None):return default
    def group(self,group):return {}


def process_metrics(root,*,live=False):
    candidates=[];proc=Path('/proc')
    if not proc.exists():return {'available':False}
    if live:candidates=[(os.getpid(),0)]
    else:
        for folder in proc.iterdir():
            if not folder.name.isdigit():continue
            try:
                args=(folder/'cmdline').read_bytes().decode().split('\0')
                if 'bender' not in args or '--data' not in args or any(x in args for x in ('doctor','report','try')):continue
                if not same(args[args.index('--data')+1],root):continue
                parent=int((folder/'stat').read_text().rsplit(')',1)[1].split()[1])
                candidates.append((int(folder.name),parent))
            except (OSError,ValueError,IndexError,UnicodeError,HumanError):continue
    parents={parent for _,parent in candidates}
    leaves=[pid for pid,_ in candidates if pid not in parents]
    if not leaves:return {'available':True,'running':False}
    pid=leaves[0];out={'available':True,'running':True,'pid':pid}
    try:
        for line in (proc/str(pid)/'status').read_text().splitlines():
            if line.startswith('VmRSS:'):out['rss_mb']=round(int(line.split()[1])/1024,2)
            if line.startswith('Threads:'):out['threads']=int(line.split()[1])
        out['fds']=len(list((proc/str(pid)/'fd').iterdir()))
        for line in (proc/str(pid)/'io').read_text().splitlines():
            key,_,value=line.partition(':')
            if key in ('wchar','write_bytes'):out[key]=int(value.strip())
    except (OSError,ValueError):out['available']=False
    return out


def collect(root,*,app=None):
    root=Path(root).expanduser().resolve();rows=[];reader=None;settings=Defaults()
    def add(key,level,title,action='',**details):rows.append({'id':key,'level':level,'title':title,'action':action,'details':details})
    add('runtime','ok' if sys.version_info>=(3,10) else 'error','Python и ОС','Обнови Python до 3.10 или новее.' if sys.version_info<(3,10) else '',python=sys.version.split()[0],os=sys.platform)
    add('data','ok' if root.is_dir() else 'warn','Папка данных','При первом запуске выбери эту папку через --data; не переноси файлы по одному.' if not root.is_dir() else '',path=str(root))
    dbpath=root/'bender.db'
    try:
        if not dbpath.exists():
            add('database','warn','База ещё не создана','Запусти Бендера для знакомства.')
        else:
            reader=Reader(dbpath)
            ok=reader.one('PRAGMA quick_check')[0]=='ok'
            add('database','ok' if ok else 'error','Целостность SQLite','Восстанови целую копию в отдельный слот.' if not ok else '',bytes=dbpath.stat().st_size,wal_bytes=Path(str(dbpath)+'-wal').stat().st_size if Path(str(dbpath)+'-wal').exists() else 0)
            try:
                settings=Settings(reader)
                add('settings','ok','Настройки читаются')
            except (HumanError,sqlite3.Error,ValueError):add('settings','error','Настройки повреждены','Восстанови настройки из бекапа; исходные данные не заменены.')
    except (OSError,sqlite3.Error):add('database','error','База не открывается','Сохрани повреждённый файл и восстанови бекап в отдельный слот.')
    try:
        if (root/'secrets.json').exists():
            Secrets(root/'secrets.json').names()
            private=os.name!='posix' or (root/'secrets.json').stat().st_mode&0o077==0
            add('secrets','ok' if private else 'error','Сейф читается, права частные' if private else 'Сейф доступен другим пользователям','На POSIX установи chmod 600 для secrets.json.' if not private else '')
        else:add('secrets','warn','Сейф ещё не создан','Ключи появятся после подключения провайдера или канала.')
    except (HumanError,OSError):add('secrets','error','Сейф повреждён или недоступен','Восстанови secrets.json из целой копии; не заменяй пустым файлом.')
    try:
        path=root/'self'/'IDENTITY.md'
        if not path.exists():add('identity','warn','Личность ещё не создана','Пройди знакомство при первом запуске.')
        elif not path.read_text(encoding='utf-8').strip():add('identity','error','IDENTITY.md пуст','Восстанови файл из бекапа или заполни его вручную.')
        else:add('identity','ok','Личность читается')
    except (OSError,UnicodeError):add('identity','error','Личность не читается','Проверь кодировку UTF-8 и права файла.')
    from ..guard import sandbox
    protection=sandbox.for_app(SimpleNamespace(settings=settings))
    add('sandbox','ok' if protection.get('kind')=='bwrap' else 'warn','Изоляция ОС','Без bubblewrap строгая Мастерская и её сеть закрыты.' if protection.get('kind')!='bwrap' else '',kind=protection.get('kind'),limits=bool(protection.get('limits')))
    port=getattr(app,'web_port',None) or settings.get('web','port',8770)
    try:
        with socket.socket() as probe:
            probe.settimeout(.2);busy=probe.connect_ex(('127.0.0.1',int(port)))==0
        add('port','ok' if app is not None or not busy else 'warn','Порт пульта','Если это другой процесс, выбери --port; запуск также проверяет соседние порты.' if busy and app is None else '',port=port,in_use=busy)
    except (OSError,ValueError,TypeError,OverflowError):add('port','error','Некорректный порт','Выбери порт от 1 до 65535 в настройках или --port.')
    from ..router.config import ROLES
    roles=settings.group('roles');providers=settings.group('providers')
    from ..router.roles import broken
    damaged=len(broken(roles,providers));affected=int(bool(damaged))
    if reader and not any(row['id']=='database' and row['level']=='error' for row in rows):
        from ..agents.store import valid_id
        if reader.one("SELECT 1 FROM sqlite_master WHERE name='flock_agents'"):
            for agent in reader.q("SELECT id FROM flock_agents WHERE id!='main' AND temporary=0"):
                try:
                    table='agent_'+valid_id(agent['id'])+'__settings'
                    if not reader.one("SELECT 1 FROM sqlite_master WHERE name=?",(table,)):continue
                    row=reader.one('SELECT value FROM "'+table+'" WHERE grp=?',('roles',))
                    local=json.loads(row['value']) if row else {}
                    if not isinstance(local,dict):raise ValueError('Повреждена группа ролей')
                    count=len(broken(local,providers))
                    damaged+=count;affected+=int(bool(count))
                except (HumanError,ValueError,TypeError):damaged+=1;affected+=1
    add('role_links','error' if damaged else 'ok','Ссылки в ролях моделей','Нажми «Починить ссылки в ролях» в самопроверке: удалятся только повреждённые кандидаты; запись останется в журнале.' if damaged else '',broken=damaged,agents=affected)
    for role,(title,_) in ROLES.items():
        chain=roles.get(role,[])
        valid=not broken({role:chain},providers)
        add('role:'+role,'error' if not valid else 'ok' if chain else 'warn','Роль: '+title,'Выбери цепочку в «Моделях».' if not chain or not valid else 'Настроена; запрос модели выполняется только по отдельному выбору.',candidates=len(chain) if isinstance(chain,list) else 0)
    vision_row(add,settings,roles,providers)
    for channel in ('telegram','mail'):
        on=settings.get(channel,'enabled',False)
        actual=getattr(app,channel,None) if app else None
        healthy=not actual or actual.state not in ('error',)
        add('channel:'+channel,'error' if on and not healthy else 'ok' if on else 'warn','Канал: '+('Telegram' if channel=='telegram' else 'почта'),'Проверь настройки/привязку в «Связи».' if on else 'Выключен; включи при необходимости в «Связи».',enabled=bool(on),state=actual.state if actual else 'не проверялся')
    state={}
    if reader:
        try:
            value=reader.one("SELECT value FROM meta WHERE key='backups.state'")
            state=json.loads(value[0]) if value else {}
            if not isinstance(state,dict):state={}
        except (sqlite3.Error,ValueError):pass
    backup_set=bool(settings.get('backups','path','') or settings.get('backups','second_path',''))
    verified=bool((state.get('verification') or {}).get('ok')) if isinstance(state.get('verification'),dict) else False
    add('backups','ok' if backup_set and verified else 'warn','Резервные копии','Выбери носитель, сделай снимок и нажми проверку в «Бекапах».' if not verified else '',configured=backup_set,last=state.get('last_backup'),verified=verified,last_verify=state.get('last_verify'))
    current=root
    while not current.exists() and current!=current.parent:current=current.parent
    try:
        disk=shutil.disk_usage(current)
        add('disk','ok' if disk.free>=256*1024*1024 else 'warn','Свободное место','Освободи место перед обновлением/бекапом.' if disk.free<256*1024*1024 else '',free_bytes=disk.free,total_bytes=disk.total)
    except OSError:add('disk','error','Место на диске недоступно','Проверь носитель и права.')
    boot=load_boot(root);slots=boot.get('slots') if isinstance(boot.get('slots'),dict) else {}
    add('watchdog','error' if boot.get('_error') else 'ok' if boot.get('starts') else 'warn','Сторож','Восстанови целый boot.txt; исходник не перезаписан.' if boot.get('_error') else 'Обычный bender run запускает Сторожа.',starts=boot.get('starts',0),safe=bool(boot.get('safe')),watched=bool(getattr(app,'watched',False)) if app else None)
    slot=slots.get('current')
    slot_ok=not slot or (isinstance(slot,str) and slot.startswith('v-') and all(char.isalnum() or char in '-_' for char in slot) and not (root/'versions').is_symlink() and not (root/'versions'/slot).is_symlink() and (root/'versions'/slot).is_dir())
    add('slots','ok' if slot_ok else 'error','Слоты версий','Откатись к прежнему целому слоту.' if not slot_ok else '',pending=bool(slots.get('pending')),has_previous=bool(slots.get('previous')))
    metrics=process_metrics(root,live=app is not None)
    add('process','ok' if metrics.get('running') else 'warn','Память и диск процесса','Сравни показатели сейчас и после суток работы.' if metrics.get('running') else 'Процесс не найден; запусти Бендера и повтори doctor.',**metrics)
    if reader:reader.close()
    return {'format':1,'version':__version__,'rows':rows,'ok':not any(row['level']=='error' for row in rows),'data':str(root)}


def vision_row(add,settings,roles,providers):
    """Видит ли главная картинки и кто опишет их, если нет; без запросов к моделям."""
    from ..router.capabilities import description
    if not (settings.get('attachments','enabled',True) and settings.get('attachments','vision',True)):
        add('vision','ok','Картинки','Передача фотографий выключена в настройках вложений.');return
    main=next((c for c in roles.get('main') or [] if isinstance(c,dict) and c.get('enabled',True) is not False),None)
    provider=providers.get(main.get('provider')) if main and isinstance(providers,dict) else None
    if not provider:return
    cap=description(provider,main.get('model',''),'vision')
    own=(main.get('provider'),main.get('model'))
    helpers=[c for c in roles.get('vision') or [] if isinstance(c,dict) and (c.get('provider'),c.get('model'))!=own]
    helper=bool(helpers) and settings.get('attachments','vision_fallback',True)
    mode=settings.get('attachments','vision_unknown','both')
    label=str(provider.get('name') or main.get('provider'))+'/'+str(main.get('model'))
    # Текст действия попадает в отчёт: только постоянные фразы, причина отказа — в карточке модели.
    remembered=' Запомнен отказ модели на картинку (причина — в её карточке в «Моделях»); если она на самом деле видит — поставь там «неизвестно».' if cap.get('source')=='rejected' else ''
    details={'main':label,'vision':cap.get('value'),'source':cap.get('source'),'vision_role':len(helpers),'unknown_mode':mode}
    if cap.get('value') is True:add('vision','ok','Картинки: главная видит сама','',**details)
    elif cap.get('value') is False:
        add('vision','ok' if helper else 'warn','Картинки: главная не видит',('Описывает роль «Зрение».' if helper else 'Агент получит только «содержимое неизвестно»: назначь модель со зрением в роль «Зрение».')+remembered,**details)
    elif cap.get('source')=='probe_unseen':
        # Ответ модели — только в её карточке: в отчёт идёт постоянная фраза.
        add('vision','warn','Картинки: главная принимает, но не видит','Проба зрения: модель приняла картинку, но ответила, что не видит её. Поставь «Нет» в её карточке в «Моделях» — картинки будет описывать роль «Зрение»'
            +('.' if helper else ' (назначь в неё модель со зрением).'),**details)
    elif helper and mode!='image':add('vision','ok','Картинки: неизвестно, видит ли главная','Картинки идут вместе с описанием роли «Зрение».',**details)
    else:
        add('vision','warn','Картинки: неизвестно, видит ли главная','Если за именем модели роутер, он может молча терять картинки. '
            +('Назначь модель со зрением в роль «Зрение» и оставь «И картинка, и описание».' if not helper else 'Выбрано «Только картинка»: описание Зрением выключено.')
            +' Проверить модель — проба зрения в её карточке в «Моделях».',**details)


async def repair_roles(app):
    """Явная починка постоянных агентов хозяином; пассивный doctor ничего не меняет."""
    from ..tools.registry import ToolContext
    from ..router.roles import broken
    root=app.host
    await ToolContext(app,actor='owner').check('settings','roles','убрать повреждённые ссылки ролей постоянных агентов')
    total=0
    for row in root.agents.public():
        ctx=root.agents.get(row['id'],archived=True)
        count=len(broken(ctx.settings.group('roles'),ctx.models.providers()))
        if count:ctx.settings.set('roles',{},by='owner');total+=count
    return {'count':total,'message':'Удалены повреждённые кандидаты: '+str(total)+'. Проверь Уши/Голос и другие опустевшие цепочки в «Моделях». Запись — в журнале.'}


async def probe_models(app):
    from ..tools.registry import ToolContext
    from ..router.config import ROLES
    ctx=ToolContext(app,actor='owner',session_id='doctor',tool='doctor')
    app.guard.grant('doctor',[('net.send','doctor/roles')])
    try:
        await ctx.check('net.send','doctor/roles','проверить настроенные роли маленькими запросами без памяти и разговоров')
        out=[]
        for role in ROLES:
            if not app.router.chain(role,fallback_main=False):continue
            try:
                started=time.monotonic()
                if role in ('ears','voice'):
                    cfg=app.voice.config()
                    if not cfg['enabled'] or not cfg['stt' if role=='ears' else 'tts']:
                        out.append({'id':'probe:'+role,'level':'warn','title':'Роль: '+ROLES[role][0],'action':'Голос выключен. Проверка не меняет выключатели; включи нужный канал в настройках.'});continue
                    if role=='ears':
                        import io
                        import wave
                        stream=io.BytesIO()
                        with wave.open(stream,'wb') as audio:
                            audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(16000);audio.writeframes(b'\0'*32000)
                        await asyncio.wait_for(app.voice.transcribe(stream.getvalue(),ext='wav',seconds=1,session_id='doctor'),20)
                    else:await asyncio.wait_for(app.voice.speak('Готов.',session_id='doctor',format='wav'),20)
                elif role=='embed':
                    await asyncio.wait_for(app.router.embed(['готов']),20)
                    app.store.add_call(session_id=None,role='doctor',provider='',model='',usage={'cost_known':False},cost=0,seconds=time.monotonic()-started,notice='Проверка эмбеддингов; цена не сообщена провайдером',request={'purpose':'doctor','role':role})
                else:
                    result=await asyncio.wait_for(app.router.call([{'role':'user','content':'Ответь одним словом: готов.'}],role=role,params={'max_tokens':8}),20)
                    app.store.add_call(session_id=None,role='doctor',provider=result.provider_name,model=result.model,usage=result.usage.to_dict(),cost=result.cost,seconds=result.seconds,notice='самопроверка роли',request={'purpose':'doctor','role':role})
                out.append({'id':'probe:'+role,'level':'ok','title':'Роль: '+ROLES[role][0],'action':'Ответ получен.'})
            except (HumanError,asyncio.TimeoutError):out.append({'id':'probe:'+role,'level':'error','title':'Роль: '+ROLES[role][0],'action':'Запрос не прошёл; проверь цепочку и ключи в «Моделях».'})
        return out
    finally:app.guard.revoke('doctor',[('net.send','doctor/roles')])
