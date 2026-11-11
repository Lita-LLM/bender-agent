"""Параметры собственного SVG-лица рядом с IDENTITY; чужой код сюда не попадает."""
import json
import re
from ..core.errors import HumanError
from ..core.fsutil import atomic_write_text

DEFAULT = {'color': '#ffb840', 'shape': 'visor', 'eyes': 'round'}


def validate(value):
    if not isinstance(value, dict) or any(k not in DEFAULT for k in value):
        raise HumanError('avatar', 'Нужны параметры color, shape, eyes', 'Цвет #RRGGBB; shape visor/round/square, eyes round/slit/square.')
    result = {**DEFAULT, **value}
    if (not isinstance(result['color'], str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', result['color']) or
            result['shape'] not in ('visor', 'round', 'square') or result['eyes'] not in ('round', 'slit', 'square')):
        raise HumanError('avatar', 'Неверные параметры лица', 'Цвет #RRGGBB; shape visor/round/square, eyes round/slit/square.')
    return result


def read(root):
    path = root / 'AVATAR.json'
    try:
        if path.stat().st_size > 4096:
            return dict(DEFAULT)
        raw = json.loads(path.read_text(encoding='utf-8'))
        # Незнакомые поля будущей версии остаются в файле.
        return validate({k:v for k,v in raw.items() if k in DEFAULT})
    except (OSError, ValueError, HumanError, AttributeError):
        return dict(DEFAULT)


def write(root, changes):
    value = validate(changes)
    path = root / 'AVATAR.json'
    try:
        original = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        original = {}
    if not isinstance(original, dict):
        original = {}
    atomic_write_text(path, json.dumps({**original, **value}, ensure_ascii=False, indent=2))
    return value
