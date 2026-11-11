"""Блокировка одной работающей копии; CLI с той же базой остаются доступны."""
import json
import os
from pathlib import Path
import sys
import stat

from .errors import HumanError
from .pathutil import canonical, within


def launch_kind():
    if os.environ.get('BENDER_SERVICE_KIND'):return os.environ['BENDER_SERVICE_KIND']
    return 'systemd' if os.environ.get('BENDER_SERVICE_DATA') else 'manual'


def _lock(file, acquire=True):
    if os.name=='nt':
        import msvcrt
        file.seek(0)
        msvcrt.locking(file.fileno(),msvcrt.LK_NBLCK if acquire else msvcrt.LK_UNLCK,1)
    else:
        import fcntl
        fcntl.flock(file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB if acquire else fcntl.LOCK_UN)


def describe(info):
    pid=info.get('pid') or '?'
    kind={'systemd':'службой systemd','termux':'через Termux:Boot','manual':'вручную'}.get(info.get('kind'),'неизвестным способом')
    if info.get('watched'):kind+=' через Сторожа'
    stop='systemctl --user stop bender' if info.get('kind')=='systemd' else f'Ctrl+C в его терминале или kill -TERM {pid}'
    return f'Данные уже держит Бендер PID {pid}, запущен {kind}. Остановить: {stop}.'


def current(data):
    path=Path(data)/'local'/'run.lock'
    if not path.exists():return None
    try:
        with path.open('r+b') as file:
            try:_lock(file)
            except OSError:
                file.seek(1 if os.name=='nt' else 0)
                try:
                    info=json.loads(file.read(8192))
                    return info if isinstance(info,dict) else {'pid':None,'kind':'unknown'}
                except (ValueError,UnicodeError):return {'pid':None,'kind':'unknown'}
            else:_lock(file,False);return None
    except OSError as exc:
        raise HumanError('run_lock','Не удалось проверить работающую копию','Проверь права папки local.',detail=str(exc)) from None


def preflight(data):
    """Короткий дочерний процесс: отказ до изменения счётчиков Сторожем."""
    try:
        info=current(canonical(data))
        if info:
            print('Эта папка данных уже открыта работающим Бендером. '+describe(info))
            return 78
        return 0
    except HumanError as error:
        print(error.title+'. '+error.hint);return 78


class RunLock:
    def __init__(self,data):
        root=canonical(data);path=root/'local'/'run.lock';self.file=None
        if not within(canonical(path),root):
            raise HumanError('run_lock','Блокировка выходит из папки данных','Убери ссылку local/run.lock или local на чужую папку.')
        try:
            path.parent.mkdir(parents=True,exist_ok=True)
            fd=os.open(path,os.O_RDWR|os.O_CREAT|getattr(os,'O_NOFOLLOW',0),0o600)
            file=os.fdopen(fd,'r+b')
            self.file=file
            info=os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink!=1:
                file.close();self.file=None
                raise HumanError('run_lock','Блокировка не является отдельным обычным файлом','Убери ссылку local/run.lock; другие файлы не изменены.')
            try:_lock(file)
            except OSError:
                file.seek(1 if os.name=='nt' else 0)
                try:info=json.loads(file.read(8192))
                except (ValueError,UnicodeError):info={}
                if not isinstance(info,dict):info={}
                file.close()
                self.file=None
                raise HumanError('already_running','Эта папка данных уже открыта работающим Бендером',describe(info),status=409) from None
            info={'pid':os.getpid(),'parent_pid':os.getppid(),'kind':launch_kind(),'watched':os.environ.get('BENDER_WATCHED')=='1','python':sys.executable}
            # Windows запрещает другим дескрипторам читать заблокированный байт 0.
            file.seek(0);file.truncate();file.write((b'\0' if os.name=='nt' else b'')+json.dumps(info,ensure_ascii=False).encode());file.flush()
            self.file=file
        except OSError as exc:
            self.close()
            raise HumanError('run_lock','Не удалось заблокировать папку данных','Проверь права local/run.lock; вторая копия не запускается.',detail=str(exc)) from None

    def close(self):
        if self.file is not None:self.file.close();self.file=None

    def __enter__(self):return self
    def __exit__(self,*args):self.close()
