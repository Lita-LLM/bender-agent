"""Побочные вызовы модели: пересказ, разбор после хода — короткие запросы к роли «Быстрая» мимо чата.

Не стримятся в чат, не дают тулов, пишутся в «вызовы» с пометкой цели (видно, сколько стоит память).
Роль «Быстрая» не настроена — берётся Главная (так устроен router.chain).
"""

from __future__ import annotations

import json
import re

from ..core import json5


async def ask(agent_ctx, system: str, user: str, *, session_id: str | None, purpose: str) -> str:
    res = await agent_ctx.router.call([{"role": "system", "content": system}, {"role": "user", "content": user}], role="fast")
    agent_ctx.store.add_call(session_id=session_id, role="fast", provider=res.provider_name, model=res.model, usage=res.usage.to_dict(), cost=res.cost,
                       seconds=res.seconds, notice=purpose, request={"messages": [{"role": "system", "content": system[:2000]}, {"role": "user", "content": user}]})
    return res.text or ""


def parse_json(text: str) -> dict | None:
    """Первый JSON-объект из ответа модели: в ```json```, с хвостовыми запятыми, с болтовнёй вокруг."""
    text = (text or "").strip()
    m = re.search(r"```(?:json5?|JSON)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [m.group(1)] if m else []
    start = text.find("{")
    if start >= 0:
        depth, in_str, esc = 0, False, False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break
    for c in candidates:
        for loads in (json.loads, json5.loads):
            try:
                v = loads(c)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(v, dict):
                return v
    return None


def transcript(rows: list[dict], *, limit_chars: int = 12000, tool_chars: int = 300, skip_tools: tuple[str, ...] = ()) -> str:
    """Сообщения → читаемая стенограмма для побочной модели. Длинные результаты тулов урезаются,
    результаты skip_tools заменяются пометкой (например, recall — это уже память, её не надо запоминать снова)."""
    lines: list[str] = []
    for r in rows:
        role = r.get("role")
        content = r.get("content")
        if not isinstance(content, str):
            content = json.dumps(content, ensure_ascii=False)[:2000] if content else ""
        if role == "user":
            from ..core.dates import message_prefix
            label = message_prefix(r)
            actor=r.get('meta',{}).get('actor','owner')
            speaker='Хозяин' if actor=='owner' else f'Не хозяин ({actor})'
            lines.append((label + ' ' if label else '') + f"{speaker}: {content.strip()}")
        elif role == "assistant":
            if content.strip():
                lines.append(f"Агент: {content.strip()}")
            for c in r.get("tool_calls") or []:
                fn = c.get("function") or c
                args = fn.get("arguments")
                if not isinstance(args, str):
                    args = json.dumps(args or {}, ensure_ascii=False)
                lines.append(f"Агент вызвал {fn.get('name')}({args[:200]})")
        elif role == "tool":
            name = r.get("name") or "тул"
            if name in skip_tools:
                lines.append(f"[{name}: результат — из памяти, пропущен]")
            else:
                body = content.strip()
                if len(body) > tool_chars:
                    body = body[:tool_chars] + "…"
                lines.append(f"[{name}: {body}]")
    text = "\n".join(lines)
    if len(text) > limit_chars:
        text = "…\n" + text[-limit_chars:]
    return text
