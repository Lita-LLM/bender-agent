"""Каталог моделей провайдера: список с /models, длины контекста, цены, самопроверка.

OpenRouter отдаёт цены за токен (pricing.prompt / completion) — переводим в $ за миллион.
"""

from __future__ import annotations

import time

from ..core.errors import HumanError
from ..net.http_client import HttpClient, NetError, Timeouts
from .adapters import adapter_for
from .errors import from_http


async def list_models(http: HttpClient, provider: dict, key: str | None) -> list[dict]:
    if provider.get('audio_only'):
        return [dict(model,note='Из голосового пресета; доступность проверяет фактический аудиозапрос.') for model in provider.get('models',[])]
    if provider.get("kind") == "chatgpt":
        from .chatgpt import MODELS

        return [dict(m, subscription=True) for m in MODELS]  # у подписки нет /models — свой список
    if provider.get("kind") == "antigravity":
        from .agy import list_models as agy_models
        return await agy_models(provider)
    if provider.get("kind") == "claude_code":
        from .cli import MODELS as CC

        sub = provider.get("billing") != "api"
        notes = {"default": "какую выбрал сам Claude Code", "opus": "самая умная", "sonnet": "умная и быстрая", "fable": "новая линейка", "haiku": "самая быстрая и дешёвая"}
        return [{"id": m, "subscription": sub, "note": notes.get(m, "")} for m in CC]
    ad = adapter_for(provider)
    url = provider["base_url"].rstrip("/") + "/models"
    headers = ad.headers(key)
    headers.pop("Content-Type", None)
    try:
        resp = await http.get(url, headers=headers, timeouts=Timeouts(connect=10, read=30))
    except NetError as e:
        raise HumanError("provider_network", f"Нет связи с {provider.get('name')}", str(e)) from None
    async with resp:
        raw = await resp.read(limit=32 * 1024 * 1024)
        if not resp.ok:
            raise from_http(resp.status, raw.decode("utf-8", "replace"), resp.headers, provider=provider.get("name", ""), model="")
    import json

    try:
        doc = json.loads(raw.decode("utf-8"))
    except ValueError:
        raise HumanError("provider_format", "Провайдер прислал не JSON на /models", raw[:300].decode("utf-8", "replace")) from None
    items = doc.get("data") if isinstance(doc, dict) else doc
    if isinstance(doc, dict) and "models" in doc and not items:
        items = doc["models"]
    out = []
    for m in items or []:
        if not isinstance(m, dict):
            continue
        mid = m.get("id") or m.get("name")
        if not mid:
            continue
        entry = {"id": str(mid), "name": m.get("name") or m.get("display_name") or str(mid)}
        ctx = (m.get("context_length") or m.get("max_context_length") or (m.get("top_provider") or {}).get("context_length")
               or m.get("max_input_tokens") or m.get("inputTokenLimit") or m.get("input_token_limit") or m.get("context_window"))  # OpenRouter, Anthropic, Gemini, прочие
        if ctx:
            entry["context"] = int(ctx)
        pr = m.get("pricing") or {}
        try:
            if pr.get("prompt") is not None:
                entry["price"] = {"in": float(pr["prompt"]) * 1e6, "out": float(pr.get("completion") or 0) * 1e6}
                if pr.get("input_cache_read"):
                    entry["price"]["cache_read"] = float(pr["input_cache_read"]) * 1e6
        except (TypeError, ValueError):
            pass
        if str(mid).endswith(":free") or (entry.get("price") and entry["price"]["in"] == 0 and entry["price"]["out"] == 0):
            entry["free"] = True
        params = m.get("supported_parameters")
        if isinstance(params, list):
            entry["tools"] = "tools" in params
            entry["reasoning"] = any(key in params for key in ('reasoning','reasoning_effort','include_reasoning'))
        efforts=m.get('supported_reasoning_efforts')
        if isinstance(efforts,list):
            from .effort import LEVELS
            entry['reasoning_efforts']=[('off' if x=='none' else x) for x in efforts if isinstance(x,str) and (x in LEVELS or x=='none')]
        caps=m.get('capabilities')
        if provider.get('kind')=='anthropic' and isinstance(caps,dict):
            known={}
            for key,path in {'adaptive':('thinking','types','adaptive'),'disabled':('thinking','types','disabled'),
                             'budget':('thinking','types','enabled'),'xhigh':('effort','xhigh'),'max':('effort','max')}.items():
                current=caps;present=True
                for part in path:
                    if not isinstance(current,dict) or part not in current:present=False;break
                    current=current[part]
                if isinstance(current,dict) and isinstance(current.get('supported'),bool):known[key]=current['supported']
                elif present and current is None:known[key]=False
            if known:entry['reasoning_capabilities']=known
        mods = (m.get("architecture") or {}).get("input_modalities") or m.get('input_modalities')
        if isinstance(mods, list):
            entry["vision"] = "image" in mods
            entry['audio_input']='audio' in mods
        if isinstance(m.get('audio_input'),bool):entry['audio_input']=m['audio_input']
        if isinstance(m.get('audio_formats'),list):entry['audio_formats']=m['audio_formats']
        if provider.get('preset')=='gemini' and str(mid).startswith('gemini-'):
            entry.update(audio_input=True,audio_formats=['wav','mp3'])
        out.append(entry)
    out.sort(key=lambda e: e["id"])
    return out


def apply_catalog(provider: dict, models: list[dict]) -> dict:
    """Запомнить список и цены в провайдере (цены, заданные вручную, не перетираются)."""
    p = dict(provider)
    ids = {m.get("id") for m in models if isinstance(m, dict)}
    manual = [m for m in provider.get("models") or [] if isinstance(m, dict) and m.get("manual") and m.get("id") not in ids]
    p["models"] = list(models) + manual  # введённые руками не пропадают при обновлении каталога
    p["models_at"] = time.time()
    prices = dict(p.get("prices") or {})
    for m in models:
        if m.get("price") and m["id"] not in prices:
            prices[m["id"]] = m["price"]
    p["prices"] = prices
    return p
