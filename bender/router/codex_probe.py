"""Проверка наличия Codex; движок остаётся запрещён до подтверждения MCP-only."""
import re
import shutil
from . import cli_common as common

REASON = 'У Codex CLI не подтверждён способ отключить весь встроенный набор тулов. Движок выключен: действия должны проходить Предохранитель Бендера.'


async def check(provider=None):
    exe = common.command(provider or {}, 'codex')
    path = shutil.which(exe[0]) if exe else None
    result = {'found': bool(path), 'path': path, 'command': exe, 'version': None, 'safe_tools': False, 'reason': REASON}
    if path:
        code, text = await common.probe(exe + ['--version'])
        version = re.search(r'(?m)^(?:codex-cli\s+)?(\d+\.\d+\.\d+)\s*$', text)
        result['version'] = version[1] if code == 0 and version else None
    return result
