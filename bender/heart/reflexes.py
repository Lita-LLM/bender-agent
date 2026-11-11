"""Рефлексы: проверки по расписанию без модели. Хозяину пишут только при срабатывании.

Виды (поле check.type):
- disk     — мало места: {"path": "/", "min_free_gb": 2} или {"min_free_pct": 10};
- file     — файл изменился: {"path": "~/notes.txt"};
- url      — страница изменилась или в ней появился/пропал текст: {"url": "…", "contains": "…", "absent": false};
- battery  — разрядилась: {"below": 20};
- temp     — перегрев: {"above": 80};
- command  — команда упала или её вывод изменился: {"cmd": "systemctl is-active nginx", "fire": "nonzero" | "changed"}
             (идёт через Предохранитель без хозяина рядом: нужна песочница, простая команда чтения или полномочие задачи).

Повторно о том же условии рефлекс не пишет, пока оно не пройдёт (или раз в repeat_hours, по умолчанию 24).
Состояние (хеш, «сработал») — в meta задачи; меняется только когда что-то изменилось.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import time
from pathlib import Path

from ..core.errors import HumanError

TYPES = {"disk": "место на диске", "file": "файл изменился", "url": "страница изменилась", "battery": "батарея", "temp": "перегрев", "command": "команда"}


def _h(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:32]


async def check(agent_ctx, job) -> tuple[bool | None, str, dict]:
    """(сработал ли — None, если первый замер; что сказать; новое состояние для meta)."""
    c = dict(job.action.get("check") or {})
    t = c.get("type")
    st = dict(job.meta.get("reflex") or {})
    if t == "disk":
        path = os.path.expanduser(c.get("path") or str(agent_ctx.dirs.root))
        du = shutil.disk_usage(path)
        free_gb, pct = du.free / 2**30, 100 * du.free / max(1, du.total)
        low = (c.get("min_free_gb") is not None and free_gb < float(c["min_free_gb"])) or (c.get("min_free_pct") is not None and pct < float(c["min_free_pct"]))
        return low, f"Мало места на диске {path}: свободно {free_gb:.1f} ГБ ({pct:.0f}%)", st
    if t == "battery":
        from ..body.sensors import battery

        b = battery()
        if not b:
            return False, "", st
        low = b.get("percent") is not None and b["percent"] < float(c.get("below", 20)) and not b.get("charging")
        return low, f"Батарея садится: {b.get('percent')}%", st
    if t == "temp":
        from ..body.sensors import temperature

        v = temperature()
        hot = v is not None and v > float(c.get("above", 80))
        return hot, f"Машина перегрелась: {v} °C", st
    if t == "file":
        p = Path(os.path.expanduser(c.get("path") or ""))
        try:
            h = _h(p.read_bytes())
        except OSError:
            h = "нет файла"
        prev = st.get("hash")
        st["hash"] = h
        if prev is None:
            return None, "", st
        return h != prev, f"Файл {p} изменился" if h != "нет файла" else f"Файл {p} пропал", st
    if t == "url":
        from ..tools.web import fetch_text

        text = await fetch_text(agent_ctx, c.get("url") or "")
        if c.get("contains"):
            has = c["contains"].lower() in text.lower()
            want_absent = bool(c.get("absent"))
            hit = (not has) if want_absent else has
            return hit, (f"На странице {c['url']} пропало «{c['contains']}»" if want_absent else f"На странице {c['url']} появилось «{c['contains']}»"), st
        h = _h(text.encode("utf-8"))
        prev = st.get("hash")
        st["hash"] = h
        if prev is None:
            return None, "", st
        return h != prev, f"Страница {c.get('url')} изменилась", st
    if t == "command":
        from ..tools.registry import ToolContext

        ctx = ToolContext(app=agent_ctx, session_id=f"job:{job.id}", actor="agent", tainted=False, unattended=True, turn={}, progress=lambda s: None)
        r = await agent_ctx.tools.run("run", {"command": c.get("cmd") or "", "timeout": int(c.get("timeout", 30))}, ctx)
        if not r.ok and "Отказано" in (r.content or ""):
            raise HumanError("reflex_denied", "Предохранитель не дал выполнить команду рефлекса", r.content[:300])
        code = (r.data or {}).get("code")
        if c.get("fire", "nonzero") == "nonzero":
            return code not in (0, None), f"Команда «{c.get('cmd')}» упала (код {code}): {(r.content or '')[-300:]}", st
        h = _h((r.content or "").encode("utf-8"))
        prev = st.get("hash")
        st["hash"] = h
        if prev is None:
            return None, "", st
        return h != prev, f"Вывод «{c.get('cmd')}» изменился:\n{(r.content or '')[-500:]}", st
    raise HumanError("bad_reflex", f"Не знаю рефлекс «{t}»", "Виды: " + ", ".join(f"{k} ({v})" for k, v in TYPES.items()))


async def evaluate(agent_ctx, job) -> tuple[bool, str, dict | None]:
    """Проверить и решить, говорить ли хозяину. Возвращает (сказать, текст, новое meta или None — не менялось)."""
    fired, text, st = await check(agent_ctx, job)
    old = dict(job.meta.get("reflex") or {})
    now = time.time()
    say = False
    if fired:
        repeat = float(job.action.get("check", {}).get("repeat_hours", 24)) * 3600
        if not old.get("firing") or now - float(old.get("said", 0)) > repeat:
            say = True
            st["said"] = now
        st["firing"] = True
    else:
        st.pop("firing", None)
    changed = st != old
    return say, text, ({**job.meta, "reflex": st} if changed else None)


async def run(agent_ctx, job) -> str:
    """Для запуска вручную («проверить сейчас»): всегда возвращает, что увидел."""
    say, text, meta = await evaluate(agent_ctx, job)
    if meta is not None:
        agent_ctx.heart.jobs.update(job.id, meta=meta)
    return text if say else ("сработало раньше, хозяин уже знает: " + text if (meta or job.meta).get("reflex", {}).get("firing") else "всё спокойно")
