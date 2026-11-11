"""Навыки из опыта, версии и куратор. Только работа по событию."""
from __future__ import annotations

import difflib
import json
from pathlib import Path
import tempfile
import time

from ..core.ids import new_id
from ..core.fsutil import atomic_write_text
from ..mind.review import SECRET
from .manager import bytes_from, digest, error, file_path, scan


def enabled(agent_ctx, key="experience"):
    return not agent_ctx.safe and agent_ctx.settings.get("workshop", "enabled", True) and agent_ctx.settings.get("workshop", key, True) and agent_ctx.settings.get("skills", "enabled", True)


def stats(skills, name):
    return dict(skills.app.store.meta_get("skills.stats", {}).get(name, {}))


def update(skills, name, **changes):
    all_stats = skills.app.store.meta_get("skills.stats", {})
    all_stats[name] = {**all_stats.get(name, {"created": time.time(), "uses": 0, "helped": 0, "failed": 0}), **changes}
    skills.app.store.meta_set("skills.stats", all_stats)
    return all_stats[name]


def use(skills, name, state):
    if not skills.app.settings.get("workshop", "enabled", True):
        return
    used = state.setdefault("used_skills", [])
    if name in used:
        return
    used.append(name)
    old = stats(skills, name)
    update(skills, name, uses=old.get("uses", 0) + 1, last_used=time.time(), stale=False)


def outcome(skills, state, success):
    if not skills.app.settings.get("workshop", "enabled", True):
        return
    for name in state.get("used_skills", []):
        old = stats(skills, name)
        key = "helped" if success else "failed"
        update(skills, name, **{key: old.get(key, 0) + 1})


def history_root(skills, name):
    skills.root(name)
    return skills.app.dirs.local / "skill-history" / name


def archive_version(skills, name):
    item = skills.package(name, full=True)
    root = history_root(skills, name)
    root.mkdir(parents=True, exist_ok=True)
    ident = "r-" + new_id().lower()
    target = root / ident
    target.mkdir()
    for rel, raw in item["data"].items():
        path = target / "files" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    record = {"id": ident, "at": time.time(), "hash": item["hash"], "approved": skills.trust(name).get("hash") == item["hash"],
              "manifest": item["manifest"], "skill": item["skill"]}
    atomic_write_text(target / "version.json", json.dumps(record, ensure_ascii=False))
    return record


def history(skills, name):
    root = history_root(skills, name)
    current = skills.package(name)["skill"]
    rows = []
    if root.exists():
        for path in sorted(root.glob("r-*/version.json"), reverse=True):
            row = json.loads(path.read_text(encoding="utf-8"))
            row["diff"] = "".join(difflib.unified_diff(row["skill"].splitlines(True), current.splitlines(True), fromfile=row["id"], tofile="текущий"))
            rows.append(row)
    return rows


def replace(skills, name, data):
    """Меняем весь пакет атомарным переименованием; старый уже сохранён версией."""
    dest = skills.root(name)
    with tempfile.TemporaryDirectory(prefix=".revision-", dir=skills.app.dirs.skills) as temporary:
        staging = Path(temporary) / "new"
        staging.mkdir()
        for rel, raw in data.items():
            path = staging / str(file_path(rel))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(raw)
        scan(staging, *skills.limits())
        old = Path(temporary) / "old"
        if dest.exists():
            dest.rename(old)
        try:
            staging.rename(dest)
        except OSError:
            if old.exists():
                old.rename(dest)
            raise
    skills._cache.pop(name, None)


async def learn(skills, spec, ctx, *, draft=False):
    if not enabled(skills.app):
        raise error("Навыки из опыта выключены", "Хозяин может включить их в настройках Мастерской.")
    if not isinstance(spec, dict):
        raise error("Предложение навыка должно быть объектом")
    name = spec.get("name", "")
    skills.root(name)
    description, body = spec.get("description", ""), spec.get("body", "")
    if not isinstance(description, str) or not description.strip() or len(description) > 500 or not isinstance(body, str) or not 20 <= len(body) <= 20000:
        raise error("Нужны описание и процедура навыка", "Описание — до 500 знаков; процедура — от 20 до 20000.")
    files = spec.get("files") or {}
    if not isinstance(files, dict) or len(files) > 32 or "SKILL.md" in files:
        raise error("Дополнительные файлы должны быть словарём без SKILL.md, до 32 файлов")
    data = {}
    for rel, text in files.items():
        file_path(rel)
        if not isinstance(text, str) or len(text.encode()) > 1024 * 1024:
            raise error("Файл навыка слишком большой или не является текстом")
        data[rel] = skills.app.guard.scrub(text).encode()
    text = "---\nname: " + name + "\ndescription: " + json.dumps(description, ensure_ascii=False) + "\n---\n\n" + body.strip() + "\n"
    if SECRET.search(text) or any(SECRET.search(raw.decode()) for raw in data.values()):
        raise error("В предложении навыка найден возможный секрет", "Убери ключи и пароли; они хранятся только в сейфе.")
    data["SKILL.md"] = skills.app.guard.scrub(text).encode()
    if sum(map(len, data.values())) > skills.limits()[1]:
        raise error("Предложение навыка превышает лимит пакета")
    await ctx.check("skill.learn", name, "сохранить проверяемый текст навыка или черновик в карантине")
    if skills.root(name).exists():
        if skills.package(name, full=True)["data"] == data:
            return {"name": name, "enabled": skills.active(skills.package(name)), "unchanged": True}
        archive_version(skills, name)
    replace(skills, name, data)
    item = skills.package(name)
    auto = not (draft or ctx.tainted or (ctx.unattended and ctx.actor != "sleep") or item["warnings"] or item["scripts"] or len(item["files"]) != 1 or item["error"])
    if auto:
        skills.approve(name, item["hash"], by="experience")
    update(skills, name, source="experience", archived=False, stale=False)
    skills.app.journal.write("skill", f"Навык из опыта {name}: " + ("включён" if auto else "черновик в карантине"), actor=ctx.actor,
                             data={"name": name, "auto": auto, "tainted": ctx.tainted})
    return {"name": name, "enabled": auto, "hash": item["hash"]}


async def edit(skills, name, text, expected, ctx):
    await ctx.check("skill.learn", name, "сохранить новую версию навыка для повторного просмотра")
    item = skills.package(name, full=True)
    if item["hash"] != expected or not isinstance(text, str) or len(text.encode()) > 65536:
        raise error("Навык изменился после просмотра или текст слишком большой", "Обнови просмотр и повтори сохранение.")
    if item["skill"] == text:
        return skills.public()
    from .format import parse
    if parse(text)[0]["name"] != name:
        raise error("Редактор не переименовывает навык", "Сохрани прежнее поле name.")
    archive_version(skills, name)
    replace(skills, name, {**item["data"], "SKILL.md": text.encode()})
    # Даже возврат к тексту прежнего доверенного хэша требует нового просмотра.
    trust = skills.app.settings.group("skill_trust")
    trust[name] = {**trust.get(name, {}), "hash": None}
    skills.app.settings.replace("skill_trust", trust)
    skills.app.journal.write("skill", f"Новая версия навыка {name}: ждёт просмотра", actor=ctx.actor)
    return skills.public()


async def rollback(skills, name, ident, expected, ctx):
    await ctx.check("skill.learn", name, "вернуть просмотренную прежнюю версию навыка")
    if skills.package(name)["hash"] != expected or not isinstance(ident, str) or not ident.startswith("r-") or not ident[2:].isalnum():
        raise error("Версия навыка изменилась или указан неверный архив")
    path = history_root(skills, name) / ident
    record = json.loads((path / "version.json").read_text(encoding="utf-8"))
    files, _ = scan(path / "files", *skills.limits())
    data = {rel: bytes_from(file) for rel, file in files.items()}
    import hashlib
    if digest({rel: hashlib.sha256(raw).hexdigest() for rel, raw in data.items()}) != record["hash"]:
        raise error("Архив навыка повреждён", "Не восстанавливай изменённые файлы архива.")
    archive_version(skills, name)
    replace(skills, name, data)
    if record.get("approved"):
        skills.approve(name, record["hash"])
    else:
        trust = skills.app.settings.group("skill_trust")
        trust[name] = {**trust.get(name, {}), "hash": None}
        skills.app.settings.replace("skill_trust", trust)
    update(skills, name, archived=False, stale=False)
    skills.app.journal.write("skill", f"Откат навыка {name} на {ident}", actor=ctx.actor)
    return skills.public()


def curate(skills, *, now=None):
    if not enabled(skills.app, "curator"):
        return {"disabled": True}
    now = time.time() if now is None else now
    protected = "\n".join(json.dumps(job.action, ensure_ascii=False) for job in skills.app.heart.jobs.all())
    result = {"stale": [], "archived": []}
    for item in skills.catalog():
        name = item["id"]
        old = stats(skills, name)
        if old.get("pinned") or old.get("archived") or name in protected or item["error"]:
            continue
        since = old.get("last_used", old.get("created", skills.root(name).stat().st_mtime))
        days = (now - since) / 86400
        if days >= skills.app.settings.get("workshop", "archive_days", 30):
            update(skills, name, archived=True, stale=True)
            result["archived"].append(name)
        elif days >= skills.app.settings.get("workshop", "stale_days", 14) and not old.get("stale"):
            update(skills, name, stale=True)
            result["stale"].append(name)
    if any(result.values()):
        skills.app.journal.write("skill", "Куратор навыков: " + json.dumps(result, ensure_ascii=False), actor="sleep")
    return result


async def from_conversation(agent_ctx, sid, hint=""):
    """Явная команда хозяина использует тот же ограниченный формат предложения."""
    from ..mind import review, side
    from ..tools.registry import ToolContext
    last = ((agent_ctx.store.session(sid) or {}).get("meta") or {}).get("last_turn") or {}
    ranges = [[last["first"], last["last"]]] if last.get("first") and last.get("last") else review.last_turns(agent_ctx, sid, 1)
    if not ranges:
        raise error("Пока не из чего сделать навык", "Сначала выполни задачу вместе с агентом.")
    rows = review.gather(agent_ctx, sid, ranges)
    a, b = ranges[0]
    tainted = bool(last.get("tainted")) or any(any(json.loads(r["meta"]).get(key) for key in ("tainted", "untrusted", "imported")) for r in agent_ctx.store.q("SELECT meta FROM messages WHERE session_id=? AND id>=? AND id<=?", (sid, a, b)))
    previous = agent_ctx.mind.turns.get(sid)
    tainted = tainted or bool(previous and previous.tainted)
    spec = side.parse_json(await side.ask(agent_ctx, 'Оформи полезную повторяемую процедуру по завершённой работе. Только JSON {"name":"latin-id","description":"кратко","body":"шаги и условия"}. Без секретов, догадок и активного кода.',
        "Пожелание хозяина: " + hint[:1000] + "\n" + side.transcript(rows, limit_chars=10000), session_id=sid, purpose="навык по команде хозяина"))
    return await learn(agent_ctx.skills, spec, ToolContext(agent_ctx, session_id=sid, actor="owner", tainted=tainted))


async def suggest_merge(skills, ask):
    """Склейка только предлагает новый черновик; исходные навыки остаются на месте."""
    if not enabled(skills.app, "curator") or not skills.app.settings.get("workshop", "merge_skills", False):
        return None
    rows = [{"name": row["id"], "description": row["metadata"].get("description", ""), "body": skills.package(row["id"])["body"][:2000]}
            for row in skills.catalog() if row["enabled"] and skills.trusted_text(row["id"]) and not row.get("stats", {}).get("pinned")][:20]
    if len(rows) < 2:
        return None
    fingerprint = digest(rows)
    if skills.app.store.meta_get("skills.merge_last") == fingerprint:
        return None
    from ..mind import side
    from ..tools.registry import ToolContext

    value = side.parse_json(await ask('Найди дубли процедур. Обычно ответ {}. Для явных дублей предложи один объединённый навык: JSON {"description":"какие навыки объединяет и когда полезен","body":"шаги"}. Не добавляй новых полномочий.', json.dumps(rows, ensure_ascii=False)))
    skills.app.store.meta_set("skills.merge_last", fingerprint)
    if not value or not value.get("body"):
        return None
    value["name"] = "merge-" + new_id().lower()
    return await learn(skills, value, ToolContext(skills.app, actor="sleep", unattended=True), draft=True)
