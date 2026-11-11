"""Автозапуск хозяина: одна реализация для CLI, установщика и пульта."""
import asyncio
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

from .errors import HumanError
from .fsutil import atomic_write_text
from .paths import PROGRAM_DIR,default_data_dir
from .pathutil import canonical,within,same
from .run_lock import current,describe


def termux():return bool(os.environ.get('TERMUX_VERSION'))


def unit_path(voice=False):
    name='bender-voice' if voice else 'bender'
    return Path.home()/('.termux/boot' if termux() else '.config/systemd/user')/(name if termux() else name+'.service')


def command(args,*,optional=False):
    try:
        result=subprocess.run(args,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=20,text=True)
    except (OSError,subprocess.SubprocessError):
        if optional:return None
        raise HumanError('service','Не удалось обратиться к службе','Проверь systemctl --user status bender и журнал пользователя.') from None
    if result.returncode:
        if optional:return None
        raise HumanError('service','Служба не приняла команду','Проверь доступность systemd пользователя; journalctl --user -u bender -n 50.',detail='Код '+str(result.returncode))
    return result.stdout[:16000]


def available():
    return termux() or sys.platform.startswith('linux') and bool(shutil.which('systemctl')) and command(['systemctl','--user','show','default.target','--property=LoadState'],optional=True) is not None


def status(data,*,voice=False):
    path=unit_path(voice);running=None if voice else current(data)
    ready=available();state={}
    if ready and not termux():
        raw=command(['systemctl','--user','show','bender-voice.service' if voice else 'bender.service','--property=LoadState,ActiveState,SubState,UnitFileState'],optional=True) or ''
        state={k:v for line in raw.splitlines() for k,sep,v in [line.partition('=')] if sep and k in ('LoadState','ActiveState','SubState','UnitFileState')}
    return {'available':ready,'platform':'termux' if termux() else 'systemd' if sys.platform.startswith('linux') else 'manual',
            'installed':path.is_file(),'path':str(path),'state':state,'running':running,
            'message':describe(running) if running else 'Эта папка данных сейчас не запущена.'}


def quote(value):return '"'+str(value).replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('\n','\\n')+'"'


def bare_path(value):
    # WorkingDirectory= не снимает кавычки: «"/home/…"» systemd считает относительным путём и не грузит unit.
    value=str(value)
    if '\n' in value or not value.startswith('/'):
        raise HumanError('service_program','Путь программы не подходит для systemd','Нужен абсолютный путь без перевода строки; перенеси Бендера в обычную папку.')
    return value.replace('%','%%')


def program_dir():
    path=canonical(os.environ.get('BENDER_PROGRAM_DIR') or PROGRAM_DIR)
    if not (path/'bender/__main__.py').is_file():
        raise HumanError('service_program','Не найдена папка программы для автозапуска','Запусти bender service из установки Бендера; проверь BENDER_PROGRAM_DIR.')
    return path


def core_unit(data):
    # Environment не выполняется оболочкой. Данные заданы явно, независимо от cwd службы.
    program=program_dir()
    values={'PATH':os.environ.get('PATH',''),'PYTHONPATH':str(program),'PYTHONUNBUFFERED':'1',
            'BENDER_HOME':str(canonical(data)),'BENDER_PROGRAM_DIR':str(program),
            'BENDER_SERVICE_DATA':str(canonical(data)),'BENDER_SERVICE_KIND':'systemd'}
    return ('[Unit]\nDescription=Бендер — постоянно живущий ИИ-агент\nAfter=network-online.target\n\n[Service]\n'
            +'WorkingDirectory='+bare_path(program)+'\nExecStart='+quote(sys.executable).replace('$','$$')+' -m bender --no-anim run --no-browser\n'
            +'Restart=on-failure\nRestartSec=5\nTimeoutStopSec=30\n'+''.join('Environment='+quote(k+'='+v)+'\n' for k,v in values.items())
            +'\n[Install]\nWantedBy=default.target\n')


def check_path(path,data,voice):
    if not within(canonical(path.parent),Path.home()):
        raise HumanError('service_path','Папка автозапуска является ссылкой наружу','Хозяин должен исправить папку службы; файл не изменён.')
    expected=Path(data)/'local/voice/bender-voice.service'
    if path.is_symlink() and not (voice and same(path,expected)):
        raise HumanError('service_path','Путь службы является посторонней ссылкой','Проверь прежний unit вручную; он не изменён.')


