"""Telegram из OpenClaw: токен бота и хозяин (allowFrom или одобренное сопряжение) — сразу готовым каналом.

Канал остаётся выключенным: включает хозяин в «Связи» (бот один, и пока он у OpenClaw, двое одновременно
читать его не смогут — Telegram отдаёт обновления только одному). Токен копируется из сейфа, куда его
положил переезд (с согласия хозяина на секреты).
"""

from __future__ import annotations

import re
import time

from .telegram import OWNER_META, TOKEN_SECRET


def owner_ids(cfg_telegram: dict, pairing_entries: list[str]) -> list[int]:
    raw = []
    for key in ("allowFrom",):
        v = cfg_telegram.get(key) if isinstance(cfg_telegram, dict) else None
        raw += v if isinstance(v, list) else []
    acc = cfg_telegram.get("accounts") if isinstance(cfg_telegram, dict) else None
    if isinstance(acc, dict):
        for a in acc.values():
            if isinstance(a, dict) and isinstance(a.get("allowFrom"), list):
                raw += a["allowFrom"]
    raw += pairing_entries
    out = []
    for x in raw:
        m = re.fullmatch(r"(?:tg:|telegram:)?\s*(\d{3,})", str(x).strip())
        if m and int(m.group(1)) not in out:
            out.append(int(m.group(1)))
    return out


def group_allow_ids(cfg_telegram: dict) -> list[int]:
    """Кого OpenClaw пускал в группы (groupAllowFrom, в том числе у аккаунтов): только числовые номера."""
    raw = []
    if isinstance(cfg_telegram, dict):
        v = cfg_telegram.get("groupAllowFrom")
        raw += v if isinstance(v, list) else []
        acc = cfg_telegram.get("accounts")
        for a in (acc.values() if isinstance(acc, dict) else []):
            if isinstance(a, dict) and isinstance(a.get("groupAllowFrom"), list):
                raw += a["groupAllowFrom"]
    out = []
    for x in raw:
        m = re.fullmatch(r"(?:tg:|telegram:)?\s*(\d{3,})", str(x).strip())
        if m and int(m.group(1)) not in out:
            out.append(int(m.group(1)))
    return out[:200]


def adopt(app, ids: list[int], group_ids: list[int] | None = None) -> dict:
    out = {"token": False, "owner": None}
    if not app.secrets.get(TOKEN_SECRET):
        for name in app.secrets.names():
            if name.startswith("openclaw:channels.telegram") and re.search(r"(bot)?token$", name, re.I):
                val = app.secrets.get(name)
                if val:
                    app.secrets.set(TOKEN_SECRET, val, note="бот Telegram (из OpenClaw)", allow={"hosts": ["api.telegram.org"], "tools": []})
                    out["token"] = True
                    break
    if ids and not app.store.meta_get(OWNER_META):
        app.store.meta_set(OWNER_META, {"user_id": ids[0], "chat_id": ids[0], "name": "хозяин из OpenClaw", "username": None, "bound_at": time.time(),
                                        "from": "openclaw"})
        out["owner"] = ids[0]
    if group_ids:
        # В группы сам не применяется: список общий у OpenClaw и свой у каждой группы Бендера. В нужной группе: /members import
        app.store.meta_set("telegram:group_allow_import", list(group_ids))
        out["group_allow"] = len(group_ids)
        app.journal.write("channel", f"Telegram из OpenClaw: список людей для групп ({len(group_ids)}) сохранён; в нужной группе напиши /members import", actor="system", data={"count": len(group_ids)})
    if out["token"] or out["owner"]:
        app.journal.write("channel", "Telegram из OpenClaw: " + ", ".join(x for x in ("токен бота" if out["token"] else "", f"хозяин {out['owner']}" if out["owner"] else "") if x)
                          + " — включи в «Связи», когда OpenClaw остановлен", actor="system", data=out)
    return out
