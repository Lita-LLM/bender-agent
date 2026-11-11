"""Промежуточный процесс: ограничения до exec, без preexec_fn и импорта приложения."""
from __future__ import annotations

import json
import os
import sys


def landlock_abi():
    if not sys.platform.startswith("linux"):
        return 0
    if os.uname().machine.lower() not in {"x86_64", "amd64", "aarch64", "arm64", "riscv64", "i386", "i686", "armv7l", "armv6l"}:
        return 0  # На другой архитектуре номера системных вызовов могут отличаться.
    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    result = libc.syscall(444, 0, 0, 1)
    return int(result) if result >= 0 else 0


def landlock(directories, files=()):
    """Оставляем чтение/исполнение; запись только в перечисленные деревья и устройства."""
    import ctypes

    abi = landlock_abi()
    if not abi:
        raise OSError("Landlock недоступен")
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long

    class Ruleset(ctypes.Structure):
        _fields_ = [("handled_access_fs", ctypes.c_uint64)]

    class Beneath(ctypes.Structure):
        _pack_ = 1
        _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]

    rights = (1 << 1) | sum(1 << n for n in range(4, 13))
    if abi >= 2:
        rights |= 1 << 13  # REFER
    if abi >= 3:
        rights |= 1 << 14  # TRUNCATE
    rules = Ruleset(rights)

    def check(value):
        if value < 0:
            number = ctypes.get_errno()
            raise OSError(number, os.strerror(number))
        return value

    fd = check(libc.syscall(444, ctypes.byref(rules), ctypes.sizeof(rules), 0))
    try:
        for path, directory in [(p, True) for p in directories] + [(p, False) for p in files]:
            pfd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            try:
                access = rights if directory else ((1 << 1) | ((1 << 14) if abi >= 3 else 0))
                rule = Beneath(access, pfd)
                check(libc.syscall(445, fd, 1, ctypes.byref(rule), 0))
            finally:
                os.close(pfd)
        check(libc.prctl(38, 1, 0, 0, 0))  # PR_SET_NO_NEW_PRIVS
        check(libc.syscall(446, fd, 0))
    finally:
        os.close(fd)


def user_tasks():
    """Сколько потоков уже у пользователя: ядро считает RLIMIT_NPROC по ним всем, а не по команде."""
    uid, total = str(os.getuid()), 0
    try:
        names = os.listdir("/proc")
    except OSError:
        return None
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/status", encoding="ascii", errors="replace") as handle:
                fields = dict(line.split(":", 1) for line in handle if ":" in line)
        except OSError:
            continue  # процесс уже завершился
        if fields.get("Uid", "").split()[:1] == [uid]:
            total += int(fields.get("Threads", "1").strip() or 1)
    return total or None


def resource_limits(values, relative=False):
    if os.name != "posix":
        return
    import resource

    for name, amount in values.items():
        kind = getattr(resource, name, None)
        if kind is None or amount <= 0:
            continue
        if name == "RLIMIT_NPROC" and relative:
            # Без своего пространства имён считаются все процессы хозяина (браузер, оболочка).
            # Лимит «128» тогда запрещал бы любой fork; даём столько сверх уже запущенных.
            running = user_tasks()
            if running is None:
                continue
            amount += running
        _, hard = resource.getrlimit(kind)
        ceiling = int(amount) if hard == resource.RLIM_INFINITY else min(int(amount), hard)
        resource.setrlimit(kind, (ceiling, ceiling))


def main():
    if sys.argv[1:] == ["--probe-landlock"]:
        landlock([], ["/dev/null"])
        return
    config = json.loads(sys.argv[1])
    if config.get('windows_job'):
        # Обёртка изолирует Python startup от PYTHONPATH и присоединяется до любого целевого кода.
        import runpy
        import subprocess
        helper=runpy.run_path(os.path.join(os.path.dirname(__file__),'windows_job.py'))
        helper['join'](config['windows_job'])
        if config.get('windows_shell'):raise SystemExit(subprocess.call(sys.argv[2],shell=True))
        raise SystemExit(subprocess.call(sys.argv[2:]))
    resource_limits(config["limits"], config.get("nproc_relative", False))
    if config["landlock"]:
        landlock(config["write"], ["/dev/null"])
    os.execvpe(sys.argv[2], sys.argv[2:], os.environ)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as exc:
        print("Бендер: защита дочернего процесса не установлена: " + str(exc), file=sys.stderr)
        sys.exit(126)
