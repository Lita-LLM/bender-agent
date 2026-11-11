"""Запуск одобренного скрипта из проверенного снимка, отдельным процессом."""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

from ..guard import sandbox
from ..tools.registry import ToolResult
from ..tools.shell import MAX_CAPTURE, _clip, _kill
from ..tools.web import untrusted
from .manager import error, file_path, requirements


async def run(skills, name, path, args, ctx, *, timeout=120, allow_net=False):
    agent_ctx = skills.app
    item = skills.package(name, full=True)
    path = str(file_path(path))
    if not skills.active(item) or not agent_ctx.settings.get("skills", "scripts_enabled", True):
        raise error("Скрипты навыка выключены или не одобрены")
    from .experience import use

    use(skills, name, ctx.turn)
    if item["metadata"].get("disable-model-invocation") is True and ctx.turn.get("selected_skill") != name:
        raise error("Этот навык должен сначала выбрать хозяин слэш-командой")
    if path not in item["scripts"] or Path(path).suffix not in (".py", ".sh"):
        raise error("Нужен Python- или shell-скрипт из scripts/", "Например: scripts/report.py. Произвольная команда здесь не принимается.")
    missing = skills.unavailable(item)
    if missing:
        raise error("Для навыка не хватает условий", "; ".join(missing))
    if not isinstance(args, list) or len(args) > 64 or any(not isinstance(x, str) or len(x) > 4096 or "\x00" in x for x in args):
        raise error("Аргументы скрипта должны быть списком строк (не более 64)")
    info = sandbox.for_app(agent_ctx)
    ctx.tainted = True
    env = {k: v for k, v in os.environ.items() if k in {
        "PATH", "HOME", "USER", "LANG", "LC_ALL", "TMP", "TEMP", "TMPDIR", "SystemRoot", "SYSTEMROOT", "PATHEXT", "COMSPEC",
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
    required_env = requirements(item["metadata"])["env"]
    for key in required_env:
        await ctx.check("secret.use", key, f"передать переменную {key} скрипту навыка {name}")
        if key not in os.environ:
            raise error("Требуемая переменная больше не доступна", key)
        env[key] = os.environ[key]
    def scrub(text):
        text = agent_ctx.guard.scrub(text)
        for key in required_env:
            value = env.get(key)
            if value:
                text = text.replace(value, "[секрет скрыт]")
        return text
    timeout = max(1, min(int(timeout), int(agent_ctx.settings.get("skills", "timeout_seconds", 120))))
    # local/ защищён от файловых тулов агента: пока ждём хозяина, снимок нельзя подменить ими.
    with ExitStack() as cleanup:
        temporary = cleanup.enter_context(tempfile.TemporaryDirectory(prefix="skill-run-", dir=agent_ctx.dirs.local))
        stage = Path(temporary)
        for rel, content in item["data"].items():
            target = stage / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        argv = [sys.executable if Path(path).suffix == ".py" else (shutil_shell()), str(stage / path), *args]
        command = shlex.join(argv)
        await ctx.check("skill.run", f"{name}/{path}", f"запустить навык {name}: {path}", in_sandbox=sandbox.confined(info, allow_net),
                        detail={"command": command, "arguments": args, "hash": item["hash"], "sandbox": info["title"], "net": allow_net, "env": required_env})
        current = skills.package(name)
        if not skills.active(current) or current["hash"] != item["hash"] or not agent_ctx.settings.get("skills", "scripts_enabled", True):
            raise error("Навык изменился или был выключен во время одобрения", "Обнови просмотр.", "skill_changed")
        env["BENDER_SKILL_DIR"], env["BENDER_WORKSPACE"] = str(stage), str(agent_ctx.dirs.work)
        prepared = cleanup.enter_context(sandbox.prepare(command, info=info, work_dir=agent_ctx.dirs.work, allow_net=bool(allow_net), read_dirs=(stage,), temp_root=agent_ctx.dirs.local,raw_argv=argv))
        env.update(prepared.env)
        proc = await asyncio.create_subprocess_exec(*(prepared.argv or argv), cwd=str(agent_ctx.dirs.work), env=env,
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            start_new_session=os.name != "nt")
        prepared.bind(proc)
        skills.processes.add(proc)
        chunks, captured, total, timed_out = [], 0, 0, False

        async def pump():
            nonlocal captured, total
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if captured < MAX_CAPTURE:
                    kept = chunk[:MAX_CAPTURE - captured]
                    chunks.append(kept)
                    captured += len(kept)
                if ctx.progress:
                    ctx.progress(scrub(chunk.decode("utf-8", "replace")[-200:]))
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
            # На Python 3.10 отменённый wait_for иногда оставляет исключение gather непрочитанным.
            await asyncio.gather(completion, return_exceptions=True)
            skills.processes.discard(proc)
        text = scrub(b"".join(chunks).decode("utf-8", "replace"))
        agent_ctx.journal.write("skill", f"Навык {name}: {path}, код {proc.returncode}", actor=ctx.actor,
                          data={"skill": name, "script": path, "hash": item["hash"], "code": proc.returncode, "timeout": timed_out})
        header = f"[остановлено по таймауту {timeout} с]" if timed_out else f"[код выхода {proc.returncode}]"
        if total > captured:
            header += f" [показана часть из {total} байт]"
        return ToolResult(untrusted(f"скрипт навыка {name}/{path}", header + "\n" + _clip(text)),
                          ok=proc.returncode == 0 and not timed_out, tainted=True, summary=f"навык {name}: {path} → {proc.returncode}",
                          data={"code": proc.returncode, "timed_out": timed_out})


def shutil_shell():
    import shutil

    shell = shutil.which("sh")
    if shell is None:
        raise error("На этой машине нет sh", "Shell-скрипты здесь недоступны; Python-скрипты поддерживаются.")
    return shell
