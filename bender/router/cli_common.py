"""Общее для официальных CLI: команда, help, ограниченный JSONL и уборка процесса."""
from __future__ import annotations

import asyncio
import json
import os
import re
import shlex
import shutil
import sys


async def _spawn(args,protection=None,**kwargs):
    job=None
    if sys.platform=='win32' and protection and protection.get('enabled'):
        from ..guard.windows_job import wrap
        job,args=wrap(args,protection.get('limits'))
    try:proc=await asyncio.create_subprocess_exec(*args,**kwargs)
    except BaseException:
        if job:job.close()
        raise
    if job:proc._bender_job=job
    return proc,job

_FLAGS = {}
AUTH_WORDS = ("not logged in", "please run /login", "/login", "invalid api key", "authentication", "oauth token", "unauthorized", "401", "set an auth method", "gemini_api_key", "not authenticated")
LIMIT_WORDS = ("usage limit", "rate limit", "limit reached", "limit will reset", "429", "credit balance", "out of extra usage", "resource_exhausted", "quota")


def command(provider, default="claude"):
    raw = provider.get("command") or default
    return list(raw) if isinstance(raw, list) else shlex.split(str(raw))


def classify(text):
    text = (text or "").lower()
    if any(w in text for w in AUTH_WORDS):
        return "auth"
    if any(w in text for w in LIMIT_WORDS):
        return "rate_limit"
    if "overloaded" in text or "529" in text:
        return "overloaded"
    if "prompt is too long" in text or "context" in text and "exceed" in text:
        return "context"
    return "server"


async def probe(args, timeout=15, *, env=None, cwd=None,protection=None):
    proc,job = await _spawn(args,protection, env=env, cwd=cwd, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=os.name != "nt")
    chunks, size = [], 0

    async def read():
        nonlocal size
        while True:
            chunk = await proc.stdout.read(8192)
            if not chunk:
                break
            size += len(chunk)
            if size <= 200000:
                chunks.append(chunk)
        await proc.wait()
    try:
        await asyncio.wait_for(read(), timeout)
    except asyncio.TimeoutError:
        return -1, "молчит"
    finally:
        if job:job.close()
        if proc.returncode is None:
            from ..tools.shell import _kill

            _kill(proc)
            await proc.wait()
    return proc.returncode or 0, b"".join(chunks).decode("utf-8", "replace")


async def flags(exe, *, env=None, cwd=None,protection=None):
    path = shutil.which(exe[0]) or exe[0]
    try:
        stamp = os.stat(path).st_mtime_ns
    except OSError:
        stamp = 0
    key = (tuple(exe), path, stamp)
    if key not in _FLAGS:
        code, text = await probe(exe + ["--help"], timeout=20, env=env, cwd=cwd,protection=protection)
        _FLAGS[key] = frozenset(re.findall(r"--[a-zA-Z][a-zA-Z0-9-]*", text)) if code == 0 else frozenset()
    return _FLAGS[key]


async def stream(args, *, prompt, cwd, env, timeout, on_event,protection=None):
    """Ограничиваем весь ход, stderr и JSONL; отмена убирает всю группу процессов."""
    proc,job = await _spawn(args,protection, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, cwd=str(cwd), env=env, limit=2*1024*1024, start_new_session=os.name != "nt")
    tail = bytearray()

    async def read_err():
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                break
            tail.extend(chunk)
            del tail[:-8000]

    async def consume():
        proc.stdin.write(prompt.encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
        total = 0
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            total += len(line)
            if total > 16*1024*1024:
                raise ValueError("CLI прислал слишком много событий")
            try:
                event = json.loads(line)
            except (ValueError,RecursionError):
                continue
            if isinstance(event, dict):
                on_event(event)
        await proc.wait()

    reader = asyncio.create_task(read_err())
    try:
        await asyncio.wait_for(consume(), float(timeout))
        await reader
    finally:
        if job:job.close()
        if proc.returncode is None:
            from ..tools.shell import _kill

            _kill(proc)
            await proc.wait()
        if not reader.done():
            reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
    return proc.returncode, tail.decode("utf-8", "replace")
