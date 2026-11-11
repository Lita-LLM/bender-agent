"""Перенос Hermes и заметок Claude Code: просмотр точного снимка, выбор, добавление.

Источник не открывается на запись. Чужие инструкции становятся заметками, а не
системными правилами. Неизвестные задания сохраняются в архиве без исполнения.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import time
from pathlib import Path
from urllib.parse import urlsplit

from ..core.errors import HumanError
from ..core.pathutil import within, same, relative_path
from ..core.fsutil import atomic_write_text
from ..core.ids import id_at, new_id
from ..mcp.manager import digest
from .openclaw import Report, redact

KINDS = {"hermes": "Hermes", "claude": "Claude Code"}
DEFAULTS = {"hermes": "~/.hermes", "claude": "~/.claude"}


def error(title, hint="Выбери обычную папку источника и повтори предпросмотр."):
    return HumanError("migration", title, hint, status=409)


def source(app, kind, path=None):
    if kind not in KINDS:
        raise error("Неизвестный источник переезда")
    if not app.settings.get("migration", "enabled", True):
        raise error("Перенос выключен", "Включи его в настройках «Переезд».")
    root = Path(path or (os.environ.get("HERMES_HOME") if kind == "hermes" else "") or DEFAULTS[kind]).expanduser()
    if root.is_symlink() or not root.is_dir():
        raise error("Папка источника не найдена")
    root = root.resolve()
    from .sourceguard import check
    check(app,root)
    if app.guard.policy.protected(root) or same(root, app.dirs.root):
        raise error("Перенос из защищённой папки Бендера запрещён")
    return root


def read(app, root, relative, *, limit=2*1024*1024):
    path = root / relative_path(relative)
    if not path.exists():
        return None
    from .sourceguard import check
    check(app,path,root)
    for parent in (path, *path.parents):
        if same(parent, root):
            break
        if parent.is_symlink():
            raise error("Ссылки в источнике не переносятся")
    if not path.is_file() or not within(path, root) or app.guard.policy.protected(path.resolve()):
        raise error("Источник содержит недопустимый файл")
    with path.open("rb") as handle:
        value = handle.read(limit + 1)
    if len(value) > limit:
        raise error("Файл источника слишком большой", f"Лимит для {relative}: {limit // 1024} КБ. Выбери более узкую папку.")
    return value


def discover(app, kind, path=None):
    roots = [path] if path else [os.environ.get("HERMES_HOME") or DEFAULTS[kind]] if kind == "hermes" else [DEFAULTS[kind]]
    if kind == "hermes" and not path:
        profiles = Path("~/.hermes/profiles").expanduser()
        if profiles.is_dir():
            roots += [str(p) for p in sorted(profiles.iterdir())[:64] if p.is_dir() and not p.is_symlink()]
    found = []
    for hint in roots:
        try:
            root = source(app, kind, hint)
            found.append({"path": str(root), "version": "", "agents": [{"id": kind, "name": KINDS[kind], "default": True}]})
        except HumanError:
            if path:
                raise
    return {"installs": found}


def make_plan(app, kind, path=None):
    root = source(app, kind, path)
    key = kind + ":" + digest(str(root))[:16]
    ledger = app.store.meta_get("import:" + key + ":done", {})
    items, warnings = [], []
    budget = app.settings.get("migration", "text_mb", 16)*1024*1024
    consumed = 0

    def add(iid, category, title, payload, *, selected=True, note=""):
        nonlocal consumed
        fingerprint = digest(payload)
        consumed += len(json.dumps(payload, ensure_ascii=False).encode())
        if consumed > budget:
            raise error("Предпросмотр слишком большой", "Увеличь лимит текста переезда или выбери более узкую папку.")
        done = ledger.get(iid) == fingerprint
        items.append({"id": iid, "kind": category, "title": title, "action": "skip" if done else "create", "selected": selected,
                      "detail": app.guard.scrub(payload.get("text") or json.dumps(payload, ensure_ascii=False, indent=2)), "note": note, "payload": payload, "hash": fingerprint})

    notes = []
    if kind == "hermes":
        notes = ["SOUL.md", "memories/MEMORY.md", "memories/USER.md", "MEMORY.md", "USER.md"]
    else:
        notes = ["CLAUDE.md", ".claude/CLAUDE.md", "memory/MEMORY.md"]
        visited = 0
        projects = root / "projects"
        if projects.is_dir() and not projects.is_symlink():
            for base, dirs, files in os.walk(projects, followlinks=False):
                visited += 1
                if visited > 512:
                    raise error("Слишком много папок проектных заметок", "Выбери одну папку проекта.")
                dirs[:] = [d for d in dirs if not (Path(base)/d).is_symlink()]
                notes += [(Path(base)/f).relative_to(root).as_posix() for f in sorted(files) if f.endswith(".md")]
        memory = root / "memory"
        if memory.is_dir() and not memory.is_symlink():
            notes += [p.relative_to(root).as_posix() for p in sorted(memory.glob("*.md"))]
    for rel in dict.fromkeys(notes):
        raw = read(app, root, rel)
        if raw and raw.strip():
            add("note:" + rel, "diary", rel + " → заметка памяти", {"text": raw.decode("utf-8-sig", "replace"), "path": rel}, selected=kind == "hermes",
                note="Чужой текст: после выбора хозяином останется недоверенной заметкой; правила и ядро личности не заменяются.")

    if kind == "hermes":
        raw = read(app, root, "config.yaml")
        if raw:
            from ..skills.format import header
            try:
                config = header(raw.decode("utf-8-sig").splitlines())
                model = config.get("model", {})
                if isinstance(model, str):
                    model = {"default": model}
                if not isinstance(model, dict):
                    raise ValueError("model")
                name = model.get("default") or model.get("model") or model.get("name")
                provider = model.get("provider") or config.get("provider") or "auto"
                url = model.get("base_url") or model.get("api_base") or config.get("base_url") or config.get("api_base")
                url = url or {"openrouter": "https://openrouter.ai/api/v1", "openai": "https://api.openai.com/v1", "anthropic": "https://api.anthropic.com", "deepseek": "https://api.deepseek.com/v1", "ollama": "http://127.0.0.1:11434/v1"}.get(provider)
                if isinstance(name, str) and isinstance(url, str) and urlsplit(url).scheme in ("https", "http") and not urlsplit(url).username and not urlsplit(url).password:
                    add("model", "provider", "Модель Hermes → новый провайдер и предложение роли", {"name": str(provider), "model": name, "base_url": url, "kind": "anthropic" if provider == "anthropic" else "openai"},
                        note="Ключ добавь в «Моделях». Существующая основная роль сохранится; новая назначится только если роль пуста.")
                else:
                    warnings.append("Автовыбор/подписку или неизвестную модель Hermes подключи вручную в «Моделях».")
                add("config", "archive", "Остальные настройки Hermes → архив без ключей", {"config": redact(config)}, selected=False)
            except (HumanError, ValueError, UnicodeError):
                warnings.append("config.yaml не входит в поддержанное подмножество YAML. Модель подключи вручную; конфиг и ключи не копировались.")
        raw = read(app, root, "cron/jobs.json")
        if raw:
            try:
                document = json.loads(raw)
                jobs = document.get("jobs", []) if isinstance(document, dict) else document
                if not isinstance(jobs, list):
                    raise ValueError("jobs")
                for i, job in enumerate(jobs):
                    if not isinstance(job, dict):
                        raise ValueError("job")
                    schedule = job.get("schedule", {})
                    action = {"type": "agent", "prompt": str(job.get("prompt") or ""), "session": "isolated", "deliver": "chat"}
                    converted = None
                    if isinstance(schedule, dict) and action["prompt"] and not any(job.get(f) for f in ("script", "no_agent", "skill", "skills", "monitor_script", "monitor_url", "context_from")):
                        from ..heart.jobs import make_schedule
                        from datetime import datetime
                        try:
                            if schedule.get("kind") == "cron":
                                converted = make_schedule(cron=schedule.get("expr"))
                            elif schedule.get("kind") == "interval":
                                converted = make_schedule(every=int(float(schedule["minutes"])*60), now=1)
                            elif schedule.get("kind") == "once":
                                converted = make_schedule(at=datetime.fromisoformat(str(schedule["run_at"]).replace("Z", "+00:00")).timestamp())
                        except (HumanError, ValueError, TypeError, KeyError, OverflowError):
                            pass
                    add("cron:" + str(job.get("id", i)), "setting" if converted else "archive", "Задание " + str(job.get("name") or job.get("id") or i),
                        {"schedule": converted, "action": action, "original": redact(job)}, note="Будет выключено, без полномочий. Неподдержанные скрипты/доставка/расписания — только в архиве.")
            except (ValueError, TypeError):
                warnings.append("cron/jobs.json повреждён или неизвестного формата; задания не переносились.")
        db = root / "state.db"
        if db.exists():
            limit = app.settings.get("migration", "database_mb", 256)*1024*1024
            # Работаем только с копией файлов: SQLite не создаст shm/wal в источнике.
            with tempfile.TemporaryDirectory(prefix="bender-hermes-", dir=app.dirs.local) as folder:
                for suffix in ("", "-wal"):
                    value = read(app, root, "state.db" + suffix, limit=limit)
                    if value is not None:
                        (Path(folder)/("state.db" + suffix)).write_bytes(value)
                connection = sqlite3.connect(str(Path(folder)/"state.db"))
                connection.row_factory = sqlite3.Row
                try:
                    connection.execute("PRAGMA trusted_schema=OFF")
                    sessions = connection.execute("SELECT id FROM sessions ORDER BY id LIMIT ?", (app.settings.get("migration", "sessions", 1000)+1,)).fetchall()
                    if len(sessions) > app.settings.get("migration", "sessions", 1000):
                        raise error("Слишком много сессий Hermes", "Увеличь лимит сессий переезда.")
                    count = 0
                    for session in sessions:
                        maximum = app.settings.get("migration", "messages", 10000)
                        messages = [dict(r) for r in connection.execute("SELECT id,role,content,timestamp FROM messages WHERE session_id=? ORDER BY id LIMIT ?", (session["id"], maximum-count+1))]
                        count += len(messages)
                        if count > maximum:
                            raise error("Слишком много сообщений Hermes", "Увеличь лимит сообщений переезда.")
                        add("session:"+str(session["id"]), "session", "Разговор Hermes " + str(session["id"]), {"messages": messages, "session": str(session["id"])}, note=f"Сообщений: {len(messages)}. Вызовы тулов не исполняются.")
                except sqlite3.Error:
                    warnings.append("state.db имеет неизвестную/повреждённую схему. История не переносилась; источник сохранён.")
                finally:
                    connection.close()
        skills = root / "skills"
        if skills.is_dir() and not skills.is_symlink():
            from ..skills.manager import scan, bytes_from
            visited = 0
            for base, dirs, files in os.walk(skills, followlinks=False):
                visited += 1
                if visited > 512:
                    raise error("Слишком много папок навыков")
                dirs[:] = [d for d in dirs if not (Path(base)/d).is_symlink() and not d.startswith(".")]
                if "SKILL.md" in files:
                    paths, _ = scan(Path(base), *app.skills.limits())
                    manifest = {rel: hashlib.sha256(bytes_from(p)).hexdigest() for rel,p in paths.items()}
                    rel = Path(base).relative_to(root).as_posix()
                    add("skill:"+rel, "skill", "Навык " + Path(base).name, {"path": rel, "manifest": manifest}, note="Только карантин. Просмотр и включение — в «Навыках».")
                    dirs[:] = []
    else:
        for rel in (".mcp.json", "settings.json", ".claude.json"):
            raw = read(app, root, rel)
            if not raw:
                continue
            try:
                config = json.loads(raw)
                if isinstance(config.get("mcpServers"), dict):
                    # Переменные и ключи не копируются: их хозяин добавит в сейфе.
                    servers = {name: {k:v for k,v in value.items() if k not in ("env", "headers")} for name,value in config["mcpServers"].items() if isinstance(value, dict)}
                    add("mcp:"+rel, "setting", "MCP из " + rel, {"mcpServers": servers}, selected=False, note="Выключенные подключения; без ключей и без одобрений определений.")
            except (ValueError, AttributeError):
                warnings.append(rel + ": MCP не разобран, файл не копировался.")
    token = digest({"source": key, "items": [(i["id"], i["hash"]) for i in items]})
    return {"kind": kind, "key": key, "path": str(root), "hash": token, "items": items, "warnings": warnings}


def public(plan):
    return {"source": {"path": plan["path"], "version": ""}, "agent": {"name": KINDS[plan["kind"]]}, "hash": plan["hash"],
            "items": [{k:v for k,v in item.items() if k not in ("payload", "hash")} for item in plan["items"]], "warnings": plan["warnings"]}


def apply(app, plan, expected, only):
    if expected != plan["hash"]:
        raise error("Источник изменился после предпросмотра", "Построй новый план и просмотри изменения.")
    if not isinstance(only, list) or any(i not in {x["id"] for x in plan["items"]} for i in only):
        raise error("Нужен явный выбор пунктов плана")
    archive = app.dirs.imports / (plan["kind"] + "-" + new_id())
    archive.mkdir(parents=True, mode=0o700)
    before = archive / "before"
    before.mkdir()
    app.store.backup_to(before / "bender.db")
    shutil.copytree(app.dirs.self_dir, before / "self")
    rep = Report(dir=str(archive), warnings=list(plan["warnings"]))
    key = "import:" + plan["key"]
    ledger = app.store.meta_get(key + ":done", {})
    for item in plan["items"]:
        if item["id"] not in only or item["action"] == "skip":
            rep.skipped.append(item["title"])
            continue
        p = item["payload"]
        try:
            if item["kind"] == "diary":
                dest = app.dirs.diary / plan["kind"] / (digest(plan["key"] + item["id"] + item["hash"])[:24]+".md")
                dest.parent.mkdir(parents=True, exist_ok=True)
                if not dest.exists():
                    # Каталог помечается индексом как недоверенный целиком.
                    atomic_write_text(dest, "# Из " + KINDS[plan["kind"]] + ": " + p["path"] + "\n\n" + p["text"].replace("<!--", "&lt;!--"))
            elif item["kind"] == "session":
                skey = key + ":session:" + p["session"]
                target = app.store.meta_get(skey)
                if not target or not app.store.session(target):
                    target = app.store.new_session("imported", item["title"], meta={"imported": "hermes"})
                    app.store.meta_set(skey, target)
                seen = {m["meta"].get("source_id") for m in app.store.messages(target)}
                output = []
                for m in p["messages"]:
                    if str(m["id"]) in seen or m["role"] not in ("user", "assistant") or not m["content"]:
                        continue
                    text = m["content"] if isinstance(m["content"], str) else json.dumps(m["content"], ensure_ascii=False)
                    ts = float(m["timestamp"] or time.time())
                    output.append((id_at(int(ts*1000)), {"role": m["role"], "content": text}, ts, {"imported": "hermes", "source_id": str(m["id"]), "tainted": True}))
                rep.counts["messages"] = rep.counts.get("messages", 0) + app.store.import_messages(target, output)
                atomic_write_text(archive/(digest(item["id"])[:20]+".json"), json.dumps(p, ensure_ascii=False, indent=2))
            elif item["kind"] == "provider":
                pkey = key + ":provider"
                pid = app.store.meta_get(pkey)
                if not pid:
                    provider = app.models.add_provider(name="Из Hermes: " + p["name"], kind=p["kind"], base_url=p["base_url"])
                    pid = provider["id"]
                    app.store.meta_set(pkey, pid)
                    if not app.models.roles().get("main"):
                        app.models.set_role("main", [{"provider": pid, "model": p["model"]}])
                atomic_write_text(archive/"model.json", json.dumps(p, ensure_ascii=False, indent=2))
            elif item["kind"] == "skill":
                result = app.skills.import_path(Path(plan["path"])/p["path"], source="hermes")
                if result.get("errors"):
                    raise error("Навык не перенесён", "; ".join(result["errors"]))
            elif p.get("mcpServers") is not None:
                result = app.mcp.import_servers(p, source="claude", secrets=False)
                rep.warnings += result.get("errors", []) + result.get("warnings", [])
            elif item["kind"] == "setting" and p.get("schedule"):
                jkey = key + ":" + item["id"]
                if not app.store.meta_get(jkey):
                    job = app.heart.jobs.add(item["title"], p["schedule"], p["action"], source="hermes", enabled=False, authority=[], meta={"original": p["original"]})
                    app.store.meta_set(jkey, job.id)
            else:
                atomic_write_text(archive/(digest(item["id"])[:20]+".json"), json.dumps(p, ensure_ascii=False, indent=2))
            ledger[item["id"]] = item["hash"]
            app.store.meta_set(key + ":done", ledger)
            rep.done.append(item["title"])
        except (HumanError, ValueError, OSError, TypeError) as exc:
            rep.errors.append(item["title"] + ": " + app.guard.scrub(str(exc)))
    atomic_write_text(archive/"plan.json", json.dumps(public(plan), ensure_ascii=False, indent=2))
    atomic_write_text(archive/"report.json", json.dumps(rep.public(), ensure_ascii=False, indent=2))
    app.journal.write("import", f"Перенос {KINDS[plan['kind']]}: {len(rep.done)}, ошибок {len(rep.errors)}", actor="owner")
    return rep
