"""Минимальная среда кода Мастерской: без базы, ключей и прямой сети."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

from ..core.errors import HumanError
from ..guard import sandbox


class Confined:
    def __init__(self, agent_ctx, package, arguments, *, capabilities=(), network_hosts=None, ctx=None, active=None):
        info = sandbox.for_app(agent_ctx)
        if info.get("kind") != "bwrap":
            raise HumanError("workshop_isolation", "Коду Мастерской нужен работающий bubblewrap", "Без изоляции файлов и сети код не запускается.")
        if any(c not in ("exec", "fs.read", "fs.write", "net.read") for c in capabilities):
            raise HumanError("workshop_caps", "Эта способность пока недоступна коду Мастерской", "Доступны exec, fs.read/fs.write и net.read через посредник; сообщения недоступны.")
        if "fs.write" in capabilities and "fs.read" not in capabilities:
            raise HumanError("workshop_caps", "Для записи нужна также способность чтения рабочей папки", "ОС не отделяет чтение от записи в открытой папке.")
        self.proxy = None
        if "net.read" in capabilities:
            from .proxy import hosts, status
            if not status(agent_ctx)['enabled']:
                raise HumanError('workshop_network', 'Сетевой посредник выключен', 'Хозяин включает его в настройках Мастерской.')
            network_hosts = hosts(network_hosts)
        self.temporary = tempfile.TemporaryDirectory(prefix="workshop-run-", dir=agent_ctx.dirs.local)
        python = str(Path(sys.executable).resolve())
        limits = {name: min(info["limits"].get(name, ceiling), ceiling) for name, ceiling in
                  {"RLIMIT_CPU": 120, "RLIMIT_AS": 1024**3, "RLIMIT_NPROC": 1024, "RLIMIT_FSIZE": 64 * 1024**2}.items()}
        args = [info["path"], "--unshare-all", "--die-with-parent", "--new-session", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
        for raw in sorted(set(["/usr", "/bin", "/sbin", "/lib", "/lib64", sys.base_prefix, str(Path(sys.base_prefix).resolve()), str(Path(python).parent.parent)])):
            path = Path(raw)
            if path.exists():
                args += ["--ro-bind", str(path.resolve()), raw]
        work = agent_ctx.dirs.work if "fs.read" in capabilities else Path(self.temporary.name)
        if "fs.read" in capabilities:
            from ..guard.control import sealed

            if sealed(work.resolve(), "fs.write", agent_ctx.dirs.root):
                self.close()
                raise HumanError("sealed", "Рабочая папка пересекается с контуром доверия", "Выбери отдельную папку данных.")
        args += ["--ro-bind", str(Path(package).resolve()), "/package", "--ro-bind", str(sandbox.CHILD), "/limits.py",
                 "--bind" if "fs.write" in capabilities or "fs.read" not in capabilities else "--ro-bind", str(work), "/work", "--chdir", "/work"]
        if "net.read" in capabilities:
            from .proxy import Proxy
            self.proxy = Proxy(agent_ctx, Path(self.temporary.name)/'p.sock', network_hosts,
                               module=Path(package).name, ctx=ctx, active=active)
            args += ['--ro-bind', str(self.proxy.path), '/proxy.sock', '--ro-bind', str(Path(__file__).with_name('network_child.py')), '/network.py']
            arguments = ['/network.py', *arguments]
        args += [python, "/limits.py", json.dumps({"limits": limits, "landlock": False}), python, *arguments]
        self.argv = args
        self.env = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/tmp", "PYTHONPATH": "/package", "PYTHONDONTWRITEBYTECODE": "1"}

    async def start(self):
        if self.proxy:
            try:
                await self.proxy.start()
            except BaseException:
                self.close()
                raise
        return self

    def close(self):
        if self.proxy:
            self.proxy.close()
        self.temporary.cleanup()
