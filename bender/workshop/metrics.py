"""Метрики из журнала/задач/вызовов. Только чтение по запросу, без таймера."""
import json
import time

from ..core.watchdog import load_boot
from .lessons import correction


def snapshot(agent_ctx):
    turns = [{"at": row["ts"], **json.loads(row["data"])} for row in agent_ctx.store.q("SELECT ts,data FROM journal WHERE kind='turn' AND actor='owner' ORDER BY seq DESC LIMIT 5000")]
    tasks = agent_ctx.store.one("SELECT COUNT(*) AS total,COALESCE(SUM(CASE WHEN status='done' AND (schema_valid IS NULL OR schema_valid=1) THEN 1 ELSE 0 END),0) AS successful,COALESCE(SUM(CASE WHEN schema_valid=0 THEN 1 ELSE 0 END),0) AS invalid_schema,COALESCE(AVG(cost),0) AS mean_cost FROM tasks WHERE status IN ('done','error','cancelled')")
    billing = agent_ctx.store.one("SELECT COUNT(*) AS calls,COALESCE(SUM(cost),0) AS cost FROM calls")
    fixes = 0
    for row in agent_ctx.store.q("SELECT data,meta FROM messages WHERE role='user' ORDER BY id DESC LIMIT 10000"):
        meta = json.loads(row["meta"] or "{}")
        if meta.get("actor", "owner") == "owner" and not meta.get("imported"):
            content = json.loads(row["data"]).get("content", "")
            if isinstance(content, str) and correction(content):
                fixes += 1
    series = {}
    for row in turns:
        day = time.strftime("%Y-%m-%d", time.gmtime(row["at"]))
        item = series.setdefault(day, {"day": day, "total": 0, "successful": 0, "cost": 0})
        item["total"] += 1
        item["successful"] += int(bool(row.get("ok")))
        item["cost"] += row.get("cost", 0)
    skills = agent_ctx.store.meta_get("skills.stats", {})
    exams = agent_ctx.store.meta_get("workshop.exams", [])
    success = sum(bool(row.get("ok")) for row in turns)
    return {"enabled": agent_ctx.settings.get("workshop", "enabled", True) and agent_ctx.settings.get("workshop", "metrics", True),
            "turns": {"total": len(turns), "successful": success, "rate": round(success / len(turns), 4) if turns else None,
                      "mean_cost": sum(row.get("cost", 0) for row in turns) / len(turns) if turns else None},
            "owner_reactions": {e:sum(e in value for value in agent_ctx.store.meta_get("telegram:ratings",{}).values()) for e in ("👍","👎")},
            "tasks": dict(tasks), "billing": dict(billing), "corrections": fixes, "rollbacks": load_boot(agent_ctx.dirs.root).get("rollback_count", 0),
            "skills": [{"name": name, **values} for name, values in skills.items()], "skill_uses": sum(row.get("uses", 0) for row in skills.values()),
            "exams": [{"at": row["at"], "ok": row["ok"], "seconds": row["seconds"], "id": row["id"]} for row in exams],
            "series": sorted(series.values(), key=lambda row: row["day"]),
            "limits": "Исход — отсутствие ошибки хода/тула; это не подтверждение правильности хозяином. Последние 5000 завершений с вехи 7; поправки — поиск фраз в последних 10000 сообщениях хозяина. Цена хода — вызовы модели в нём; фоновые разборы и субагенты учитываются отдельно в общих расходах."}
