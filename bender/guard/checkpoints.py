"""Снимки перед разрушающими правками: копия файла до изменения, откат одной кнопкой.

Снимок — копия файла в data/checkpoints/<ГГГГ-ММ-ДД>/<id>/ и запись в manifest.json рядом.
Файлы больше 10 МБ не копируются (правка всё равно разрешается, но откатить её нельзя —
об этом говорится честно). Старше 7 дней — удаляются при уборке.
"""

from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

from ..core.fsutil import atomic_write_text
from ..core.ids import new_id

MAX_FILE = 10 * 1024 * 1024


class Checkpoints:
    def __init__(self, root: Path):
        self.root = Path(root)

    def take(self, path: Path, reason: str) -> dict | None:
        """Снять копию существующего файла. None — файла нет (нечего сохранять)."""
        path = Path(path)
        if not path.is_file():
            return None
        size = path.stat().st_size
        cid = new_id()
        day = time.strftime("%Y-%m-%d")
        entry = {"id": cid, "path": str(path), "reason": reason, "ts": time.time(), "size": size}
        if size > MAX_FILE:
            entry["skipped"] = "файл больше 10 МБ — снимок не сделан"
            return entry
        dest = self.root / day / cid
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, dest / "file")
        atomic_write_text(dest / "manifest.json", json.dumps(entry, ensure_ascii=False), durable=False)
        entry["copy"] = str(dest / "file")
        return entry

    def list(self, limit: int = 100) -> list[dict]:
        out = []
        if not self.root.exists():
            return out
        for m in sorted(self.root.glob("*/*/manifest.json"), reverse=True)[:limit]:
            try:
                e = json.loads(m.read_text(encoding="utf-8"))
                e["copy"] = str(m.parent / "file")
                out.append(e)
            except (OSError, ValueError):
                continue
        return out

    def restore(self, cid: str) -> dict:
        for m in self.root.glob(f"*/{cid}/manifest.json"):
            e = json.loads(m.read_text(encoding="utf-8"))
            target = Path(e["path"])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(m.parent / "file", target)
            return e
        raise FileNotFoundError(cid)

    def prune(self, keep_days: float = 7.0) -> int:
        removed = 0
        if not self.root.exists():
            return 0
        cutoff = time.time() - keep_days * 86400
        for day in self.root.iterdir():
            try:
                if day.is_dir() and time.mktime(time.strptime(day.name, "%Y-%m-%d")) < cutoff:
                    shutil.rmtree(day, ignore_errors=True)
                    removed += 1
            except ValueError:
                continue
        return removed
