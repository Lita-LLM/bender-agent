"""Выбор реальной защиты ОС; лимиты ресурсов не зависят от изоляции файлов."""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from functools import lru_cache
from pathlib import Path

CHILD = Path(__file__).with_name("sandbox_child.py")
SSH_CONFIG_D = "/etc/ssh/ssh_config.d"


@lru_cache(maxsize=8)
def _usable(path: str, kind="bwrap") -> bool:
    """Наличие программы не означает поддержку namespaces/LSM в текущем ядре."""
    import subprocess

    commands = {
        "bwrap": [path, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--unshare-all", "/bin/true"],
        "unshare": [path, "--user", "--map-root-user", "--net", "--fork", "--kill-child", "/bin/true"],
        "landlock": [sys.executable, str(CHILD), "--probe-landlock"],
    }
    try:
        return subprocess.run(commands[kind], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=2).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


@lru_cache(maxsize=1)
def detect() -> dict:
    info = {"kind": "none", "fs": False, "net": False, "landlock": False, "abi": 0,
            "title": "Изоляции файлов и сети нет; команды имеют права пользователя и спрашивают хозяина."}
    if sys.platform.startswith("linux"):
        bwrap = shutil.which("bwrap")
        if bwrap and _usable(bwrap):
            return {**info, "kind": "bwrap", "path": bwrap, "fs": True, "net": True,
                    "title": "bubblewrap: система на чтение, рабочая папка на запись, отдельный /tmp; сеть закрывается."}
        from .sandbox_child import landlock_abi

        abi = landlock_abi()
        locked = abi > 0 and _usable(str(CHILD), "landlock")
        unshare = shutil.which("unshare")
        network = bool(unshare and _usable(unshare, "unshare"))
        if locked or network:
            title = (f"Landlock ABI {abi}: запись только в рабочую и временную папки. " if locked else "Изоляции файлов нет. ")
            if locked and abi < 3:
                title += "Старое ядро не ограничивает truncate; полная защита записи недоступна. "
            title += "unshare: отдельная сеть." if network else "Изоляции сети нет; для запуска без сети нужно доступное пространство имён."
            return {**info, "kind": "landlock" if locked else "unshare", "fs": locked and abi >= 3, "net": network,
                    "landlock": locked, "abi": abi, "unshare": unshare if network else None, "title": title}
    return info


def for_app(agent_ctx, *, owner=False):
    info = dict(detect()) if agent_ctx.settings.get("guard", "sandbox", "auto") == "auto" else {
        "kind": "none", "fs": False, "net": False, "landlock": False, "title": "Изоляция файлов и сети выключена хозяином."}
    info['windows_job']=sys.platform=='win32' and agent_ctx.settings.get('guard','windows_job',True)
    enabled = agent_ctx.settings.get("guard", "resource_limits", True) and (os.name == "posix" and sys.platform!='win32' or info['windows_job'])
    info["limits"] = {
        "RLIMIT_CPU": agent_ctx.settings.get("guard", "cpu_seconds", 120),
        "RLIMIT_AS": agent_ctx.settings.get("guard", "memory_mb", 1024) * 1024 * 1024,
        "RLIMIT_NPROC": agent_ctx.settings.get("guard", "process_count", 128),
        "RLIMIT_FSIZE": agent_ctx.settings.get("guard", "file_mb", 64) * 1024 * 1024,
    } if enabled else {}
    if not info["limits"].get("RLIMIT_AS"):info["limits"].pop("RLIMIT_AS",None)  # 0 — без лимита памяти
    if info['windows_job']:
        info['limits'].pop('RLIMIT_FSIZE',None)
        info['title']+=' Windows Job Object: дерево завершается вместе с Бендером.'+(' Память, ЦП и число процессов ограничены суммарно, включая обёртку; размер файла не ограничивается.' if enabled else ' Численные лимиты выключены.')
    else:info["title"] += " Лимиты ресурсов POSIX включены." if enabled else " Лимиты ресурсов не применяются."
    if hasattr(agent_ctx, 'dirs') and hasattr(agent_ctx, 'host'):
        info['data_root'] = str(agent_ctx.host.dirs.root)
        info['agent_read'] = [str(getattr(agent_ctx.dirs, name)) for name in ('self_dir', 'diary', 'skills')]
        if getattr(agent_ctx,'_task_agent',None):info['agent_read'] += list(map(str,agent_ctx._task_agent.read_dirs()))
        if hasattr(agent_ctx.host, 'agents'):
            rights=agent_ctx.host.agents.rights.effective(agent_ctx)
            info['agent_isolation']=bool(rights['require_isolation'] and not owner)
            info['ssh']=bool(rights['ssh'])
            info['owner_process']=owner
            info['home']=str(Path.home())
            info['ssh_auth_sock']=os.environ.get('SSH_AUTH_SOCK','')
            info['extra_read']=rights.get('work_folders') or []
            if info['kind']!='bwrap':
                info['title']+=' Без bubblewrap команды этого агента видят файлы других агентов и домашние ключи.'
                if info['agent_isolation']:info['title']+=' Запуск закрыт: sudo apt install bubblewrap или Стая → Права → Требовать изоляцию команд.'
    return info


def confined(info, allow_net=False):
    return bool(info.get("fs") and (allow_net or info.get("net")))


def wrap(command: str, *, info: dict, work_dir: Path, allow_net: bool, read_dirs=(), cwd=None) -> list[str] | None:
    """Низкоуровневая обёртка namespaces; ресурсы и Landlock добавляет prepare."""
    work = str(Path(work_dir).resolve())
    if info.get('agent_isolation') and info.get('kind') != 'bwrap':
        from ..core.errors import HumanError
        raise HumanError('agent_isolation', 'Шаблон агента требует изоляцию файлов команд', 'Установи sudo apt install bubblewrap и выбери Настройки → Предохранитель → Песочница команд → Авто. Или хозяин: Стая → Права → Требовать изоляцию команд — выключить; команды получат доступ к чужим файлам.')
    if info.get("write_work", True):
        from .control import CONTROL_PATHS, source_roots, within
        from ..core.errors import HumanError

        if any(within(Path(work), root / name) or within(root / name, Path(work)) for root in source_roots() for name in CONTROL_PATHS):
            raise HumanError("sealed", "Рабочая папка процесса пересекается с контуром доверия", "Выбери отдельную папку данных; экзамены и защита доступны только на чтение.")
    if info.get("kind") == "bwrap":
        args = [info["path"], "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
        from ..core.pathutil import canonical, within
        for directory in info.get('extra_read',[]):
            path=canonical(directory)
            # Служебные данные остаются закрыты даже при выборе родительской папки.
            if info.get('data_root') and within(path,info['data_root']):continue
            args+=['--ro-bind',str(path),str(path)]
        if info.get('data_root'):
            args += ['--tmpfs', info['data_root']]
            for path in info.get('agent_read', []): args += ['--ro-bind', path, path]
        args += ["--bind" if info.get("write_work", True) else "--ro-bind", work, work, "--chdir", str(cwd or work), "--die-with-parent", "--new-session", "--unshare-all"]
        if allow_net:
            args.append("--share-net")
        for directory in read_dirs:
            path = str(Path(directory).resolve())
            args += ["--ro-bind", path, path]
        # Применяем после всех разрешённых папок: дополнительный bind не открывает ключи.
        if not info.get('owner_process'):
            from .policy import PROTECTED_HOME
            from ..core.pathutil import canonical, within
            from ..core.errors import HumanError
            home=Path(info.get('home') or Path.home())
            for rel in PROTECTED_HOME:
                path=canonical(home/rel)
                if not path.exists():continue
                if rel=='.ssh' and info.get('ssh'):
                    if not path.is_dir():
                        raise HumanError('ssh_path','~/.ssh не является папкой','Хозяин должен исправить ~/.ssh или выключить право Стая → Права → SSH.')
                    if info.get('data_root') and (within(path,info['data_root']) or within(info['data_root'],path)):
                        raise HumanError('ssh_path','Папка SSH пересекается со служебными данными','Хозяин должен выбрать отдельную ~/.ssh; ссылка не должна открывать данные Бендера.')
                    args+=['--ro-bind',str(path),str(path)]
                    known=path/'known_hosts'
                    if known.is_symlink() or known.exists() and known.stat().st_nlink!=1:
                        raise HumanError('ssh_hosts','known_hosts является ссылкой','Убери символьную/жёсткую ссылку; SSH открывает запись только в отдельный known_hosts, не в ключи.')
                    if not known.exists():known.touch(mode=0o600)
                    args+=['--bind',str(known),str(known)]
                else:args+=['--tmpfs',str(path)] if path.is_dir() else ['--ro-bind','/dev/null',str(path)]
            sock=info.get('ssh_auth_sock')
            if info.get('ssh') and sock and canonical(sock).is_socket():
                args+=['--ro-bind',str(canonical(sock)),str(canonical(sock))]
            elif sock and canonical(sock).is_socket():
                args+=['--ro-bind','/dev/null',str(canonical(sock))]
        return args + ["/bin/sh", "-c", command]
    if info.get("unshare") and not allow_net:
        return [info["unshare"], "--user", "--map-root-user", "--net", "--fork", "--kill-child", "/bin/sh", "-c", command]
    return None


class Prepared:
    def __init__(self, command, *, info, work_dir, allow_net, cwd=None, read_dirs=(), temp_root=None,raw_argv=None):
        self.temporary = None
        self.job = None
        self.env = {}
        self.argv = wrap(command, info=info, work_dir=work_dir, allow_net=allow_net, cwd=cwd, read_dirs=read_dirs)
        if not info.get('ssh') and not info.get('owner_process'):self.env['SSH_AUTH_SOCK']=''
        elif info.get('ssh_auth_sock'):
            from ..core.pathutil import canonical
            self.env['SSH_AUTH_SOCK']=str(canonical(info['ssh_auth_sock']))
        if info.get('windows_job'):
            from .windows_job import Job
            from ..core.errors import HumanError
            try:self.job=Job(info.get('limits'))
            except OSError as exc:raise HumanError('windows_job','Windows не создала ограничитель процессов','Проверь поддержку Job Objects; запуск без защиты не выполнен.',detail=str(exc)) from None
            config={'limits':{},'landlock':False,'write':[],'windows_job':self.job.name,'windows_shell':not bool(raw_argv)}
            target=list(raw_argv) if raw_argv else [command]
            self.argv=[sys.executable,'-I',str(CHILD),json.dumps(config,separators=(',',':')),*target]
            return
        if os.name != "posix":
            return
        if info.get("kind") == "bwrap":
            self.env.update({"TMPDIR": "/tmp", "TMP": "/tmp", "TEMP": "/tmp"})
            private = None
            if allow_net:
                self._ssh_config(temp_root)
        else:
            self.temporary = tempfile.TemporaryDirectory(prefix="sandbox-", dir=temp_root)
            private = self.temporary.name
            self.env.update({"TMPDIR": private, "TMP": private, "TEMP": private})
        if self.argv is None:
            self.argv = ["/bin/sh", "-c", command]
        config = {"limits": info.get("limits", {}), "landlock": bool(info.get("landlock")),
                  "write": ([str(Path(work_dir).resolve())] if info.get("write_work", True) else []) + ([private] if private else [])}
        if info.get("kind") == "bwrap":
            # NPROC считается по uid: не ограничиваем создание самого namespace.
            self.argv = self.argv[:-3] + ["--ro-bind", str(CHILD), str(CHILD), sys.executable, str(CHILD), json.dumps(config, separators=(",", ":")), *self.argv[-3:]]
            return
        if info.get("unshare") and not allow_net:
            # Всё ставим внутри: unshare нужна запись uid_map до Landlock, а NPROC снаружи
            # считал бы все процессы хозяина и не дал бы unshare даже fork.
            self.argv = self.argv[:-3] + [sys.executable, str(CHILD), json.dumps(config, separators=(",", ":")), *self.argv[-3:]]
            return
        config["nproc_relative"] = True
        self.argv = [sys.executable, str(CHILD), json.dumps(config, separators=(",", ":")), *self.argv]

    def _ssh_config(self, temp_root, source=SSH_CONFIG_D):
        """В namespace файлы root принадлежат «никому», а ssh для Include требует владельца root или себя.
        Без копии ssh под bubblewrap падает на /etc/ssh/ssh_config.d/*.conf (systemd 256+ кладёт туда свой)."""
        try:
            files = [item for item in Path(source).iterdir() if item.is_file()]
        except OSError:
            return
        uid = os.getuid()
        if all(item.stat().st_uid == uid for item in files):
            return
        self.temporary = tempfile.TemporaryDirectory(prefix="sandbox-", dir=temp_root)
        copy = Path(self.temporary.name) / "ssh_config.d"
        copy.mkdir()
        for item in files:
            try:
                shutil.copyfile(item, copy / item.name)
                os.chmod(copy / item.name, 0o644)
            except OSError:
                continue  # нечитаемый файл ssh пропустил бы и без песочницы
        self.argv = self.argv[:-3] + ["--ro-bind", str(copy), str(Path(source).resolve())] + self.argv[-3:]

    def close(self):
        if self.job is not None:
            self.job.close();self.job=None
        if self.temporary is not None:
            self.temporary.cleanup()
            self.temporary = None

    def bind(self,process):
        if self.job is not None:process._bender_job=self.job

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def prepare(command, **kwargs):
    return Prepared(command, **kwargs)
