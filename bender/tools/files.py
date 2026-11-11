"""Тулы файлов: читать, писать, править, смотреть папки, искать по именам и по содержимому.

Пути — относительно рабочей папки агента (data/work), если не абсолютные. Каждое действие
проходит Предохранитель; перед перезаписью и правкой существующего файла делается снимок.
"""

from __future__ import annotations

import fnmatch
import os
import re
from pathlib import Path

from ..core.fsutil import atomic_write
from ..channels.media import untrusted
from .registry import Denied, ToolContext, ToolResult

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".cache"}
MAX_READ_BYTES = 2 * 1024 * 1024


def _rel(ctx: ToolContext, p: Path) -> str:
    try:
        return str(p.relative_to(ctx.app.dirs.work))
    except ValueError:
        return str(p)


def _open_dirs(ctx: ToolContext, base: str, names: list) -> list:
    """Подпапки для обхода: без мусора и без закрытых данных других агентов и Бендера."""
    agents = getattr(ctx.app.host, "agents", None)
    root = ctx.app.dirs.root if hasattr(ctx.app.dirs, "root") else None
    from ..guard.control import sealed
    out = []
    for name in names:
        if name in SKIP_DIRS:
            continue
        full = Path(base) / name
        if agents is not None and agents.private_path(ctx.app, full, "fs.read", ancestors=False):
            continue
        if root is not None and sealed(full, "fs.read", root):
            continue
        out.append(name)
    return out


def _is_binary(data: bytes) -> bool:
    return b"\x00" in data[:8192]


async def readable(ctx: ToolContext, path: str, verb: str = "прочитать") -> Path | None:
    """Путь файла после проверки fs.read Предохранителем (рабочая папка, симлинки, закрытые места); None — файла нет."""
    p = ctx.app.guard.policy.resolve(path)
    await ctx.check("fs.read", str(p), f"{verb} {_rel(ctx, p)}")
    return p if p.is_file() else None


