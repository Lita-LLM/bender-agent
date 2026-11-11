"""Ссылка хозяина и кука пульта; localhost тоже требует авторизацию."""
from pathlib import Path
import os
import secrets

from .errors import HumanError
from .fsutil import atomic_write_text


def token(data, *, create=False):
    path = Path(data) / 'local' / 'web_token'
    try:
        value = path.read_text().strip()
        if len(value) >= 24:
            return value
    except OSError:
        pass
    if not create:
        raise HumanError('web_token', 'Нет токена пульта', 'Сначала запусти bender с этой папкой данных.')
    value = secrets.token_urlsafe(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, value)
    os.chmod(path, 0o600)
    return value


def headers(data):
    return {'Cookie': 'bender_token=' + token(data), 'X-Bender': '1'}
