"""Единая граница путей: реальные имена, особенности тома и переносимые пакеты."""
from __future__ import annotations

import ntpath
import os
from pathlib import Path, PurePosixPath
import re

from .errors import HumanError

_CASE = {}
_UNICODE = {}
_RESERVED = re.compile(r"^(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?$", re.I)


def _probe_file(prefix, folder):
    """Один отказ ACL не превращается в тысячи повторов tempfile на Windows."""
    for _ in range(4):
        name=Path(folder)/(prefix+os.urandom(8).hex())
        try:
            return os.open(name,os.O_RDWR|os.O_CREAT|os.O_EXCL,0o600),str(name)
        except FileExistsError:
            continue
    raise FileExistsError('Не удалось выбрать имя пробного файла')


def safe_name(raw, *, portable=False):
    """Не допускаем неоднозначные имена Win32 даже в пакете, собранном на Linux."""
    raw = os.fspath(raw)
    drive, tail = ntpath.splitdrive(raw)
    bad = not raw or "\0" in raw or raw.startswith(("\\\\", "//"))
    if drive and (portable or os.name != "nt" or len(drive) != 2 or drive[1] != ":"):
        bad = True
    for part in tail.replace("\\", "/").split("/"):
        if part in ("", ".", "..") and not portable:
            continue
        if (part in ("", ".", "..") or part.endswith((".", " ")) or _RESERVED.fullmatch(part)
                or any(ord(c) < 32 or c in '<>:"|?*' for c in part)):
            bad = True
    if portable and (drive or raw.startswith("/") or "\\" in raw):
        bad = True
    if bad:
        raise HumanError("path_alias", "Неоднозначный или опасный путь", "Используй обычное имя без потоков NTFS, устройств, завершающих точек и пробелов.", detail=raw)
    return raw


def relative_path(raw):
    return PurePosixPath(safe_name(raw, portable=True))


def canonical(path):
    """realpath раскрывает ссылки и 8.3; отсутствующий хвост — через существующего родителя."""
    safe_name(path)
    return Path(os.path.realpath(os.path.expanduser(os.fspath(path))))


def case_sensitive(path):
    """Проверяем реальный том один раз, не угадываем по названию ОС."""
    folder = canonical(path)
    while not folder.is_dir() and folder != folder.parent:
        folder = folder.parent
    volume = folder
    try:
        dev = folder.stat().st_dev
        while volume.parent != volume and volume.parent.stat().st_dev == dev:
            volume = volume.parent
        key = (dev, str(volume))
    except OSError:
        return False  # неизвестная ФС: защитные сравнения строже
    if key in _CASE:
        return _CASE[key]
    probe = folder
    while True:
        try:
            fd, name = _probe_file('.bender-case-a',probe)
            os.close(fd)
            try:
                alternate = str(Path(name).with_name(Path(name).name.upper()))
                sensitive = not os.path.exists(alternate) or not os.path.samefile(name, alternate)
            finally:
                os.unlink(name)
            _CASE[key] = sensitive
            return sensitive
        except OSError:
            if probe == volume:
                _CASE[key] = False
                return False
            probe = probe.parent


def unicode_equivalent(path):
    """Некоторые тома APFS считают NFC/NFD одним именем даже с разным регистром."""
    import unicodedata

    folder = canonical(path)
    while not folder.is_dir() and folder != folder.parent:
        folder = folder.parent
    key = folder.stat().st_dev
    if key not in _UNICODE:
        try:
            fd, name = _probe_file('.bender-é-',folder)
            os.close(fd)
            try:
                alternate = unicodedata.normalize("NFD", name)
                _UNICODE[key] = os.path.exists(alternate) and os.path.samefile(name, alternate)
            finally:
                os.unlink(name)
        except OSError:
            _UNICODE[key] = True
    return _UNICODE[key]


def _key(path, sensitive, normalize=False):
    text = os.fspath(path)
    if normalize and not text.isascii():
        import unicodedata

        text = unicodedata.normalize("NFC", text)
    if not sensitive:
        text = os.path.normcase(text).casefold()
    return Path(text).parts


def within(path, root):
    """Равенство или вложенность с учётом реального тома; не строковый префикс."""
    path, root = canonical(path), canonical(root)
    try:
        if os.path.samefile(path, root):
            return True
    except OSError:
        pass
    sensitive = case_sensitive(root)
    normalize = (not str(path).isascii() or not str(root).isascii()) and unicode_equivalent(root)
    p, r = _key(path, sensitive, normalize), _key(root, sensitive, normalize)
    return p[:len(r)] == r


def same(path, root):
    return within(path, root) and within(root, path)
