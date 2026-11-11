"""Навыки: ограниченные пакеты файлов, карантин, одобрение снимка и раскрытие."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
from pathlib import Path

from ..core.errors import HumanError
from ..core.pathutil import relative_path, within
from ..core.fsutil import atomic_write_text
from ..tools.web import untrusted
from .format import parse

ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
INTERNAL = {".quarantine", ".origin.json"}
SKIP_DIRS = {".git", ".archive", "__pycache__", "node_modules", ".venv"}


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def error(message, hint="Посмотри пакет в разделе «Навыки».", code="skill"):
    return HumanError(code, message, hint)


def file_path(value):
    return relative_path(value)


def scan(root, max_files, max_bytes, required="SKILL.md"):
    if root.is_symlink() or not root.is_dir():
        raise error("Навык должен быть обычной папкой", "Символические ссылки не импортируются.")
    paths, stamps, total, entries = {}, [], 0, 0

    def visit(folder, depth=0):
        nonlocal total, entries
        if depth > 12:
            raise error("Слишком глубокая вложенность навыка")
        with os.scandir(folder) as items:
            for item in items:
                entries += 1
                if entries > max_files * 4:
                    raise error("Слишком много файлов и папок в навыке")
                info = item.stat(follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    raise error("В навыке найдена символическая ссылка", "Импортируй обычные файлы: " + item.name)
                if stat.S_ISDIR(info.st_mode):
                    if item.name not in SKIP_DIRS:
                        visit(Path(item.path), depth + 1)
                    continue
                if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                    raise error("В навыке есть специальный файл или жёсткая ссылка", item.name)
                rel = Path(item.path).relative_to(root).as_posix()
                if rel in INTERNAL:
                    continue
                file_path(rel)
                if not within(item.path, root):
                    raise error("Файл вышел из папки пакета")
                total += info.st_size
                if len(paths) >= max_files or total > max_bytes or info.st_size > 1024 * 1024 or (rel == required and info.st_size > 65536):
                    raise error("Пакет навыка слишком большой", "Уменьши число файлов и объём; SKILL.md — до 64 КБ, отдельный файл — до 1 МБ.")
                paths[rel] = Path(item.path)
                stamps.append((rel, info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns))

    visit(root)
    if required not in paths:
        raise error("В папке нет " + required)
    return paths, tuple(sorted(stamps))


def bytes_from(path, limit=1024 * 1024):
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise error("Файл навыка заменён ссылкой или специальным файлом")
        data = stream.read(limit + 1)
        if len(data) > limit:
            raise error("Файл навыка слишком большой")
        return data


def strings(value):
    if isinstance(value, str):
        return [s for s in re.split(r"[,\s]+", value) if s]
    return [s for s in value if isinstance(s, str)] if isinstance(value, list) else []


def requirements(meta):
    vendor = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
    oc = vendor.get("openclaw") if isinstance(vendor.get("openclaw"), dict) else {}
    req = meta.get("requires") if isinstance(meta.get("requires"), dict) else {}
    oreq = oc.get("requires") if isinstance(oc.get("requires"), dict) else {}
    return {"bins": sorted(set(strings(req.get("bins")) + strings(oreq.get("bins")))),
            "env": sorted(set(strings(req.get("env")) + strings(oreq.get("env")))),
            "tools": sorted(set(strings(meta.get("required-tools")) + strings(meta.get("requires_tools")) + strings(req.get("tools")))),
            "toolsets": strings(meta.get("requires_toolsets")),
            "platforms": strings(meta.get("platforms")) or strings(oc.get("os"))}


class Skills:
    def __init__(self, agent_ctx):
        self.app = agent_ctx
        self._cache = {}
        self.processes = set()

    def root(self, name):
        if self.app.dirs.skills.is_symlink():
            raise error("Папка навыков не должна быть символической ссылкой")
        if not isinstance(name, str) or not ID.fullmatch(name):
            raise error("Некорректное имя навыка")
        return self.app.dirs.skills / name

    def limits(self):
        return int(self.app.settings.get("skills", "max_files", 128)), int(self.app.settings.get("skills", "max_total_mb", 8)) * 1024 * 1024

    def package(self, name, *, full=False):
        root = self.root(name)
        paths, stamp = scan(root, *self.limits())
        cached = self._cache.get(name)
        if cached and cached[0] == stamp and not full:
            return cached[1]
        data = {rel: bytes_from(path, 65536 if rel == "SKILL.md" else 1024 * 1024) for rel, path in sorted(paths.items())}
        if scan(root, *self.limits())[1] != stamp:
            raise error("Файлы навыка изменились во время чтения", "Повтори просмотр.", "skill_changed")
        manifest = {rel: hashlib.sha256(value).hexdigest() for rel, value in data.items()}
        text = data["SKILL.md"].decode("utf-8-sig")
        try:
            meta, body = parse(text)
            failure = None
        except HumanError as exc:
            meta, body, failure = {"name": name, "description": ""}, "", exc.to_dict()
        warnings = []
        patterns = [(r"(?i)ignore.{0,30}(previous|instructions)|игнориру[йя].{0,30}(правил|инструкц)", "Похоже на попытку сменить инструкции"),
                    (r"(?i)https?://|\b(curl|wget|requests|urlopen)\b", "Есть обращение к сети"),
                    (r"(?i)\b(subprocess|os\.system|eval|exec|sudo)\b|rm\s+-[a-z]*r", "Есть выполнение команд или активный код"),
                    (r"(?i)api[_-]?key|password|secret|token", "Есть упоминания секретов")]
        for rel, value in data.items():
            content = value.decode("utf-8", "replace")
            for pattern, title in patterns:
                if re.search(pattern, content):
                    warnings.append(f"{rel}: {title}")
        item = {"id": name, "metadata": meta, "body": body, "skill": text, "error": failure,
                "hash": digest(manifest), "manifest": manifest, "files": [{"path": rel, "bytes": len(value)} for rel, value in data.items()],
                "scripts": [rel for rel in data if rel.startswith("scripts/")], "warnings": warnings}
        self._cache[name] = (stamp, item)
        return {**item, "data": data} if full else item

    def trust(self, name):
        return self.app.settings.group("skill_trust").get(name) or {}

    def active(self, package):
        from .experience import stats

        return not self.app.safe and bool(self.app.settings.get("skills", "enabled", True)) and not package["error"] and \
            not stats(self, package["id"]).get("archived") and package["id"] not in self.app.settings.get("skills", "disabled", []) and self.trust(package["id"]).get("hash") == package["hash"]

    def unavailable(self, package):
        req = requirements(package["metadata"])
        missing = ["нет программы " + x for x in req["bins"] if shutil.which(x) is None]
        missing += ["нет переменной " + x for x in req["env"] if not os.environ.get(x)]
        missing += ["нет тула " + x for x in req["tools"] if not self.app.tools.enabled(x)]
        groups = {t.group for t in self.app.tools.tools.values() if self.app.tools.enabled(t.name)}
        missing += ["нет набора тулов " + x for x in req["toolsets"] if x not in groups]
        platform = "android" if os.environ.get("TERMUX_VERSION") else sys.platform
        aliases = {"macos": "darwin", "windows": "win32"}
        if req["platforms"] and platform not in [aliases.get(x, x) for x in req["platforms"]]:
            missing.append("другая платформа: " + ", ".join(req["platforms"]))
        return missing

    def catalog(self):
        from .experience import stats

        out = []
        for root in sorted(self.app.dirs.skills.iterdir()):
            if root.name.startswith("."):
                continue
            try:
                item = self.package(root.name)
                approved = self.trust(root.name)
                out.append({k: item[k] for k in ("id", "metadata", "hash", "files", "scripts", "warnings", "error")} |
                           {"directory": str(root), "approved": approved.get("hash") == item["hash"], "enabled": self.active(item), "missing": self.unavailable(item),
                            "previous": approved.get("skill"), "previous_manifest": approved.get("manifest"), "stats": stats(self, root.name)})
            except (HumanError, OSError, UnicodeError) as exc:
                out.append({"id": root.name, "metadata": {}, "hash": None, "files": [], "scripts": [], "warnings": [],
                            "approved": False, "enabled": False, "missing": [], "error": {"title": "Не удалось прочитать навык", "hint": str(exc)}})
        return out

    def public(self):
        return {"enabled": self.app.settings.get("skills", "enabled", True), "safe": self.app.safe, "skills": self.catalog()}

    def approve(self, name, expected, *, by="owner"):
        item = self.package(name)
        if item["error"]:
            raise error("Некорректный SKILL.md нельзя включить", item["error"]["hint"])
        if not expected or expected != item["hash"]:
            raise error("Навык изменился после просмотра", "Обнови список и просмотри новые файлы.", "skill_changed")
        trust = self.app.settings.group("skill_trust")
        trust[name] = {**trust.get(name, {}), "hash": item["hash"], "manifest": item["manifest"], "skill": item["skill"], "origin": by,
                       "trusted_text": by == "experience" and len(item["files"]) == 1 and not item["warnings"]}
        self.app.settings.replace("skill_trust", trust)
        self.set_enabled(name, True)
        self.app.journal.write("skill", f"Навык {name}: пакет одобрен ({len(item['files'])} файлов)", actor=by)
        return self.public()

    def trusted_text(self, name):
        item = self.package(name)
        trust = self.trust(name)
        return trust.get("trusted_text", False) and len(item["files"]) == 1 and not item["warnings"] and not item["scripts"] and trust.get("hash") == item["hash"] and self.active(item)

    def catalog_tainted(self):
        return any(item["enabled"] and not self.trusted_text(item["id"]) for item in self.catalog())

    def set_enabled(self, name, enabled):
        item = self.package(name)
        if enabled and (item["error"] or self.trust(name).get("hash") != item["hash"]):
            raise error("Сначала нужно просмотреть и одобрить навык")
        disabled = set(self.app.settings.get("skills", "disabled", []))
        disabled.discard(name) if enabled else disabled.add(name)
        self.app.settings.set("skills", {"disabled": sorted(disabled)}, by="owner")

    def read(self, name, path="SKILL.md", *, offset=0, max_chars=20000, manual=False, review=False):
        item = self.package(name, full=True)
        path = str(file_path(path))
        if path not in item["data"]:
            raise error("В навыке нет такого файла", path)
        if not review:
            if not self.active(item):
                raise error("Навык выключен или находится в карантине", "Просмотри и одобри его в пульте.")
            if item["metadata"].get("disable-model-invocation") is True and not manual:
                raise error("Этот навык запускает только хозяин", f"Используй /{name} или /skill {name}.")
        try:
            text = item["data"][path].decode("utf-8-sig")
        except UnicodeError:
            raise error("Это двоичный файл", "Агенту доступны текстовые ссылки, шаблоны и скрипты.") from None
        start = max(0, int(offset))
        text = self.app.guard.scrub(text)
        fragment = text[start:start + max(1, min(int(max_chars), 24000))]
        return {"name": name, "path": path, "hash": item["hash"], "text": fragment, "total": len(text),
                "next_offset": start + len(fragment) if start + len(fragment) < len(text) else None,
                "files": item["files"] if path == "SKILL.md" else [], "metadata": item["metadata"]}

    def index(self):
        if self.app.safe or not self.app.settings.get("skills", "enabled", True):
            return ""
        lines, size = [], 0
        limit = self.app.settings.get("skills", "index_chars", 6000)
        for item in self.catalog():
            meta = item["metadata"]
            if not item["enabled"] or meta.get("disable-model-invocation") is True:
                continue
            line = json.dumps({"name": item["id"], "description": self.app.guard.scrub(meta["description"]), "missing": item["missing"]}, ensure_ascii=False)
            if size + len(line) > limit:
                break
            lines.append(line)
            size += len(line)
        if not lines:
            return ""
        return "# Навыки\nДля подходящей задачи открой SKILL.md тулом skill_read; затем читай только нужные references/ и templates/. Навык не расширяет права.\n" + untrusted("каталог навыков", "\n".join(lines))

    def command(self, text):
        tokens = text.strip().split(maxsplit=1)
        if not tokens:
            return None
        first, rest = (tokens + [""])[:2]
        cmd = first.split("@", 1)[0]
        if cmd == "/skill":
            parts = rest.strip().split(maxsplit=1)
            name, args = (parts + [""])[:2] if parts else ("", "")
            self.root(name)
        elif cmd.startswith("/") and ID.fullmatch(cmd[1:]) and self.root(cmd[1:]).is_dir():
            name, args = cmd[1:], rest
        else:
            return None
        if self.app.safe or not self.app.settings.get("skills", "enabled", True):
            raise error("Навыки выключены", "Включи их в настройках; в безопасном режиме они недоступны.")
        item = self.package(name)
        if item["metadata"].get("user-invocable") is False:
            raise error("Для этого навыка слэш-команда выключена", "Он доступен агенту через каталог.")
        return {"name": name, "args": args}

    def import_path(self, path, *, source="local"):
        path = Path(path).expanduser()
        if path.is_symlink() or not path.is_dir():
            raise error("Не найдена обычная папка навыков", str(path))
        path = path.resolve()
        protected = self.app.guard.policy.protected(path)
        if protected:
            raise error("Импорт из защищённого места запрещён", protected)
        roots = []
        if (path / "SKILL.md").is_file():
            roots.append(path)
        else:
            visited = 0
            for base, dirs, files in os.walk(path, followlinks=False):
                visited += 1
                if visited > 512:
                    raise error("Слишком много папок для поиска навыков", "Укажи более узкую папку.")
                dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not (Path(base) / d).is_symlink()]
                if "SKILL.md" in files:
                    roots.append(Path(base))
                    dirs[:] = []
                if len(roots) > 100:
                    raise error("За один импорт поддерживается не более 100 навыков")
        imported, skipped, errors = [], [], []
        for root in roots:
            try:
                paths, stamp = scan(root, *self.limits())
                for file in paths.values():
                    if self.app.guard.policy.protected(file.resolve()):
                        raise error("В навыке есть файл из защищённого места")
                data = {rel: bytes_from(file) for rel, file in paths.items()}
                meta, _ = parse(data["SKILL.md"].decode("utf-8-sig"))
                name = meta["name"]
                manifest = {rel: hashlib.sha256(value).hexdigest() for rel, value in data.items()}
                if scan(root, *self.limits())[1] != stamp:
                    raise error("Исходный навык изменился во время импорта")
                dest = self.root(name)
                if dest.exists():
                    if self.package(name)["hash"] == digest(manifest):
                        skipped.append(name)
                        continue
                    raise error(f"Навык {name} уже есть и отличается", "Импорт не заменяет файлы хозяина.")
                with tempfile.TemporaryDirectory(prefix=".import-", dir=self.app.dirs.skills) as temporary:
                    staging = Path(temporary) / name
                    staging.mkdir()
                    for rel, value in data.items():
                        target = staging / rel
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(value)
                    atomic_write_text(staging / ".quarantine", json.dumps({"from": source, "why": "нужен просмотр хозяином"}, ensure_ascii=False))
                    staging.rename(dest)
                imported.append(name)
            except (HumanError, OSError, UnicodeError) as exc:
                errors.append(f"{root.name}: {exc}")
        if not roots:
            errors.append("В указанной папке не найдены SKILL.md")
        if imported:
            self.app.journal.write("skill", f"Навыки из {source}: {', '.join(imported)} — в карантине", actor="owner")
        return {"imported": imported, "skipped": skipped, "errors": errors}

    def stop(self):
        from ..tools.shell import _kill

        for process in list(self.processes):
            _kill(process)

    async def close(self):
        import asyncio

        self.stop()
        if self.processes:
            await asyncio.gather(*(p.wait() for p in list(self.processes)), return_exceptions=True)
