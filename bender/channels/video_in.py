"""Видео, гифка, кружок и стикер из канала → то, что модель может увидеть и услышать.

Ни Claude, ни модели OpenAI не принимают видео на вход, поэтому ролик разбирается на равномерные кадры
(картинки) и звуковую дорожку (её расшифровывает обычный путь аудиофайлов). Для этого нужен ffmpeg на
компьютере Бендера: ядро его не требует и не ставит — нет ffmpeg, модель видит обложку от Telegram.
Обычный стикер (WEBP) — картинка сама по себе; анимированный (Lottie .tgs) ffmpeg не читает — тоже обложка.
Всё полученное — недоверенные данные.
"""
from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from ..core.errors import HumanError
from ..net.http_client import NetError

KINDS = ('video', 'video_note', 'animation', 'sticker')
LABEL = {'video': 'Видео', 'video_note': 'Кружок', 'animation': 'Гифка', 'sticker': 'Стикер'}
SOUND = ('video', 'video_note')  # у гифок и видеостикеров звука нет
TIMEOUT = 60.0
WIDTH = 768
IMAGE = ('image/jpeg', 'image/png', 'image/webp', 'image/gif')


def ffmpeg() -> str | None:
    return shutil.which('ffmpeg')


async def _run(args, timeout=TIMEOUT) -> tuple[int, str]:
    """ffmpeg над присланным файлом: только локальные файлы (-protocol_whitelist), без ввода, со сроком."""
    proc = await asyncio.create_subprocess_exec(*args, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.PIPE)
    try:
        _, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, 'ffmpeg не уложился в ' + str(int(timeout)) + ' с'
    lines = [line.strip() for line in (err or b'').decode('utf-8', 'replace').splitlines() if line.strip()]
    return proc.returncode, (lines[-1][:200] if lines else '')


def _input(tool, path) -> list[str]:
    return [tool, '-v', 'error', '-nostdin', '-y', '-protocol_whitelist', 'file', '-i', str(path)]


async def frames(tool, path, folder: Path, count: int, duration: float | None) -> list[dict]:
    """Равномерные кадры по всей длине (без длительности — раз в секунду с начала)."""
    folder.mkdir(parents=True, exist_ok=True)
    rate = count / duration if duration and duration > 0 else 1.0
    code, err = await _run(_input(tool, path) + ['-vf', f"fps={rate:.6f},scale='min({WIDTH},iw)':-2", '-frames:v', str(count),
                                                 '-q:v', '4', str(folder / 'кадр-%02d.jpg')])
    made = sorted(folder.glob('кадр-*.jpg'))
    if not made:
        raise HumanError('video_frames', 'Кадры не получились' + (': ' + err if err else ''), 'Файл сохранён; можно разобрать его вручную.')
    return [{'path': str(p), 'mime': 'image/jpeg', 'at': round(i / rate, 1)} for i, p in enumerate(made)]


async def sound(tool, path, out: Path, limit: int) -> bytes | None:
    """Звуковая дорожка в WAV 16 кГц моно, не длиннее limit секунд; None — звука нет."""
    code, _ = await _run(_input(tool, path) + ['-vn', '-ac', '1', '-ar', '16000', '-t', str(int(limit)), '-f', 'wav', str(out)])
    try:
        raw = out.read_bytes() if code == 0 and out.is_file() else b''
    finally:
        out.unlink(missing_ok=True)
    return raw if len(raw) > 44 else None


async def look(telegram, ctx, item: dict, sid: str) -> dict:
    """Разобрать вложение: {'item', 'text', 'extra' (доп. вложения — звук для модели), 'notice'}."""
    meta = item.pop('_tg', None) or {}
    kind = item['kind']
    seconds = meta.get('duration')
    head = LABEL[kind]
    if meta.get('emoji'): head += ' ' + str(meta['emoji'])
    if meta.get('set_name'): head += ', набор «' + str(meta['set_name']) + '»'
    if seconds: head += f', {int(seconds) // 60}:{int(seconds) % 60:02d}'
    lines, extra, notes, shots = [], [], [], []
    tool = ffmpeg()
    count = int(ctx.settings.get('attachments', 'video_frames', 6) or 0)
    animated = kind == 'sticker' and meta.get('is_animated')
    if kind == 'sticker' and not meta.get('is_animated') and not meta.get('is_video') and item.get('mime') in IMAGE:
        shots = [{'path': item['path'], 'mime': item['mime'], 'at': None}]
    elif count and tool and not animated:
        try:
            shots = await frames(tool, item['path'], Path(item['path'] + '.кадры'), count, float(seconds) if seconds else None)
            head += ', кадров: ' + str(len(shots))
        except HumanError as error:
            notes.append(error.title)
    if kind in SOUND and tool:
        try:
            text, native = await _listen(ctx, tool, item, sid)
            if text: lines.append(text)
            if native: extra.append(native)
        except (HumanError, NetError, ValueError) as error:
            lines.append('[Звук не расшифрован: ' + str(error) + ']')
    if not shots and meta.get('thumbnail'):
        try:
            cover = await telegram._download(ctx, sid, 'photo', meta['thumbnail'])
            shots = [{'path': cover['path'], 'mime': cover['mime'], 'at': None, 'cover': True}]
            head += ', видна только обложка'
        except (HumanError, NetError) as error:
            notes.append('обложка не скачалась: ' + str(error))
    notice = ''
    if not tool and kind != 'sticker' or animated and not shots:
        why = 'анимированный стикер ffmpeg не читает' if animated else 'на компьютере Бендера нет ffmpeg'
        notes.append(why + ('; видна только обложка' if shots else '') + ('' if kind not in SOUND or tool else '; звук не расшифрован'))
        if not animated:
            notice = LABEL[kind] + ': ' + notes[-1] + '. Поставь ffmpeg (например, apt install ffmpeg), и агент увидит кадры и услышит звук.'
    elif kind in SOUND and not shots and not lines:
        notice = LABEL[kind] + ' не разобран' + (': ' + '; '.join(notes) if notes else '') + '.'
    if shots: item['frames'] = shots
    text = '[' + head + ', недоверенные данные' + ('; ' + '; '.join(notes) if notes else '') + ']'
    return {'item': item, 'text': '\n'.join([text] + lines) + '\nФайл: ' + item['path'], 'extra': extra, 'notice': notice}


async def _listen(ctx, tool, item, sid):
    from . import media
    from ..voice.files import prepare
    if not ctx.settings.get('voice', 'enabled', False):
        return '[Звук не расшифрован: голос выключен (пульт → Настройки → Голос)]', None
    cfg = ctx.voice.require('stt')
    raw = await sound(tool, item['path'], Path(item['path'] + '.звук.wav'), min(int(cfg['max_seconds']), 1800))
    if raw is None:
        return '', None
    if len(raw) > cfg['max_mb'] * 1024 * 1024:
        raise HumanError('audio_file', 'Звуковая дорожка больше лимита голоса', 'Сократи видео; исходный файл сохранён.')
    track = await media.save(ctx, Path(item['name']).stem + '-звук.wav', raw, kind='audio', source='telegram', session=sid)
    result = await prepare(ctx, track, session=sid)
    native = result['item'] if result['item'].get('native_audio') else None
    return result['text'].replace('[Аудиофайл', '[Звук ролика', 1), native
