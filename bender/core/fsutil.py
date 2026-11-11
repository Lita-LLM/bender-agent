"""Безопасная работа с файлами.

Любая запись идёт во временный файл рядом с целевым, сбрасывается на диск и
атомарно заменяет старый файл. Обрыв питания или убийство процесса оставляет
либо старую, либо новую версию, но никогда не половину.

На Windows открытый другим процессом файл (антивирус, индексатор, синхронизация
облака) нельзя заменить сразу, поэтому замена повторяется с паузами.
"""

from __future__ import annotations

import errno
import os
import secrets
import sys
import time
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
TMP_SUFFIX = ".bdtmp"
_RETRY_SECONDS = 3.0


def _retryable(exc: OSError) -> bool:
    if isinstance(exc, PermissionError):
        return True
    winerror = getattr(exc, "winerror", None)
    return winerror in (5, 32, 33) or exc.errno in (errno.EACCES, errno.EBUSY)


def _with_retry(fn, *args):
    deadline = time.monotonic() + _RETRY_SECONDS
    delay = 0.02
    while True:
        try:
            return fn(*args)
        except OSError as exc:
            if not IS_WINDOWS or not _retryable(exc) or time.monotonic() > deadline:
                raise
            time.sleep(delay)
            delay = min(delay * 2, 0.25)


def fsync_dir(path: Path) -> None:
    """Сбросить на диск запись каталога (POSIX). На Windows и некоторых ФС Android не нужно или не поддерживается."""
    if IS_WINDOWS:
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def atomic_write(path: Path | str, data: bytes, *, durable: bool = True) -> None:
    path = Path(path)
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(6)}{TMP_SUFFIX}")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, flags, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            if durable:
                os.fsync(f.fileno())
        _with_retry(os.replace, tmp, path)
    except BaseException as exc:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        if isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, getattr(errno, 'EDQUOT', -1)):
            from .errors import HumanError
            raise HumanError('disk_full', 'На диске кончилось место', 'Освободи место и повтори действие; прежний файл сохранён.', detail=str(exc)) from None
        raise
    if durable:
        fsync_dir(path.parent)


def atomic_write_text(path: Path | str, text: str, *, durable: bool = True) -> None:
    atomic_write(path, text.encode("utf-8"), durable=durable)
