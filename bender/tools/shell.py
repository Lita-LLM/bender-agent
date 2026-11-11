"""Тул команд: выполнить команду в оболочке с таймаутом, обрезкой вывода и песочницей, где она есть."""

from __future__ import annotations

import asyncio
import os
import signal
import sys

from ..guard import sandbox
from .registry import ToolContext, ToolResult

MAX_CAPTURE = 200_000


def _clip(text: str, limit: int = 30_000) -> str:
    if len(text) <= limit:
        return text
    head = text[: limit // 2]
    tail = text[-limit // 2 :]
    return head + f"\n…[пропущено {len(text) - limit} символов]…\n" + tail


async def start_command(ctx, command, cwd="", allow_net=False, argv=None):
    """Общий запуск коротких и фоновых команд; Prepared живёт до завершения процесса."""
    agent_ctx = ctx.app
    if argv is not None:
        from ..core.errors import HumanError
        import shlex
        if not isinstance(argv, list) or not argv or len(argv)>256 or any(not isinstance(a,str) or '\0' in a for a in argv) or not argv[0]:
            raise HumanError('command_argv','Неверные аргументы команды','Передай непустой список строк без нулевых символов.')
        argv=list(argv)
        command=shlex.join(argv)  # Для Предохранителя и журнала; оболочка эту строку не исполняет.
    info = sandbox.for_app(agent_ctx, owner=ctx.actor=='owner')
    workdir = agent_ctx.guard.policy.resolve(cwd) if cwd else agent_ctx.dirs.work
    if cwd:
        await ctx.check("fs.read", str(workdir), f"работать в папке {workdir}", detail={"listing": True})
    await ctx.check("exec", command, f"выполнить: {command[:200]}", in_sandbox=sandbox.confined(info, allow_net),
                    detail={"cwd": str(workdir), "sandbox": info["title"], "net": allow_net, "limits": info["limits"]})
    if ctx.tool == "process_start" and (agent_ctx.safe or not agent_ctx.settings.get("processes", "enabled", True)):
        from ..core.errors import HumanError

        raise HumanError("process_off", "Фоновые команды выключены во время одобрения", "Повтори запуск после включения.")
    if argv is None:
        expanded, used = agent_ctx.guard.expand_secrets(command, tool="run", session_id=ctx.session_id)
    else:
        import shlex
        values=[agent_ctx.guard.expand_secrets(a,tool='run',session_id=ctx.session_id) for a in argv]
        argv=[value for value,_ in values]
        used=sorted({secret for _,names in values for secret in names})
        expanded=shlex.join(argv)
    prepared = sandbox.prepare(expanded, info=info, work_dir=agent_ctx.dirs.work, cwd=workdir,
                               allow_net=allow_net, temp_root=agent_ctx.dirs.local, raw_argv=argv)
    if os.name=='nt' and argv and not info.get('windows_job'):
        prepared.argv=argv
    env = {**os.environ, **prepared.env, "PAGER": "cat", "GIT_PAGER": "cat"}
    kw = {"stdin": asyncio.subprocess.DEVNULL, "stdout": asyncio.subprocess.PIPE,
          "stderr": asyncio.subprocess.STDOUT, "cwd": str(workdir), "env": env}
    if os.name != "nt":
        kw["start_new_session"] = True
    try:
        proc = await asyncio.create_subprocess_exec(*prepared.argv, **kw) if prepared.argv else await asyncio.create_subprocess_shell(expanded, **kw)
        prepared.bind(proc)
    except BaseException:
        prepared.close()
        raise
    return proc, prepared, used, workdir, info


def register(reg) -> None:
    @reg.tool("run", "Выполнить команду", group="shell")
    async def run(ctx: ToolContext, command: str, timeout: int = 120, cwd: str = "", allow_net: bool = False, argv: list[str] = None) -> ToolResult:
        """Короткая команда с ожиданием кода выхода и stdout/stderr; долгая фоновая — process_start. Файлы читай/правь отдельными тулами.

        command — команда
        timeout — секунд до принудительной остановки (до 1800)
        cwd — рабочая папка (по умолчанию рабочая папка агента)
        allow_net — нужна ли команде сеть (в песочнице сеть по умолчанию закрыта; ssh, git с сервером, curl — true)
        argv — отдельные аргументы программы вместо command; без подстановок оболочки, пути Windows сохраняются
        """
        agent_ctx = ctx.app
        if argv is not None:
            import shlex
            # Валидацию делает start_command до проверки и запуска.
            if isinstance(argv,list) and all(isinstance(a,str) for a in argv):command=shlex.join(argv)
        proc, prepared, used, workdir, info = await start_command(ctx, command, cwd, allow_net, argv)
        timeout = max(1, min(int(timeout), 1800))
        chunks: list[bytes] = []
        total = 0

        async def pump():
            nonlocal total
            assert proc.stdout is not None
            while True:
                b = await proc.stdout.read(65536)
                if not b:
                    break
                total += len(b)
                if sum(len(c) for c in chunks) < MAX_CAPTURE:
                    chunks.append(b)
                if ctx.progress:
                    ctx.progress(b.decode("utf-8", "replace")[-200:])

        timed_out = False
        completion = asyncio.gather(pump(), proc.wait())
        try:
            await asyncio.wait_for(completion, timeout)
        except asyncio.TimeoutError:
            timed_out = True
        finally:
            if proc.returncode is None:
                _kill(proc)
                await proc.wait()
            if not completion.done():
                completion.cancel()
            await asyncio.gather(completion, return_exceptions=True)
            prepared.close()
        out = b"".join(chunks).decode("utf-8", "replace")
        low=out.lower()
        if proc.returncode and not allow_net and any(word in low for word in ('could not resolve','name resolution','network is unreachable','name or service not known','bad owner or permissions')):
            out+='\nСеть в песочнице закрыта: если команде нужен интернет или другой узел (ssh, git, curl), повтори её с allow_net=true.'
        elif proc.returncode and any(word in low for word in ('host key verification failed','permission denied (publickey','read_passphrase','could not resolve hostname')):
            out+='\nSSH работает без терминала: настрой ключ/ssh-agent, используй -o BatchMode=yes -o StrictHostKeyChecking=accept-new. Право: Стая → Права → SSH. Пароль и подтверждение узла здесь ввести нельзя.'
        code = proc.returncode
        agent_ctx.journal.write("exec", f"Команда: {command[:200]}", actor=ctx.actor,
                          data={"command": command[:2000], "code": code, "timed_out": timed_out, "cwd": str(workdir), "sandbox": info["kind"], "secrets": used})
        head = f"[код выхода {code}]" if not timed_out else f"[остановлено по таймауту {timeout} с]"
        if total > MAX_CAPTURE:
            head += f" [вывод {total} байт, показана часть]"
        return ToolResult(head + "\n" + _clip(out), ok=(code == 0 and not timed_out), summary=f"{command[:60]} → {('таймаут' if timed_out else f'код {code}')}",
                          data={"code": code, "timed_out": timed_out})


def _kill(proc) -> None:
    job=getattr(proc,'_bender_job',None)
    if job is not None:job.close()
    try:
        if sys.platform != "win32":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass
