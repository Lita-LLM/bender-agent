"""Расписания и намерения, перенесённые из OpenClaw, становятся задачами и намерениями Бендера.

Переезд складывает их в meta (`import:openclaw:cron`, `import:openclaw:intents`), а здесь они
превращаются в настоящие: задачи — **выключенными** (хозяин включает после просмотра на экране
«Сердце»: чужой промпт по расписанию без взгляда хозяина — плохая идея), намерения — включёнными
(они только напоминают агенту, ничего не делают сами). Повтор ничего не дублирует: перенесённые
id запоминаются в `import:openclaw:adopted`.
"""

from __future__ import annotations

import json
import time

from ..core.errors import HumanError


def convert_schedule(job: dict) -> dict | None:
    s = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
    kind = s.get("kind")
    if kind == "cron" and s.get("expr"):
        return {"kind": "cron", "expr": str(s["expr"]).strip()}
    if kind == "every" and (s.get("everyMs") or s.get("every")):
        sec = int(s.get("everyMs") or 0) / 1000 or float(s.get("every") or 0)
        return {"kind": "every", "every": max(60, int(sec)), "anchor": time.time()}
    if kind == "at":
        v = s.get("atMs") or s.get("at")
        try:
            ts = float(v) / 1000 if isinstance(v, (int, float)) and v > 1e11 else float(v) if isinstance(v, (int, float)) else None
            if ts is None and isinstance(v, str):
                from datetime import datetime

                ts = datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return None
        return {"kind": "at", "at": ts} if ts else None
    return None


def convert_action(job: dict) -> dict | None:
    p = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    text = str(p.get("message") or p.get("text") or p.get("prompt") or "").strip()
    if not text:
        return None
    if p.get("kind") in ("systemEvent", "reminder", "notify"):
        return {"type": "remind", "text": text}
    session = "main" if job.get("sessionTarget") == "main" else "isolated"
    return {"type": "agent", "prompt": text, "session": session, "deliver": "chat"}


def _window(job: dict) -> str:
    pol = job.get("policy") if isinstance(job.get("policy"), dict) else {}
    ah = pol.get("activeHours") or job.get("activeHours")
    if isinstance(ah, dict) and ah.get("start") and ah.get("end"):
        end = "00:00" if str(ah["end"]) in ("24:00", "24") else str(ah["end"])
        return f"{ah['start']}-{end}"
    return ""


def adopt(agent_ctx) -> dict:
    """Превратить архив переезда в задачи и намерения. Возвращает счёт: {"jobs": n, "intents": n, "skipped": [...]}."""
    store = agent_ctx.store
    done = set(store.meta_get("import:openclaw:adopted", []) or [])
    out: dict = {"jobs": 0, "intents": 0, "skipped": []}
    changed = False
    for item in store.meta_get("import:openclaw:cron", []) or []:
        if not isinstance(item, dict) or not item.get("id") or f"cron:{item['id']}" in done:
            continue
        job = item.get("job") if isinstance(item.get("job"), dict) else {}
        sch, act = convert_schedule(job), convert_action(job)
        done.add(f"cron:{item['id']}")
        changed = True
        name = str(item.get("name") or job.get("name") or "задача из OpenClaw")
        if not sch or not act or (sch["kind"] == "at" and sch["at"] < time.time()):
            out["skipped"].append(f"«{name}»: не понял расписание или действие" if not (sch and act) else f"«{name}»: время уже прошло")
            continue
        pol = job.get("policy") if isinstance(job.get("policy"), dict) else {}
        try:
            agent_ctx.heart.jobs.add(name, sch, act, window=_window(job), idle_only=bool(pol.get("idleOnly") or job.get("idleOnly")), created_by="openclaw",
                               source="openclaw", enabled=False, meta={"openclaw": {"id": item["id"], "was_enabled": bool(item.get("enabled")),
                                                                                   "tz": (job.get("schedule") or {}).get("tz")}})
            out["jobs"] += 1
        except HumanError as e:
            out["skipped"].append(f"«{name}»: {e.title}")
    for it in store.meta_get("import:openclaw:intents", []) or []:
        if not isinstance(it, dict) or not it.get("id") or f"intent:{it['id']}" in done:
            continue
        done.add(f"intent:{it['id']}")
        changed = True
        kw = it.get("trigger_keywords")
        if isinstance(kw, str):
            try:
                kw = json.loads(kw)
            except ValueError:
                kw = [k for k in kw.split(",")]
        exp = it.get("expires_at")
        days = 90.0
        if isinstance(exp, (int, float)) and exp:
            left = (exp / 1000 if exp > 1e11 else exp) - time.time()
            if left <= 0:
                out["skipped"].append(f"намерение «{str(it.get('description'))[:40]}»: срок вышел")
                continue
            days = left / 86400
        try:
            agent_ctx.heart.intents.add(str(it.get("description") or ""), kw or [], days=days, created_by="openclaw", source="openclaw")
            out["intents"] += 1
        except HumanError as e:
            out["skipped"].append(f"намерение «{str(it.get('description'))[:40]}»: {e.title}")
    if changed:
        store.meta_set("import:openclaw:adopted", sorted(done))
        if out["jobs"] or out["intents"]:
            agent_ctx.journal.write("heart", f"Из OpenClaw: задач {out['jobs']} (выключены до просмотра), намерений {out['intents']}", actor="system", data=out)
            agent_ctx.bus.publish({"type": "job", "status": "adopted", **out})
            agent_ctx.heart.poke()
    return out