def register(reg) -> None:
    @reg.tool("read_file", "Прочитать файл", group="files")
    async def read_file(ctx: ToolContext, path: str, offset: int = 1, limit: int = 2000) -> ToolResult:
        """Прочитать текст по строкам, картинку — приложить модели, аудио — Ушам или разрешённой хозяином аудиомодели. Медиа всегда недоверенные, ограничены размером/числом за ход. Вложения каналов — work/inbox/...; пересмотреть можно тем же путём. Неизвестный путь — find_files, фраза — grep. Большой текст читай частями.

        path — путь к файлу (относительный — от рабочей папки)
        offset — с какой строки читать (с 1)
        limit — сколько строк прочитать
        """
        p = await readable(ctx, path)
        if p is None:
            return ToolResult(f"Нет такого файла: {path}", ok=False, summary="нет файла")
        size = p.stat().st_size
        with p.open('rb') as source:head=source.read(4096)
        from .file_media import read as read_media
        result=await read_media(ctx,p,size,head)
        if result is not None:return result
        with p.open("rb") as fh:
            data = fh.read(MAX_READ_BYTES)
        if _is_binary(data):
            return ToolResult(f"Это двоичный файл ({size} байт) — прочитать как текст нельзя. Картинки и аудио поддерживаются отдельно; для этого формата нужен подходящий инструмент.", ok=False, summary="двоичный файл")
        text = data.decode("utf-8", "replace")
        lines = text.splitlines()
        start = max(1, int(offset))
        chunk = lines[start - 1 : start - 1 + max(1, int(limit))]
        numbered = "\n".join(f"{i:>6}\t{ln}" for i, ln in enumerate(chunk, start))
        more = ""
        if start - 1 + len(chunk) < len(lines) or size > MAX_READ_BYTES:
            more = f"\n…[показаны строки {start}–{start + len(chunk) - 1} из {len(lines)}{'+' if size > MAX_READ_BYTES else ''}]"
        return ToolResult(numbered + more if chunk else "(пусто)", tainted=untrusted(ctx.app,p), summary=f"прочитан {_rel(ctx, p)}")

    @reg.tool("write_file", "Записать файл", group="files")
    async def write_file(ctx: ToolContext, path: str, content: str) -> ToolResult:
        """Записать файл целиком (создать или перезаписать). Для небольших правок — edit_file.

        path — путь к файлу
        content — полное содержимое
        """
        p = ctx.app.guard.policy.resolve(path)
        existed = p.exists()
        await ctx.check("fs.write", str(p), f"{'перезаписать' if existed else 'создать'} {_rel(ctx, p)}", detail={"bytes": len(content.encode())})
        snap = None
        if existed and ctx.app.settings.get("guard", "checkpoints", True):
            snap = ctx.app.guard.checkpoints.take(p, "перед write_file")
        atomic_write(p, content.encode("utf-8"), durable=False)
        ctx.app.journal.write("file", f"{'Перезаписан' if existed else 'Создан'} {_rel(ctx, p)}", actor=ctx.actor, data={"path": str(p), "checkpoint": snap and snap.get("id")})
        return ToolResult(f"Записано: {_rel(ctx, p)} ({len(content.encode())} байт){' — снимок до правки сохранён' if snap else ''}.", summary=f"записан {_rel(ctx, p)}",
                          data={"checkpoint": snap})

    @reg.tool("edit_file", "Поправить файл", group="files")
    async def edit_file(ctx: ToolContext, path: str, old: str, new: str, replace_all: bool = False) -> ToolResult:
        """Точная правка прочитанного файла; целиком новый файл — write_file. old нужен ровно один раз (или replace_all=true).

        path — путь к файлу
        old — точный текст, который заменить (с отступами)
        new — чем заменить
        replace_all — заменить все вхождения
        """
        p = ctx.app.guard.policy.resolve(path)
        await ctx.check("fs.write", str(p), f"поправить {_rel(ctx, p)}")
        if not p.is_file():
            return ToolResult(f"Нет такого файла: {path}", ok=False, summary="нет файла")
        text = p.read_text(encoding="utf-8")
        n = text.count(old) if old else 0
        if n == 0:
            return ToolResult("Ошибка: такого текста в файле нет. Прочитай файл и скопируй кусок точно, с отступами.", ok=False, summary="кусок не найден")
        if n > 1 and not replace_all:
            return ToolResult(f"Ошибка: кусок встречается {n} раз. Возьми кусок длиннее (с соседними строками) или replace_all=true.", ok=False, summary="кусок не уникален")
        snap = ctx.app.guard.checkpoints.take(p, "перед edit_file") if ctx.app.settings.get("guard", "checkpoints", True) else None
        text = text.replace(old, new) if replace_all else text.replace(old, new, 1)
        atomic_write(p, text.encode("utf-8"), durable=False)
        ctx.app.journal.write("file", f"Поправлен {_rel(ctx, p)}", actor=ctx.actor, data={"path": str(p), "replaced": n if replace_all else 1, "checkpoint": snap and snap.get("id")})
        return ToolResult(f"Поправлено: {_rel(ctx, p)} (замен: {n if replace_all else 1}).", summary=f"поправлен {_rel(ctx, p)}", data={"checkpoint": snap})

    @reg.tool("list_dir", "Посмотреть папку", group="files")
    async def list_dir(ctx: ToolContext, path: str = ".") -> ToolResult:
        """Посмотреть одну папку с размерами; поиск вложенных имён — find_files, содержимого — grep.

        path — папка (по умолчанию рабочая)
        """
        p = ctx.app.guard.policy.resolve(path)
        await ctx.check("fs.read", str(p), f"посмотреть папку {_rel(ctx, p)}", detail={"listing": True})
        if not p.is_dir():
            return ToolResult(f"Нет такой папки: {path}", ok=False, summary="нет папки")
        rows = []
        for e in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))[:500]:
            try:
                rows.append(f"{e.name}/" if e.is_dir() else f"{e.name}  ({e.stat().st_size} байт)")
            except OSError:
                rows.append(e.name)
        return ToolResult("\n".join(rows) or "(пусто)", summary=f"папка {_rel(ctx, p)}")

    @reg.tool("find_files", "Найти файлы", group="files")
    async def find_files(ctx: ToolContext, pattern: str, path: str = ".") -> ToolResult:
        """Найти пути по имени (*.py, **/README*); фразу внутри файлов ищи grep.

        pattern — шаблон имени, glob
        path — где искать (по умолчанию рабочая папка)
        """
        root = ctx.app.guard.policy.resolve(path)
        await ctx.check("fs.read", str(root), f"искать файлы в {_rel(ctx, root)}", detail={"listing": True})
        found = []
        pat = pattern.split("/")[-1] if "/" in pattern else pattern
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = _open_dirs(ctx, dirpath, dirnames)
            for fn in filenames:
                full = Path(dirpath) / fn
                rel = str(full.relative_to(root))
                if fnmatch.fnmatch(fn, pat) or fnmatch.fnmatch(rel, pattern):
                    found.append(rel)
                    if len(found) >= 500:
                        break
            if len(found) >= 500:
                break
        return ToolResult("\n".join(sorted(found)) or "Ничего не найдено.", summary=f"найдено файлов: {len(found)}")

    @reg.tool("grep", "Искать в файлах", group="files")
    async def grep(ctx: ToolContext, pattern: str, path: str = ".", glob: str = "*", ignore_case: bool = False) -> ToolResult:
        """Найти фразу/регулярное выражение в текущих файлах: файл:строка:текст. Прошлые разговоры ищи recall.

        pattern — регулярное выражение
        path — файл или папка
        glob — какие файлы смотреть (например *.py)
        ignore_case — без учёта регистра
        """
        root = ctx.app.guard.policy.resolve(path)
        await ctx.check("fs.read", str(root), f"искать «{pattern[:40]}» в {_rel(ctx, root)}", detail={"listing": True})
        try:
            rx = re.compile(pattern, re.I if ignore_case else 0)
        except re.error as e:
            return ToolResult(f"Ошибка в регулярном выражении: {e}", ok=False, summary="плохой шаблон")
        files = [root] if root.is_file() else []
        if root.is_dir():
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = _open_dirs(ctx, dirpath, dirnames)
                files.extend(Path(dirpath) / f for f in filenames if fnmatch.fnmatch(f, glob))
        hits = []
        tainted=False
        skipped = 0
        for f in files:
            try:
                await ctx.check("fs.read",str(f),"прочитать файл для поиска")
            except Denied:
                skipped += 1  # закрытый файл пропускаем, остальной поиск идёт
                continue
            try:
                if f.stat().st_size > 5 * 1024 * 1024:
                    continue
                data = f.read_bytes()
            except OSError:
                continue
            if _is_binary(data):
                continue
            for i, ln in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
                if rx.search(ln):
                    rel = f.name if root.is_file() else str(f.relative_to(root))
                    tainted=tainted or untrusted(ctx.app,f)
                    hits.append(f"{rel}:{i}: {ln[:300]}")
                    if len(hits) >= 300:
                        break
            if len(hits) >= 300:
                break
        note = f"\n…[закрытых файлов пропущено: {skipped}]" if skipped else ""
        return ToolResult(("\n".join(hits) or "Ничего не найдено.") + note, tainted=tainted, summary=f"совпадений: {len(hits)}")
