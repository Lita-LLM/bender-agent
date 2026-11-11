"""Контур доверия меняется руками в исходниках, а не карточкой разговора."""
import os
from pathlib import Path
from ..core.pathutil import within


CONTROL_PATHS = ("bender", "exams", "tests", ".git", "install.sh", "bender.sh", "bender.bat")


def updates_command(action):
    """Ловим явный обход обновления; произвольный код дополнительно ограничивает ОС."""
    if action.actor == 'owner' or action.capability != 'exec': return False
    import re
    import shlex
    from ..core.pathutil import same, canonical
    text = action.target.replace('"', '').replace("'", '')
    if re.search(r'\bbender\b[^\n;&|]*\bupdate\b', text): return True
    if not re.search(r'\bgit\b[^\n]*(?:\bpull\b|\bmerge\b|\breset\b|\brebase\b|\bcheckout\b|\bswitch\b|\bupdate-ref\b)', text): return False
    cwd = action.detail.get('cwd')
    try:
        args = shlex.split(action.target)
        if '-C' in args: cwd = Path(cwd or Path.cwd()) / args[args.index('-C') + 1]
        if cwd:
            start = path = canonical(cwd)
            while not (path / '.git').exists() and path.parent != path: path = path.parent
            if not (path / '.git').exists(): path = start  # программа без .git: сверяем саму папку
            return any(same(path, root) for root in source_roots())
    except (ValueError, IndexError): pass
    return any(str(root) in text for root in source_roots())


def source_roots():
    roots = {Path(__file__).resolve().parents[2]}
    if os.environ.get("BENDER_TRUST_ROOT"):
        roots.add(Path(os.environ["BENDER_TRUST_ROOT"]).resolve())
    return roots


def protected_change(relative):
    path = Path(relative)
    return any(within(path, Path(name)) for name in CONTROL_PATHS)


def sealed(path, capability, data_dir):
    """Проверяется раньше профиля, разового одобрения и правил хозяина."""
    if not capability.startswith("fs."):
        return False
    writing = capability in ("fs.write", "fs.delete")
    for root in source_roots():
        for name in CONTROL_PATHS:
            target = root / name
            if (writing or name == "exams") and (within(path, target) or (writing and within(target, path))):
                return True
    for name in ("exams", "workshop", "versions", "local", "bender.db", "bender.db-wal", "bender.db-shm"):
        target = Path(data_dir) / name
        if within(path, target) or (writing and within(target, path)):
            return True
    return False
