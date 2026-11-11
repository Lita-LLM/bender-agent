"""Изображения через официальный ACP agy; без чтения файлов по путям из сообщений."""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os

from .cli_common import _spawn
from .errors import ProviderError


def prepare(messages):
    """Сохраняем положение вложений в стенограмме, данные берём только из блоков."""
    out, media = [], []
    total = 0
    for message in messages:
        content = message.get('content')
        if not isinstance(content, list):
            out.append(message)
            continue
        text = []
        for part in content:
            kind = part.get('type')
            if kind == 'text':
                text.append(str(part.get('text') or ''))
            elif kind == 'audio':
                raise ProviderError('format', message='Установленный agy не принимает аудио через ACP. Используй роль Уши для расшифровки.')
            elif kind == 'image':
                data, mime = part.get('data'), part.get('media_type')
                if mime not in ('image/png', 'image/jpeg', 'image/gif', 'image/webp'):
                    raise ProviderError('format', message='Для Antigravity нужна картинка PNG, JPEG, GIF или WebP.')
                if not isinstance(data, str) or len(data) > 7*1024*1024:
                    raise ProviderError('format', message='Картинка для Antigravity слишком большая; предел — 5 МБ.')
                try:
                    size = len(base64.b64decode(data, validate=True))
                except (ValueError, binascii.Error):
                    raise ProviderError('format', message='Картинка для Antigravity повреждена; отправь её заново.') from None
                total += size
                if not size or size > 5*1024*1024 or total > 20*1024*1024 or len(media) >= 20:
                    raise ProviderError('format', message='Слишком много изображений для Antigravity: до 20 картинок и 20 МБ суммарно, до 5 МБ каждая.')
                media.append({'type':'image', 'mimeType':mime, 'data':data})
                text.append('[Приложено изображение ' + str(len(media)) + ']')
        out.append({**message, 'content':'\n'.join(text)})
    return out, media


async def stream_acp(args, *, prompt, media, model, effort, cwd, env, timeout, on_event, protection=None):
    """Одна ACP-сессия: согласование возможностей, модель, plan, поток и уборка."""
    proc, job = await _spawn(args + ['acp'], protection, cwd=str(cwd), env=env,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE, limit=2*1024*1024, start_new_session=os.name != 'nt')
    seq, total = 0, 0
    sid = None
    async def write(value):
        proc.stdin.write((json.dumps(value, ensure_ascii=False)+'\n').encode('utf-8'))
        await proc.stdin.drain()
    async def discard_err():
        while await proc.stderr.read(8192):
            pass
    async def rpc(method, params):
        nonlocal seq, total
        seq += 1
        rid = seq
        await write({'jsonrpc':'2.0', 'id':rid, 'method':method, 'params':params})
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                raise ValueError('ACP закрыл поток до ответа')
            total += len(raw)
            if total > 32*1024*1024:
                raise ValueError('ACP прислал слишком много событий')
            ev = json.loads(raw)
            if ev.get('method') and 'id' in ev:
                # Встроенные тулы никогда не получают подтверждение от Бендера.
                if ev['method'] == 'session/request_permission':
                    await write({'jsonrpc':'2.0', 'id':ev['id'], 'result':{'outcome':{'outcome':'cancelled'}}})
                else:
                    await write({'jsonrpc':'2.0', 'id':ev['id'], 'error':{'code':-32601, 'message':'Метод отключён в Бендере'}})
                continue
            if ev.get('method') == 'session/update':
                params = ev.get('params') or {}
                if params.get('sessionId') != sid:
                    continue
                update = params.get('update') or {}
                if update.get('sessionUpdate') == 'agent_message_chunk':
                    content = update.get('content') or {}
                    if content.get('type') == 'text' and content.get('text'):
                        on_event({'event':'step_update','step_update':{'step_type':'agent_response','text_delta':content['text']}})
            if ev.get('id') == rid and not ev.get('method'):
                if 'error' in ev:
                    raise ProviderError('format', message='Antigravity ACP отклонил запрос: ' + str((ev['error'] or {}).get('message',''))[:300])
                return ev.get('result') or {}
    async def consume():
        nonlocal sid
        init = await rpc('initialize', {'protocolVersion':1, 'clientCapabilities':{},
                                        'clientInfo':{'name':'bender','version':'1'}})
        caps = (init.get('agentCapabilities') or {}).get('promptCapabilities') or {}
        if not caps.get('image'):
            raise ProviderError('format', message='Этот agy не поддерживает изображения через ACP; обнови CLI или назначь роль Зрение.')
        session = await rpc('session/new', {'cwd':str(cwd.resolve()), 'mcpServers':[]})
        sid = session['sessionId']
        await rpc('session/set_mode', {'sessionId':sid, 'modeId':'plan'})
        if model and model != 'default':
            await rpc('session/set_model', {'sessionId':sid, 'modelId':model})
        if effort != 'default':
            await rpc('session/set_config_option', {'sessionId':sid,'configId':'thought_level','value':effort})
        result = await rpc('session/prompt', {'sessionId':sid,'prompt':[{'type':'text','text':prompt}, *media]})
        if result.get('stopReason') not in ('end_turn', 'max_tokens'):
            raise ProviderError('refused', message='Antigravity не завершил ответ с изображением: '+str(result.get('stopReason')))
        on_event({'event':'result','result':{'status':'SUCCESS','usage':result.get('usage') or {},'stop_reason':result.get('stopReason')}})
        return 0, ''
    reader = asyncio.create_task(discard_err())
    try:
        return await asyncio.wait_for(consume(), timeout)
    finally:
        if job:
            job.close()
        if proc.returncode is None:
            from ..tools.shell import _kill
            _kill(proc)
        await proc.wait()
        reader.cancel()
        await asyncio.gather(reader, return_exceptions=True)
