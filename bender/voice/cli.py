"""Команды хозяина для отдельного голоса: установка, служба и измерение RTF."""
from __future__ import annotations
import asyncio
import io
import json
from pathlib import Path
import sys
import time
import wave
from ..core.errors import HumanError
from ..core.paths import default_data_dir
from ..tools.registry import ToolContext


def folder_of(app):
    from .connect import folder
    return folder(app)


def status(app):
    from .connect import connection
    folder=folder_of(app);installed=folder/'installed.json'
    return {'settings':app.host.settings.group('voice_local'),'installed':installed.is_file(),'models':json.loads(installed.read_text()) if installed.is_file() else None,
            **connection(app),'agents':[{'name':row['name'],**connection(app.host.agents.get(row['id']))} for row in app.host.agents.public() if not row['archived']],
            'note':'Установка: bender voice install; подключить без установки: bender voice connect <агент|all>; проверка скорости: bender voice bench; остановка: bender voice stop.'}


def own_voice(app,agent):
    """Личный голос Piper агента, если он задан."""
    if not agent:return None
    return app.host.agents.get(app.host.agents.resolve(agent)).settings.get('voice','piper','') or None


def token_of(app):
    try:token=(folder_of(app)/'token').read_text().strip()
    except OSError:raise HumanError('local_voice','Локальный голос ещё не установлен','Выполни bender voice install.') from None
    if not token:raise HumanError('local_voice','Нет ключа локального голоса','Повтори bender voice install.')
    return token


async def request(app,path,body=None,ctype='application/json'):
    from ..net.http_client import Timeouts,NetError
    cfg=app.host.settings.group('voice_local')
    url='http://127.0.0.1:'+str(cfg['port'])+path
    headers={'Authorization':'Bearer '+token_of(app),'Content-Type':ctype}
    try:
        response=await app.host.http.request('POST' if body is not None else 'GET',url,body=body,headers=headers,timeouts=Timeouts(connect=5,read=300,total=310))
        async with response:raw=await response.read(limit=25*1024*1024)
        if not response.ok:raise HumanError('local_voice','Локальный голос не выполнил запрос','Проверь установку и формат; подробности голоса не записываются.')
        return raw
    except NetError:raise HumanError('local_voice','Отдельная голосовая служба не отвечает','Запусти bender voice serve или проверь systemctl --user status bender-voice.') from None


async def bench(app):
    await ToolContext(app,actor='owner').check('audio.speak','local_voice','измерить синтез локального голоса')
    text='Привет! Это проверка русского голоса Бендера. Я помогаю хозяину и помню наши разговоры.'
    payload=json.dumps({'model':'piper-ru','input':text,'response_format':'wav'},ensure_ascii=False).encode()
    timings=[]
    for _ in range(2):
        started=time.monotonic();wav=await request(app,'/v1/audio/speech',payload);timings.append(time.monotonic()-started)
    try:
        with wave.open(io.BytesIO(wav)) as reader:duration=reader.getnframes()/reader.getframerate()
    except (ValueError,wave.Error):raise HumanError('local_voice','Локальный голос не вернул WAV для измерения','Проверь установку и повтори bench.') from None
    if duration<=0:raise HumanError('local_voice','Пустой звук не подходит для замера','Повтори bench.')
    await ToolContext(app,actor='owner').check('audio.transcribe','local_voice','измерить распознавание локального голоса')
    from .manager import multipart
    body,ctype=multipart({'model':'gigaam-v3-e2e-ctc'},wav,'wav');stt=[];result={}
    for _ in range(2):
        started=time.monotonic();result=json.loads(await request(app,'/v1/audio/transcriptions',body,ctype));stt.append(time.monotonic()-started)
    if not isinstance(result.get('text'),str) or not result['text'].strip():raise HumanError('local_voice','Распознавание не дало текста для проверки','Проверь GigaAM и повтори bench.')
    ratio=max(timings[-1],stt[-1])/duration
    return {'audio_seconds':duration,'tts':{'cold_seconds':timings[0],'seconds':timings[-1],'rtf':timings[-1]/duration},'stt':{'cold_seconds':stt[0],'seconds':stt[-1],'rtf':stt[-1]/duration},
            'note':'RTF меньше 1 — быстрее реального времени. Холодный запрос включает загрузку моделей; тёплый — HTTP и обработку.',
            'advice':'Этот голос и качество укладываются в реальное время.' if ratio<1 else 'Попробуй int8 или другой голос Piper и повтори bench; добавь облако запасным в аудиоцепочку. Скорость других голосов здесь не измерялась.'}