def install(app,*,voice=False):
    data=app.host.dirs.root;path=unit_path(voice)
    if not available():
        raise HumanError('service_unavailable','Автозапуск здесь недоступен','Нужен systemd пользователя или Termux:Boot. Пока запускай bender вручную.')
    check_path(path,data,voice)
    running=current(data)
    if voice:
        folder=data/'local/voice';config=app.host.settings.group('voice_local')
        executable=folder/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        if not executable.is_file() or not (folder/'installed.json').is_file():
            raise HumanError('voice_missing','Локальный голос ещё не установлен','Сначала bender voice install; service install --voice не скачивает модели.')
        from ..voice.install import unit
        content=unit(folder,config['port'],config['threads'])
        argv=[str(executable),str(Path(__file__).parents[1]/'voice/local_server.py'),'--folder',str(folder),'--port',str(config['port']),'--threads',str(config['threads'])]
    else:
        content=core_unit(data);argv=[sys.executable,'-m','bender','--no-anim','run','--no-browser']
    path.parent.mkdir(parents=True,exist_ok=True)
    if termux():
        bash=shutil.which('bash') or '/data/data/com.termux/files/usr/bin/bash'
        program=program_dir()
        env={'PATH':os.environ.get('PATH',''),'PYTHONPATH':str(program),'BENDER_PROGRAM_DIR':str(program),'BENDER_HOME':str(data),'BENDER_SERVICE_KIND':'termux'}
        content='#!'+bash+'\n'+''.join('export '+k+'='+shlex.quote(v)+'\n' for k,v in env.items())+'termux-wake-lock\nexec '+shlex.join(argv)+' >> '+shlex.quote(str(Path.home()/('.bender-voice-boot.log' if voice else '.bender-boot.log')))+' 2>&1\n'
    atomic_write_text(path,content);path.chmod(0o700 if termux() else 0o644)
    if termux():
        note='Автозапуск записан. Установи Termux:Boot из F-Droid и открой его один раз; он начнёт запускать Бендера после загрузки.'
    else:
        command(['systemctl','--user','daemon-reload'])
        # Ручную копию не дублируем: включаем только будущий автозапуск.
        start=voice or not running or running.get('kind')=='systemd'
        command(['systemctl','--user','enable',*(['--now'] if start else []),path.name])
        note='Автозапуск включён.'
        if running and not start:note+=' '+describe(running)+' Затем запусти службу: systemctl --user start bender.'
    app.journal.write('system','Хозяин включил автозапуск'+(' голоса' if voice else ''),actor='owner',data={'voice':voice,'manual_running':bool(running and running.get('kind')=='manual')})
    return {'ok':True,'message':note,'path':str(path),'voice':voice}


def remove(app,*,voice=False):
    path=unit_path(voice)
    check_path(path,app.host.dirs.root,voice)
    if not termux():
        if not available():raise HumanError('service_unavailable','Нет доступа к systemd пользователя','Удаляй службу из пользовательского сеанса с доступным systemctl --user.')
        command(['systemctl','--user','disable','--now',path.name])
    if path.exists():path.unlink()
    if not termux():command(['systemctl','--user','daemon-reload'])
    app.journal.write('system','Хозяин удалил автозапуск'+(' голоса' if voice else ''),actor='owner')
    note='Автозапуск удалён.'
    if termux():note+=' Текущий ручной процесс останавливается отдельно через Ctrl+C или /shutdown.'
    return {'ok':True,'message':note,'voice':voice}


async def change(app,action,*,voice=False):
    from ..tools.registry import ToolContext
    if action not in ('install','remove'):raise HumanError('service','Неизвестное действие','Выбери install, remove или status.')
    await ToolContext(app,actor='owner').check('system.service',str(unit_path(voice)),('Включить' if action=='install' else 'Удалить')+' автозапуск',detail={'voice':voice})
    try:return await asyncio.to_thread(install if action=='install' else remove,app,voice=voice)
    except OSError as error:
        raise HumanError('service_file','Не удалось записать автозапуск','Проверь права и место в папке службы; затем повтори команду.',detail=str(error)) from None


def main(args,*,app_factory):
    app=None
    try:
        data=Path(args.data or default_data_dir())
        if args.action=='status':result=status(data,voice=args.voice)
        else:
            app=app_factory(data)
            result=asyncio.run(change(app,args.action,voice=args.voice))
        if args.json:print(json.dumps(result,ensure_ascii=False,indent=2))
        else:
            print(result['message'])
            if args.action=='status':print('Автозапуск: '+('установлен' if result['installed'] else 'не установлен')+'; '+str(result['state']))
        return 0
    except HumanError as error:
        print(error.title+'. '+error.hint);return 1
    finally:
        if app:asyncio.run(app.close())
