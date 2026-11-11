"""Движок Antigravity (`agy -p`), превью, только текст, без тулов Бендера. Выключен, пока хозяин не принял риск.

Google не поддерживает обёртки agy в сторонних приложениях на личных планах и может заблокировать аккаунт
(docs/research/ANTIGRAVITY.md). Поэтому движок осторожный:
- встроенные инструменты agy обязаны быть выключены: если в `agy --help` нет флага, который это делает, ход отказывается;
- тулы Бендера не передаются (петлевого MCP нет): годится ролям без тулов (Быстрая, Чтец, Сновидец и т. п.);
- историю уносит стенограммой, сессия agy не сохраняется; чужих настроек и памяти agy в ход не попадает.
Протокол stream-json разобран по документации и не проверялся на настоящем бинарнике."""
from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from .cli_common import classify, command, flags, stream
from .errors import ProviderError
from .types import Result, Usage

MODELS = ["default"]
# Флаги, которыми agy лишается своих инструментов, в порядке предпочтения: (флаг, аргументы)
DISABLERS = (("--no-tools", []), ("--tools", [""]), ("--allowedTools", [""]), ("--allowed-tools", [""]))
RISK = ("Google не поддерживает обёртки agy в сторонних приложениях на личных планах и может заблокировать аккаунт. "
        "Подключай только если принимаешь этот риск сам: подтверди при подключении.")


def disabler(known):
    for flag, args in DISABLERS:
        if flag in known:
            return [flag, *args]
    return None


def text_of(event) -> str:
    """Текст из события потока: форматы agy по документации разные, берём самые вероятные."""
    if not isinstance(event, dict):
        return ""
    message = event.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(str(b.get("text") or "") for b in content if isinstance(b, dict) and b.get("type", "text") == "text")
    for key in ("text", "content", "delta"):
        if isinstance(event.get(key), str):
            return event[key]
    return ""


async def run_agy(router, cand, messages: list[dict], tools, params: dict, relay) -> Result:
    p = cand.provider
    pname = p.get("name") or "Antigravity"
    if tools:
        raise ProviderError("format", provider=pname, model=cand.model,
                            message="Antigravity работает без тулов Бендера: поставь его на роль без тулов (Быстрая, Чтец) или выбери другую главную модель.")
    exe = command(p, "agy")
    if not exe or not shutil.which(exe[0]):
        raise ProviderError("not_found", provider=pname, model=cand.model, message=f"Не нашёл «{exe[0] if exe else 'agy'}». Поставь Antigravity CLI и войди в него.")
    known = await flags(exe)
    off = disabler(known)
    if off is None or "--output-format" not in known:
        raise ProviderError("format", provider=pname, model=cand.model,
                            message="В agy не нашёл флаг, отключающий его встроенные инструменты (или поток stream-json). Без этого Бендер движок не запускает: проверь `bender try agy`.")
    from .cli import stateless_prompt
    system = next((m["content"] for m in messages if m.get("role") == "system" and isinstance(m.get("content"), str)), "")
    prompt = (f"[Инструкция]\n{system}\n\n" if system else "") + stateless_prompt(messages, [])
    args = exe + ["-p", "--output-format", "stream-json", *off]
    if cand.model and cand.model.strip().lower() != "default":
        args += ["--model", cand.model]
    bridge = getattr(router, "cli_bridge", None)
    cwd = (Path(bridge.app.dirs.local) if bridge is not None else Path.home() / ".bender-tmp") / "antigravity"
    cwd.mkdir(parents=True, exist_ok=True)
    env = {k: v for k, v in os.environ.items() if not k.startswith("BENDER_")}
    parts: list[str] = []
    final = {}

    def event(ev):
        if not isinstance(ev, dict):
            return
        if ev.get("type") in ("result", "final", "done"):
            final.update(ev)
            return
        chunk = text_of(ev)
        if chunk:
            parts.append(chunk)
            relay("text", chunk)
    try:
        code, err = await stream(args, prompt=prompt, cwd=cwd, env=env, timeout=float(p.get("timeout") or 600), on_event=event)
    except asyncio.TimeoutError:
        raise ProviderError("timeout", provider=pname, model=cand.model, message="Antigravity молчит слишком долго") from None
    answer = (final.get("result") if isinstance(final.get("result"), str) else text_of(final)) or "".join(parts)
    if (code and not answer) or not answer.strip():
        raise ProviderError(classify(f"{err}"), provider=pname, model=cand.model, message=(err or f"код выхода {code}")[-400:] or "пустой ответ", raw=err[-1500:])
    res = Result(provider_id=p["id"], provider_name=pname, model=cand.model)
    res.text = answer.strip()
    res.usage = Usage(input=0, output=0)
    res.finish = "stop"
    return res
