"""Официальный agy: протокол рабочего agy-facade, один процесс на запрос.

Plan и инструкция не использовать встроенные тулы не являются изоляцией.
Тулы Бендера возвращаются текстом и исполняются через Предохранитель.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
from pathlib import Path

from .cli_common import classify, command, flags, probe, _spawn
from .errors import ProviderError
from .types import Result, Usage
from . import textproto

MODELS = ['default']
RISK = ('Google может ограничить аккаунт при использовании Antigravity сторонним приложением. '
        'Режим plan и просьба не вызывать встроенные инструменты не гарантируют их запрета: '
        'они могут пройти мимо Предохранителя. Подключай только если принимаешь этот риск сам.')


async def list_models(provider):
    """Список из самого agy, без выдуманных моделей и запросов к модели."""
    code, output = await probe(command(provider, 'agy') + ['models'], timeout=60)
    rows = []
    if not code:
        for line in output.splitlines():
            fields = line.split('\t', 1)
            name = fields[0].strip()
            if name.startswith(('gemini-', 'claude-', 'gpt-')) and not any(c.isspace() for c in name):
                rows.append({'id': name, 'name': fields[-1].strip(), 'subscription': True})
    if not rows:
        raise ProviderError(classify(output), provider=provider.get('name', 'Antigravity'),
                            message='agy не вернул модели; открой agy и проверь вход.')
    return rows


async def stream_facade(args, *, prompt, cwd, env, timeout, on_event, protection=None):
    """JSON-ввод; stdin открыт до result, затем убираем процесс и потомков."""
    proc, job = await _spawn(args, protection, cwd=str(cwd), env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=2*1024*1024, start_new_session=os.name != 'nt')
    tail = bytearray()
    async def read_err():
        while True:
            chunk = await proc.stderr.read(4096)
            if not chunk:
                return
            tail.extend(chunk)
            del tail[:-8000]
    async def consume():
        request = {'event': 'user', 'message': {'role': 'user', 'content': prompt}}
        proc.stdin.write((json.dumps(request, ensure_ascii=False) + '\n').encode('utf-8'))
        await proc.stdin.drain()
        total = 0
        while True:
            line = await proc.stdout.readline()
            if not line:
                return -1
            total += len(line)
            if total > 16*1024*1024:
                raise ValueError('agy прислал слишком много событий')
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict):
                on_event(ev)
                if ev.get('event') == 'result':
                    return 0
    reader = asyncio.create_task(read_err())
    try:
        code = await asyncio.wait_for(consume(), timeout)
        return code, tail.decode('utf-8', 'replace')
    finally:
        if job:
            job.close()
        if proc.returncode is None:
            from ..tools.shell import _kill
            _kill(proc)
        await proc.wait()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)


async def run_agy(router, cand, messages, tools, params, relay):
    p = cand.provider
    pname = p.get('name') or 'Antigravity'
    def failure(kind, message, **kw):
        return ProviderError(kind, provider=pname, model=cand.model, message=message, **kw)
    if not p.get('risk_accepted'):
        raise failure('format', RISK)
    exe = command(p, 'agy')
    if not exe or not shutil.which(exe[0]):
        raise failure('not_found', 'Не найден agy. Установи Antigravity CLI и войди в него.')
    bridge = getattr(router, 'cli_bridge', None)
    extra = {}
    if os.name == 'nt' and bridge is not None:
        from ..guard.windows_job import parameters
        extra['protection'] = parameters(bridge.app)
    known = await flags(exe, **extra)
    if not {'--input-format', '--output-format', '--mode'} <= known:
        raise failure('format', 'agy не поддерживает нужный stream-json; обнови CLI.')
    args = exe + ['--input-format', 'stream-json', '--output-format', 'stream-json', '-p=', '--mode', 'plan']
    if '--disable-slash-commands' in known:
        args.append('--disable-slash-commands')
    if cand.model and cand.model != 'default':
        args += ['--model', cand.model]
    from .effort import describe
    effort = describe(p, cand.model, params)['effective']
    if effort != 'default':
        if '--effort' not in known:
            raise failure('format', 'Этот agy не поддерживает выбранное усилие; обнови CLI.')
        args += ['--effort', effort]
    from .agy_media import prepare, stream_acp
    messages, media = prepare(messages)
    encoded = textproto.encode(messages, tools) if tools else messages
    prompt = ('API mode: never use built-in tools, inspect files or run commands. '
              'Return only the next assistant message. Request Bender tools as text '
              '<tool_call> JSON blocks; Bender executes them separately.\n\n')
    # Вся история: сжатием и лимитами контекста управляет Бендер.
    prompt += '\n\n'.join('[' + str(m.get('role', 'user')) + ']\n' + textproto._text(m.get('content')) for m in encoded)
    cwd = (Path(bridge.app.dirs.local) if bridge else Path.home() / '.bender-tmp') / 'antigravity'
    cwd.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith('BENDER_')}
    parts, final, usage = [], {}, {}
    hider = textproto.Hider() if tools else None
    def emit(chunk):
        if not isinstance(chunk, str):
            raise ValueError('agy прислал некорректный текст')
        parts.append(chunk)
        visible = hider.feed(chunk) if hider else chunk
        if visible:
            relay('text', visible)
    def event(ev):
        if ev.get('event') == 'result':
            final.update(ev.get('result') or {})
        elif ev.get('event') == 'step_update':
            step = ev.get('step_update') or {}
            usage.update(step.get('usage') or {})
            if step.get('step_type') == 'agent_response' and step.get('text_delta'):
                emit(step['text_delta'])
    try:
        if media:
            code, err = await stream_acp(exe, prompt=prompt, media=media, model=cand.model, effort=effort,
                cwd=cwd, env=env, timeout=float(p.get('timeout') or 600), on_event=event, **extra)
        else:
            code, err = await stream_facade(args, prompt=prompt, cwd=cwd, env=env,
                timeout=float(p.get('timeout') or 600), on_event=event, **extra)
    except asyncio.TimeoutError:
        raise failure('timeout', 'Antigravity не ответил вовремя.', partial={'actions': True}) from None
    except ProviderError as exc:
        exc.partial = {'actions': True}
        raise
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise failure('protocol', 'Сбой потока agy.', raw=type(exc).__name__, partial={'actions': True}) from None
    if code or final.get('status') != 'SUCCESS':
        error = str(final.get('error') or err or 'agy закрыл поток до результата')
        raise failure(classify(error), error[-400:], partial={'actions': True})
    if not parts and final.get('response'):
        emit(final['response'])
    if hider:
        rest = hider.flush()
        if rest:
            relay('text', rest)
    answer = ''.join(parts)
    if not answer.strip():
        raise failure('protocol', 'Antigravity вернул пустой ответ.', partial={'actions': True})
    clean, calls = textproto.decode(answer) if tools else (answer.strip(), [])
    usage.update(final.get('usage') or {})
    return Result(text=clean, tool_calls=calls, provider_id=p['id'], provider_name=pname, model=cand.model,
                  usage=Usage(input=int(usage.get('input_tokens') or 0), output=int(usage.get('output_tokens') or 0)),
                  notice='Antigravity ACP не сообщил расход токенов; нулевые счётчики не означают бесплатный запрос.' if media and not usage else None,
                  finish='tool_calls' if calls else 'length' if final.get('stop_reason') == 'max_tokens' else 'stop')
