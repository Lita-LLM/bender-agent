"""Сейф: ключи API и токены. Обычный JSON-файл с правами 600, без шифрования (так решено намеренно).

Значения секретов не попадают в модель: она ссылается на них по имени `{{secret:имя}}`,
а подставляет значение Предохранитель при исполнении. В пульт значения уходят только
замаскированными.
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from .fsutil import atomic_write_text
from .errors import HumanError


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return value[:4] + "…" + value[-4:]


class Secrets:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._data: dict[str, dict] | None = None
        self._stamp = None

    def _load(self) -> dict[str, dict]:
        try:
            info=self.path.stat();stamp=(info.st_mtime_ns,info.st_size,info.st_ino)
        except FileNotFoundError:stamp=None
        except OSError as exc:raise HumanError('secrets_corrupt','Не удалось проверить сейф','Проверь права файла secrets.json.',detail=str(exc)) from None
        if self._data is None or stamp!=self._stamp:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict) or any(not isinstance(item, dict) or not isinstance(item.get('value',''), str) for item in raw.values()):
                    raise ValueError('Нужен объект секретов со строковыми значениями')
                self._data = raw
                self._stamp=stamp
            except FileNotFoundError:
                self._data = {}
                self._stamp=None
            except (OSError, ValueError) as exc:
                raise HumanError('secrets_corrupt', 'Не удалось прочитать сейф', 'Файл не перезаписан. Восстанови secrets.json из целой копии; проверь права 600.', detail=str(exc)) from None
        return self._data

    def _save(self, data) -> None:
        atomic_write_text(self.path, json.dumps(data, ensure_ascii=False, indent=1))
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._data = data
        info=self.path.stat();self._stamp=(info.st_mtime_ns,info.st_size,info.st_ino)

    def get(self, name: str) -> str | None:
        with self._lock:
            item = self._load().get(name)
            return item.get("value") if isinstance(item, dict) else None

    def set(self, name: str, value: str, note: str = "", allow: dict | None = None) -> None:
        with self._lock:
            data = dict(self._load())
            item = dict(data.get(name)) if isinstance(data.get(name), dict) else {}
            item.update({"value": value, "updated_at": time.time()})
            if note:
                item["note"] = note
            if allow is not None:
                item["allow"] = allow
            item.setdefault("created_at", time.time())
            data[name] = item
            self._save(data)

    def reuse(self, name: str, value: str, note: str = '', allow: dict | None = None) -> str:
        """Общий ключ хранится один раз; существующее другое значение не затирается."""
        with self._lock:
            data=dict(self._load())
            found=name if data.get(name,{}).get('value')==value else next((k for k,v in data.items() if v.get('value')==value),None)
            if found:
                item=dict(data[found]);before=dict(item)
                if allow is not None:
                    previous=dict(item.get('allow') or {})
                    for key in ('tools','hosts'):
                        previous[key]=list(dict.fromkeys([*(previous.get(key) or []),*(allow.get(key) or [])]))
                    item['allow']=previous
                if item!=before:data[found]=item;self._save(data)
                return found
            if name in data:
                base=name;index=2
                while name in data:name=base+':import-'+str(index);index+=1
            item={'value':value,'created_at':time.time(),'updated_at':time.time()}
            if note:item['note']=note
            if allow is not None:item['allow']=allow
            data[name]=item;self._save(data)
            return name

    def delete(self, name: str) -> bool:
        with self._lock:
            data = dict(self._load())
            if name in data:
                del data[name]
                self._save(data)
                return True
            return False

    def masked(self, name: str) -> str:
        return mask(self.get(name) or "")

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._load())

    def public(self) -> list[dict]:
        with self._lock:
            return [{"name": k, "masked": mask(v.get("value", "")), "note": v.get("note", ""), "allow": v.get("allow")} for k, v in sorted(self._load().items()) if isinstance(v, dict)]

    def values(self) -> list[str]:
        """Все значения — чтобы вычищать их из вывода тулов."""
        with self._lock:
            return [v.get("value", "") for v in self._load().values() if isinstance(v, dict) and len(v.get("value", "")) >= 6]

    def allow(self, name: str) -> dict | None:
        with self._lock:
            item = self._load().get(name)
            return item.get("allow") if isinstance(item, dict) else None