async def stop(app):
    from .install import command
    await ToolContext(app,actor='owner').check('voice.install',str(folder_of(app)),'остановить и отключить отдельную голосовую службу')
    app.host.settings.set('voice_local',{'enabled':False},by='owner')
    stopped=False
    try:
        await request(app,'/shutdown',b'{}');stopped=True
    except HumanError:pass
    systemd=False
    if sys.platform.startswith('linux') and (folder_of(app)/'bender-voice.service').exists():
        try:await asyncio.to_thread(command,['systemctl','--user','disable','--now','bender-voice.service']);systemd=True
        except HumanError:pass
    return {'enabled':False,'server_stopped':stopped,'systemd_disabled':systemd,'note':'Локальная цепочка выключена. Если служба systemd недоступна, проверь её отключение вручную.'}


async def run(args,app_factory=None):
    from .install import install,PACKAGES,REPOS,DEFAULT,chosen,command
    if args.plan:
        return {'packages':list(PACKAGES),'repositories':REPOS,'voice':args.voice or 'из настроек voice_local.voice (по умолчанию '+DEFAULT+')','process':'отдельный venv, localhost и собственный ключ; ядро stdlib','note':'Без загрузки. Установка проверит commit и хеш каждого файла модели; ARMv7 без проверенных колёс не поддерживается.'}
    if getattr(args,'target',None) and args.action!='connect':raise HumanError('local_voice','Имя после команды поддерживает только voice connect','Для установки одному агенту: bender voice install --agent main.')
    if app_factory is None:raise HumanError('local_voice','Нет фабрики приложения для голосовой команды','Запусти команду через bender voice.')
    app=app_factory(args.data or default_data_dir())
    try:
        if args.action=='status':return status(app)
        if args.action=='connect':
            from .connect import connect
            return await connect(ToolContext(app,actor='owner'),args.target or args.agent or 'main')
        if args.action=='install':return await install(app,agent=args.agent,voice=args.voice or chosen(own_voice(app,args.agent) or app.settings.get('voice_local','voice',DEFAULT)),service=not args.no_service)
        if args.action=='bench':return await bench(app)
        if args.action=='stop':return await stop(app)
        if args.action=='serve':
            folder=folder_of(app)
            if not (folder/'installed.json').is_file():raise HumanError('local_voice','Голосовой пакет не установлен','Выполни bender voice install.')
            token_of(app)
            await ToolContext(app,actor='owner').check('voice.install',str(folder),'запустить отдельный голосовой процесс')
            app.settings.set('voice_local',{'enabled':True},by='owner');cfg=app.settings.group('voice_local')
            python=folder/'venv'/('Scripts/python.exe' if sys.platform=='win32' else 'bin/python')
            await asyncio.to_thread(command,[python,Path(__file__).with_name('local_server.py'),'--folder',folder,'--port',cfg['port'],'--threads',cfg['threads']],timeout=None)
            return {'stopped':True}
        raise HumanError('local_voice','Нет такой голосовой команды','Используй voice install, connect, serve, bench, status или stop.')
    finally:await app.close()


def main(args,*,app_factory=None):
    try:
        print(json.dumps(asyncio.run(run(args,app_factory)),ensure_ascii=False,indent=2));return 0
    except HumanError as error:print(error.title+'. '+error.hint,file=sys.stderr);return 2
    except (OSError,ValueError):print('Не удалось подготовить локальный голос. Проверь права, место и установленный пакет.',file=sys.stderr);return 2
