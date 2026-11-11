"""Тело: машина, на которой живёт агент, и он сам. Самочувствие — функциональное состояние, а не эмоции:
оно меняет поведение (реже пульс, бесплатные модели, сказать хозяину).

- бодр — всё в порядке;
- устал — потрачено больше 80% дневного бюджета;
- голоден — бюджет кончился: режим бомжа (только бесплатные модели), если включён «Режим бомжа сам»;
- болен — сыплются ошибки моделей (5+ за полчаса) или вся Главная остывает;
- перегрет / разряжен — машине плохо (температура, батарея).

Режимы жизни: норма · экономия · безопасный режим · стоп (стоп-кран). Сон добавится со сном.
Всё считается по запросу — тело не опрашивает датчики само.
"""

from __future__ import annotations

import time

from . import sensors

MOODS = {"ok": "бодр", "tired": "устал", "hungry": "голоден", "sick": "болен", "hot": "перегрет", "drained": "разряжен"}


class Body:
    def __init__(self, agent_ctx):
        self.app = agent_ctx

    # деньги

    def budget(self) -> dict:
        b = self.app.settings.group("budget")
        daily = float(b.get("daily_usd") or 0)
        spent = self.app.cost_today()
        return {"daily": daily, "spent": round(spent, 6), "left": round(daily - spent, 6) if daily else None, "share": (spent / daily) if daily else 0.0,
                "economy_auto": bool(b.get("economy_auto", True))}

    def economy(self) -> bool:
        """Режим бомжа: бюджет на сегодня кончился и хозяин разрешил переходить на бесплатное само."""
        b = self.budget()
        return bool(b["daily"]) and b["share"] >= 1.0 and b["economy_auto"]

    # самочувствие

    def mood(self, *, snap: dict | None = None) -> dict:
        agent_ctx = self.app
        why: list[str] = []
        code = "ok"
        snap = snap if snap is not None else {"temp_c": sensors.temperature(), "battery": sensors.battery()}
        t = snap.get("temp_c")
        if t is not None and t > float(agent_ctx.settings.get("heart", "alarm_temp_c", 85)):
            code = "hot"
            why.append(f"температура {t} °C")
        bat = snap.get("battery") or {}
        if bat.get("percent") is not None and bat["percent"] < 15 and not bat.get("charging"):
            code = code if code != "ok" else "drained"
            why.append(f"батарея {bat['percent']}%")
        errs = agent_ctx.store.one("SELECT COUNT(*) AS n FROM calls WHERE created_at > ? AND error IS NOT NULL", (time.time() - 1800,))
        n_err = errs["n"] if errs else 0
        if n_err >= 5:
            code = code if code != "ok" else "sick"
            why.append(f"ошибок моделей за полчаса: {n_err}")
        b = self.budget()
        if b["daily"]:
            if b["share"] >= 1.0:
                code = "hungry" if code == "ok" else code
                why.append(f"бюджет ${b['daily']:.2f} на сегодня кончился" + (" — режим бомжа" if b["economy_auto"] else ""))
            elif b["share"] >= 0.8:
                code = "tired" if code == "ok" else code
                why.append(f"потрачено {b['share'] * 100:.0f}% дневного бюджета")
        mode = "halt" if agent_ctx.guard.halted else "safe" if agent_ctx.safe else "economy" if self.economy() else "normal"
        return {"code": code, "word": MOODS[code], "why": why, "mode": mode,
                "mode_word": {"halt": "стоп", "safe": "безопасный режим", "economy": "экономия (режим бомжа)", "normal": "норма"}[mode]}

    def snapshot(self, *, full: bool = False) -> dict:
        agent_ctx = self.app
        snap = sensors.snapshot(agent_ctx.dirs.root)
        snap["uptime_machine_s"] = sensors.machine_uptime_s()
        snap["network"] = sensors.network()
        try:
            snap["self"]["db_mb"] = round(agent_ctx.dirs.db.stat().st_size / 1e6, 1)
        except OSError:
            pass
        snap["budget"] = self.budget()
        snap["mood"] = self.mood(snap=snap)
        snap["models"] = self.models_health()
        if full:
            snap["top"] = sensors.top_processes(6)
        return snap

    def models_health(self) -> list[dict]:
        out = []
        r = self.app.router
        for role in ("main", "fast"):
            for c in r.chain(role, fallback_main=False):
                blocked, left, why = r.health.blocked(c.provider["id"], 0, c.model)
                out.append({"role": role, "label": c.label, "ok": not blocked, "cooling_s": round(left or 0), "why": why or ""})
        return out

    def describe(self, snap: dict | None = None) -> str:
        snap = snap or self.snapshot()
        m = snap["mood"]
        lines = [sensors.describe(snap)]
        b = snap["budget"]
        lines.append(f"Деньги сегодня: ${b['spent']:.4f}" + (f" из ${b['daily']:.2f}" if b["daily"] else " (без лимита)"))
        if snap.get("self", {}).get("db_mb") is not None:
            lines.append(f"База: {snap['self']['db_mb']} МБ")
        bad = [x for x in snap.get("models") or [] if not x["ok"]]
        if bad:
            lines.append("Модели остывают: " + ", ".join(f"{x['label']} ({x['why']}, ещё {x['cooling_s']} с)" for x in bad))
        lines.append(f"Самочувствие: {m['word']}" + (f" — {'; '.join(m['why'])}" if m["why"] else "") + f" · режим: {m['mode_word']}")
        return "\n".join(lines)
