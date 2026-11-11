"""Коммутатор: кого из моделей позвать и что делать, если она не отвечает.

Роль (Главная, Быстрая, …) — это цепочка кандидатов «провайдер + модель + параметры».
Вызов идёт по цепочке: у кандидата перебираются ключи провайдера; ошибки, при которых
есть смысл повторить, повторяются с паузой; при лимитах, перегрузке, нехватке денег и
неверном ключе — следующий ключ; если ключи кончились — следующий кандидат. Ошибки, при
которых переключаться бесполезно или вредно (не влезло в контекст, мы прислали не тот
формат, отказ по содержанию), поднимаются сразу.

Запасная модель отвечает только на этот вызов; в результате есть notice — «ответила X,
потому что Y», — чтобы человек видел подмену. Явно выбранная хозяином модель строгая:
цепочка из одного кандидата, без тихой подмены.

Если поток оборвался, когда часть ответа уже ушла наружу, подменять молча нельзя: ошибка
поднимается с тем, что успело прийти (partial), — дальше решает вызывающий.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import random
import time
from dataclasses import dataclass
from typing import Callable

from ..net.http_client import HttpClient, NetError, Timeouts
from . import textproto
from .adapters import adapter_for
from .errors import RETRY_SAME, ProviderError, from_http
from .health import Health
from .types import Result, ToolCall, ToolSpec, Usage

MAX_SAME_RETRIES = {"server": 1, "timeout": 1, "network": 1, "rate_limit": 2}  # повторов на том же ключе
MAX_WAIT_INLINE = 20.0  # дольше ждать внутри хода не будем — лучше переключиться
BACKOFF_BASE = 1.5

Delta = Callable[[str, str], None]
Status = Callable[[dict], None]


@dataclass
class Candidate:
    provider: dict
    model: str
    params: dict

    @property
    def label(self) -> str:
        return f"{self.provider.get('name') or self.provider.get('id')} · {self.model}"


class NoRoute(ProviderError):
    pass


class Switchboard:
    def __init__(
        self,
        http: HttpClient,
        *,
        get_providers: Callable[[], dict[str, dict]],
        get_secret: Callable[[str], str | None],
        get_roles: Callable[[], dict[str, list[dict]]],
        set_secret: Callable[[str, str], None] | None = None,
    ):
        self.http = http
        self.get_providers = get_providers
        self.get_secret = get_secret
        self.get_roles = get_roles
        self.set_secret = set_secret
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self.health = Health()
        self._sems: dict[str, asyncio.Semaphore] = {}
        self._sticky: dict[str, int] = {}  # провайдер → ключ, которым последний раз получилось (ради кэша)
        self.text_tools: set[tuple[str, str]] = set()  # (провайдер, модель), которые умеют тулы только текстом
        self.economy: Callable[[], bool] = lambda: False  # режим бомжа: только бесплатные модели (решает тело)
        self.observe = None  # сбор только разрешённых данных ответа, без дополнительных запросов
        self._cli_bridge = None  # петлевой MCP для CLI-движков (bender/mcp/server.py), ставит App
        self.role_warning=None
        self._role_warnings={}
        self.get_reasoning=lambda:{}

    @property
    def cli_bridge(self):
        provider = getattr(self, 'bridge_provider', None)
        return provider() if provider else self._cli_bridge

    @cli_bridge.setter
    def cli_bridge(self, value): self._cli_bridge = value

    # цепочки

    def chain(self, role: str, *, fallback_main: bool = True) -> list[Candidate]:
        from .roles import broken,problem
        providers = self.get_providers()
        roles = self.get_roles()
        entries = roles.get(role,[])
        if self.role_warning:
            from ..agents.runtime import ACTIVE
            ctx=ACTIVE.get();key=(getattr(ctx,'agent_id','main'),role)
            previous=self._role_warnings.get(key,set());current=set()
            for item in broken({role:entries},providers):
                entry=entries[item['index']] if isinstance(entries,list) else entries
                mark=json.dumps([entry,item['reason']],sort_keys=True,ensure_ascii=False)
                current.add(mark)
                if mark not in previous:self.role_warning(role,entry,item['reason'])
            self._role_warnings[key]=current
            if len(self._role_warnings)>4096:self._role_warnings.pop(next(iter(self._role_warnings)))
        if not isinstance(entries,list):entries=[]
        out = []
        for e in entries:
            if problem(e,providers):continue
            if not e.get("enabled", True):
                continue
            p = providers.get(e.get("provider", ""))
            if not p or not p.get("enabled", True) or not e.get("model"):
                continue
            out.append(Candidate(p, e["model"], dict(e.get("params") or {})))
        if not out and role != 'main' and fallback_main:
            return self.chain('main', fallback_main=False)
        return out

    def explicit(self, provider_id: str, model: str, params: dict | None = None) -> list[Candidate]:
        p = self.get_providers().get(provider_id)
        if not p:
            raise NoRoute("not_found", provider=provider_id, model=model, message="Такого провайдера нет в настройках")
        if p.get('retired'):
            raise NoRoute('not_found', provider=provider_id, model=model, message='Этот провайдер удалён из программы; подключи OmniRoute или Google API.')
        return [Candidate(p, model, dict(params or {}))]

    def _keys(self, provider: dict) -> list[tuple[int, str | None]]:
        names = provider.get("keys") or []
        keys = [(i, self.get_secret(n)) for i, n in enumerate(names)]
        keys = [(i, k) for i, k in keys if k]
        if not keys:
            return [(0, None)]  # локальные модели и шлюзы без ключа
        sticky = self._sticky.get(provider["id"])
        if sticky is not None:
            keys.sort(key=lambda ik: ik[0] != sticky)
        return keys

    def protocol(self, cand: Candidate, tools, params: dict) -> str:
        """Как отдавать тулы этой модели: native (поле tools API) или text (textproto.py)."""
        if not tools:
            return "native"
        forced = params.get("tool_protocol") or cand.provider.get("tool_protocol")
        if forced in ("native", "text"):
            return forced
        if (cand.provider.get("id"), cand.model) in self.text_tools or cand.model in (cand.provider.get("text_tools") or []):
            return "text"
        for m in cand.provider.get("models") or []:
            if isinstance(m, dict) and m.get("id") == cand.model and m.get("tools") is False:
                return "text"  # каталог провайдера говорит, что тулов у модели нет
        return "native"

    async def _oauth_key(self, provider: dict, key_idx: int, key: str | None, *, force: bool = False) -> str | None:
        """Подписка ChatGPT: свежий access-токен (обновить за 5 минут до истечения или по 401)."""
        from . import chatgpt

        names = provider.get("keys") or []
        name = names[key_idx] if key_idx < len(names) else None
        lock = self._refresh_locks.setdefault(provider["id"], asyncio.Lock())
        async with lock:  # refresh-токен одноразовый: обновляет только один ход, остальные берут результат
            current = self.get_secret(name) if name else key
            tokens = chatgpt.unpack(current)
            if not tokens:
                raise ProviderError("auth", provider=provider.get("name", ""), message="Нет входа в ChatGPT — войди в «Моделях»")
            if force and current != key:
                return current  # пока ждали замок, токен уже обновили
            try:
                fresh, changed = await chatgpt.ensure_fresh(self.http, tokens, provider.get("auth_url"), force=force)
            except chatgpt.LoginError as e:
                klass = "network" if e.code == "chatgpt_network" else "server" if e.code == "chatgpt_refresh" else "auth"
                raise ProviderError(klass, provider=provider.get("name", ""), message=f"{e.title}. {e.hint}") from None
            if changed:
                current = chatgpt.pack(fresh)
                if name and self.set_secret:
                    self.set_secret(name, current)
            return current

    def _sem(self, provider: dict) -> asyncio.Semaphore:
        pid = provider["id"]
        s = self._sems.get(pid)
        if s is None:
            s = self._sems[pid] = asyncio.Semaphore(max(1, int(provider.get("parallel") or 4)))
        return s

    # вызов

    async def call(
        self,
        messages: list[dict],
        *,
        role: str = "main",
        target: tuple[str, str] | None = None,
        tools: list[ToolSpec] | None = None,
        params: dict | None = None,
        on_delta: Delta | None = None,
        on_status: Status | None = None,
        free_only: bool = False,
        prepare=None,
        reject_fallback: bool = False,
        fallback_main: bool = True,
        candidate_filter=None,
        on_media_error=None,
        single_attempt: bool = False,
    ) -> Result:
        if getattr(self,'authorize',None):self.authorize()
        candidates = self.explicit(*target) if target else self.chain(role,fallback_main=fallback_main)
        if candidate_filter:candidates=[c for c in candidates if candidate_filter(c)]
        main_fallback = set()
        if reject_fallback and not target and role != 'main':
            existing = {(c.provider['id'], c.model) for c in candidates}
            for c in self.chain('main'):
                key = (c.provider['id'], c.model)
                if key not in existing:
                    candidates.append(c)
                    main_fallback.add(key)
        if not candidates:
            raise NoRoute("not_found", message=f"Для роли «{role}» не выбрана ни одна модель",
                          raw="Открой «Модели» и подключи провайдера — или попроси Бендера помочь с этим.")
        if not target and (free_only or self.economy()):
            free = [c for c in candidates if self.is_free(c.provider, c.model)]
            if not free and free_only and not self.economy():
                raise NoRoute("billing", message=f"Здесь разрешены только бесплатные модели, а в цепочке роли «{role}» их нет",
                              raw="Добавь в цепочку бесплатную или локальную модель или разреши этой работе тратить деньги (например, «Сон → Денег на ночь»).")
            if not free:
                raise NoRoute("billing", message="Бюджет на сегодня кончился — режим бомжа, а бесплатных моделей в цепочке нет",
                              raw="Добавь в цепочку бесплатную или локальную модель (Ollama, «:free» у OpenRouter, подписка ChatGPT), "
                                  "подними дневной бюджет в «Настройках → Деньги» или выключи «Режим бомжа сам».")
            if len(free) != len(candidates) and on_status:
                on_status({"type": "economy", "skipped": [c.label for c in candidates if c not in free]})
            candidates = free
        strict = target is not None
        attempts: list[dict] = []
        first_label = candidates[0].label
        last_err: ProviderError | None = None
        started = time.monotonic()
        for ci, cand in enumerate(candidates):
            if (cand.provider['id'], cand.model) in main_fallback and (not last_err or last_err.klass not in ('format', 'auth', 'not_found')):
                break
            for key_idx, key in self._keys(cand.provider):
                blocked, left, why = self.health.blocked(cand.provider["id"], key_idx, cand.model)
                if blocked and not (strict and ci == 0 and len(candidates) == 1):
                    attempts.append({"candidate": cand.label, "key": key_idx, "skipped": why, "cooling": round(left)})
                    continue
                retries = 0
                refreshed = False
                media_retries=set()
                while True:
                    emitted = {"any": False}

                    def relay(kind: str, text: str, _e=emitted):
                        _e["any"] = True
                        if on_delta:
                            on_delta(kind, text)

                    attempt_started = time.monotonic()
                    prepared=None
                    try:
                        if cand.provider.get("kind") == "chatgpt":
                            key = await self._oauth_key(cand.provider, key_idx, key)
                        prepared=prepare(cand,messages) if prepare else messages
                        if inspect.isawaitable(prepared):prepared=await prepared
                        # Подготовка может описывать картинку той же моделью/провайдером.
                        async with self._sem(cand.provider):
                            res = await self._once(cand, key, prepared, tools, {**cand.params, **(params or {})}, relay, key_idx=key_idx)
                    except ProviderError as e:
                        e.provider = e.provider or cand.provider.get("name", "")
                        if on_status:
                            on_status({'type': 'provider_error', 'provider': e.provider, 'model': cand.model,
                                       'http': e.http_status, 'class': e.klass, 'body': (e.raw or e.message).encode('utf-8', 'replace')[:2048].decode('utf-8', 'ignore')})
                        attempts.append({"candidate": cand.label, "key": key_idx, "class": e.klass, "status": e.http_status, "message": e.message[:200]})
                        last_err = e
                        if self.observe: self.observe(cand, key_idx, "error", {"http": e.http_status, "class": e.klass, "body": (e.raw or e.message)[:2048]})
                        if (e.partial or {}).get("actions"):
                            raise  # CLI уже обращался к MCP: автоматический повтор/подмена может удвоить действие.
                        if single_attempt:raise
                        if on_media_error and not emitted['any'] and not any((e.partial or {}).values()):
                            from .capabilities import rejected
                            features=rejected(e,prepared)-media_retries
                            if features:
                                handled=on_media_error(cand,features,e)
                                if inspect.isawaitable(handled):handled=await handled
                                media_retries.update(features)
                                if handled:continue
                        if cand.provider.get("kind") == "chatgpt" and e.klass == "auth" and e.http_status != 403 and not refreshed and not emitted["any"]:
                            refreshed = True  # access отозван или истёк раньше срока — обновить и повторить один раз
                            try:
                                key = await self._oauth_key(cand.provider, key_idx, key, force=True)
                                continue
                            except ProviderError as e2:
                                e = e2
                                last_err = e2
                        merged = {**cand.params, **(params or {})}
                        if (tools and not emitted["any"] and textproto.unsupported(f"{e.message} {e.raw or ''}")
                                and self.protocol(cand, tools, merged) == "native" and not merged.get("tool_protocol")
                                and not cand.provider.get("tool_protocol")):
                            # модель не умеет тулы через API — тот же запрос текстовым протоколом
                            self.text_tools.add((cand.provider.get("id"), cand.model))
                            if on_status:
                                on_status({"type": "tool_protocol", "candidate": cand.label, "mode": "text",
                                           "provider_id": cand.provider.get("id"), "model": cand.model})
                            continue
                        if emitted["any"]:
                            e.partial = e.partial or {}
                            raise  # часть ответа уже ушла — молча подменять нельзя
                        if not e.failover and not (reject_fallback and e.klass == 'format'):
                            raise
                        # 403 модели у субагента не закрывает весь ключ для main.
                        health_class = 'not_found' if reject_fallback and e.klass == 'auth' and e.http_status == 403 else e.klass
                        wait = self.health.fail(cand.provider["id"], key_idx, cand.model, health_class, e.retry_after, e.message)
                        if self.observe: self.observe(cand, key_idx, "health", {})
                        same_ok = e.klass in RETRY_SAME and retries < MAX_SAME_RETRIES.get(e.klass, 0)
                        if e.klass == "rate_limit" and (e.retry_after or 0) > MAX_WAIT_INLINE:
                            same_ok = False
                        if same_ok:
                            retries += 1
                            delay = min(MAX_WAIT_INLINE, e.retry_after or (BACKOFF_BASE * (2 ** (retries - 1))) * (0.7 + 0.6 * random.random()))
                            if on_status:
                                on_status({"type": "retry", "candidate": cand.label, "attempt": retries, "class": e.klass, "wait": round(delay, 1)})
                            await asyncio.sleep(delay)
                            continue
                        _ = wait
                        break  # следующий ключ или кандидат
                    self.health.ok(cand.provider["id"], key_idx, cand.model)
                    if self.observe: self.observe(cand, key_idx, "ok", {"seconds": time.monotonic() - attempt_started})
                    self._sticky[cand.provider["id"]] = key_idx
                    res.attempts = attempts + [{"candidate": cand.label, "key": key_idx, "ok": True}]
                    res.seconds = time.monotonic() - started
                    if ci > 0:
                        why = (last_err.klass if last_err else "сбой")
                        res.notice = f"Ответила запасная модель {cand.label}: {first_label} — {_why(why)}."
                    return res
            if strict:
                break
            if on_status and ci + 1 < len(candidates):
                on_status({"type": "fallback", "from": cand.label, "to": candidates[ci + 1].label, "class": last_err.klass if last_err else ""})
        if last_err is not None:
            if len(attempts) > 1:
                summary = "\n".join(f"• {a['candidate']}: " + (f"остывает ещё {a['cooling']} с ({_why(a['skipped'])})" if "skipped" in a else _why(a.get("class", "")) + (f" — {a['message']}" if a.get("message") else "")) for a in attempts)
                last_err.detail = (last_err.detail or "") + "\n\nКого пробовали:\n" + summary
                last_err.title = "Ни одна модель не ответила" if not strict else last_err.title
            last_err.data["attempts"] = attempts
            raise last_err
        raise NoRoute("rate_limit", message="Все модели роли остывают после ошибок",
                      raw="\n".join(f"• {a['candidate']}: остывает ещё {a.get('cooling', 0)} с" for a in attempts))

    async def _once(self, cand: Candidate, key: str | None, messages: list[dict], tools, params: dict, relay: Delta, *, key_idx=0) -> Result:
        from .effort import configured
        params=configured(params,self.get_reasoning())
        original_relay=relay
        def relay(kind,text):
            if not isinstance(text,str):raise ValueError('Текст события провайдера должен быть строкой')
            original_relay(kind,text)
        p = cand.provider
        if p.get('kind') == 'claude_code':
            try:
                from . import cli
                return await cli.run_claude(self,cand,messages,tools,params,relay)
            except (ValueError,TypeError,AttributeError,KeyError,IndexError,RecursionError) as exc:
                raise ProviderError('protocol',provider=p.get('name','CLI'),model=cand.model,message='CLI прислал некорректное событие',raw=type(exc).__name__,
                                    partial={'actions':bool(tools and getattr(self,'cli_bridge',None))}) from None
        if p.get('kind') == 'antigravity':
            from .agy import run_agy
            try:
                return await run_agy(self,cand,messages,tools,params,relay)
            except (ValueError,TypeError,AttributeError,KeyError,IndexError,RecursionError) as exc:
                raise ProviderError('protocol',provider=p.get('name','agy'),model=cand.model,message='agy прислал некорректное событие',raw=type(exc).__name__,partial={'actions':True}) from None
        ad = adapter_for(p)
        proto = self.protocol(cand, tools, params)
        params = {k: v for k, v in params.items() if k != "tool_protocol"}
        hider = None
        if proto == "text":
            messages, tools = textproto.encode(messages, tools), None
            hider = textproto.Hider()
            visible = relay

            def relay(kind: str, text: str) -> None:  # блоки <tool_call> человеку не показываем — станут карточками тулов
                if kind == "text":
                    shown = hider.feed(text)
                    if shown:
                        visible("text", shown)
                else:
                    visible(kind, text)
        body = ad.body(cand.model, messages, tools, params, stream=not p.get("no_stream"))
        timeouts = Timeouts(connect=float(p.get("connect_timeout") or 20), read=float(p.get("timeout") or 300), total=float(p.get("timeout") or 300))
        pname = p.get("name") or p.get("id", "")
        try:
            resp = await self.http.request("POST", ad.url(), headers=ad.headers(key), body=json.dumps(body, ensure_ascii=False).encode("utf-8"), timeouts=timeouts)
        except NetError as e:
            raise ProviderError("timeout" if e.kind == "timeout" else "network", provider=pname, model=cand.model, message=str(e)) from None
        res = Result(provider_id=p["id"], provider_name=pname, model=cand.model)
        async with resp:
            if self.observe: self.observe(cand, key_idx, "headers", resp.headers.to_dict())
            if not resp.ok:
                raw = (await resp.read(limit=1024 * 1024)).decode("utf-8", "replace")
                raise from_http(resp.status, raw, resp.headers, provider=pname, model=cand.model)
            calls: dict[int, dict] = {}
            text_parts: list[str] = []
            reasoning_parts: list[str] = []
            usage = Usage()
            try:
                async for ev in ad.events(resp, pname, cand.model):
                    kind = ev[0]
                    if kind == "text":
                        text_parts.append(ev[1])
                        relay("text", ev[1])
                    elif kind == "reasoning":
                        reasoning_parts.append(ev[1])
                        relay("reasoning", ev[1])
                    elif kind == "tool":
                        _, idx, cid, name, args = ev
                        c = calls.setdefault(idx, {"id": "", "name": "", "args": ""})
                        if cid:
                            c["id"] = cid
                        if name:
                            c["name"] = name
                        c["args"] += args or ""
                        relay("tool", name or "")
                    elif kind == "usage":
                        u = ev[1]
                        usage.input = u.input or usage.input
                        usage.output = u.output or usage.output
                        usage.cache_read = u.cache_read or usage.cache_read
                        usage.cache_write = u.cache_write or usage.cache_write
                        usage.reasoning = u.reasoning or usage.reasoning
                        if u.cost is not None:
                            usage.cost = u.cost
                    elif kind == "finish":
                        res.finish = ev[1]
                    elif kind == "block":
                        res.provider_blocks.append(ev[1])
            except NetError as e:
                raise ProviderError("timeout" if e.kind == "timeout" else "network", provider=pname, model=cand.model, message=str(e),
                                    partial={"text": "".join(text_parts), "reasoning": "".join(reasoning_parts)}) from None
            except (ValueError,TypeError,AttributeError,KeyError,IndexError) as exc:
                raise ProviderError('protocol',provider=pname,model=cand.model,message='Провайдер прислал повреждённый ответ',raw=type(exc).__name__,partial={'text':''.join(text_parts),'reasoning':''.join(reasoning_parts)}) from None
            except ProviderError as e:
                e.partial = {"text": "".join(text_parts), "reasoning": "".join(reasoning_parts)}
                raise
        res.text = "".join(text_parts)
        res.reasoning = "".join(reasoning_parts)
        if hider is not None:
            tail = hider.flush()
            if tail:
                visible("text", tail)
            res.text, parsed = textproto.decode(res.text)
            res.tool_calls.extend(parsed)
            if parsed:
                res.finish = "tool_calls"
        for idx in sorted(calls):
            c = calls[idx]
            raw = c["args"].strip()
            try:
                args = json.loads(raw) if raw else {}
                bad = not isinstance(args, dict)
                if bad:
                    args = {}
            except ValueError:
                args, bad = {}, True
            res.tool_calls.append(ToolCall(id=c["id"] or f"call_{idx}_{int(time.time() * 1000) % 100000}", name=c["name"], arguments=args, raw_arguments=raw, bad_json=bad))
        if not res.finish:
            if not res.text and not res.tool_calls:
                raise ProviderError("network", provider=pname, model=cand.model, message="Поток закончился без ответа и без причины окончания")
            res.finish = "tool_calls" if res.tool_calls else "stop"
        res.usage = usage
        res.cost = self.price(p, cand.model, usage)
        return res

    # эмбеддинги — для поиска по смыслу (memory/vectors.py)

    async def embed(self, texts: list[str], *, role: str = "embed") -> tuple[list[list[float]], str]:
        """Векторы текстов через /embeddings (OpenAI-совместимый). Возвращает (векторы, «провайдер/модель»).
        Роль «Эмбеддинги» не подменяется Главной: у разных моделей векторы несравнимы."""
        if getattr(self,'authorize',None):self.authorize()
        candidates = [c for c in self.chain(role, fallback_main=False) if c.provider.get("kind", "openai") == "openai"]
        if not candidates:
            raise NoRoute("not_found", message="Роль «Эмбеддинги» не настроена",
                          raw="Поиск по памяти работает словами. Для поиска по смыслу выбери модель эмбеддингов в «Моделях».")
        last: ProviderError | None = None
        for cand in candidates:
            for key_idx, key in self._keys(cand.provider):
                p = cand.provider
                headers = adapter_for(p).headers(key)
                body = {"model": cand.model, "input": texts}
                body.update({k: v for k, v in cand.params.items() if k in ("dimensions", "encoding_format")})
                try:
                    async with self._sem(p):
                        resp = await self.http.request("POST", p["base_url"].rstrip("/") + "/embeddings", headers=headers,
                                                       body=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                                       timeouts=Timeouts(connect=15, read=float(p.get("timeout") or 120)))
                        async with resp:
                            raw = await resp.read(limit=64 * 1024 * 1024)
                            if not resp.ok:
                                raise from_http(resp.status, raw.decode("utf-8", "replace"), resp.headers, provider=p.get("name", ""), model=cand.model)
                    doc = json.loads(raw.decode("utf-8"))
                    data = sorted(doc.get("data") or [], key=lambda d: d.get("index", 0))
                    vecs = [d.get("embedding") for d in data]
                    if len(vecs) != len(texts) or not all(isinstance(v, list) and v for v in vecs):
                        raise ProviderError("format", provider=p.get("name", ""), model=cand.model, message="Провайдер вернул не те векторы")
                    return vecs, f"{p['id']}/{cand.model}"
                except NetError as e:
                    last = ProviderError("network", provider=p.get("name", ""), model=cand.model, message=str(e))
                except (ValueError, KeyError) as e:
                    last = ProviderError("format", provider=p.get("name", ""), model=cand.model, message=f"Ответ не разобрался: {e}")
                except ProviderError as e:
                    last = e
                    if not e.failover:
                        raise
        raise last or NoRoute("not_found", message="Ни одна модель эмбеддингов не ответила")

    @staticmethod
    def is_free(provider: dict, model: str) -> bool:
        """Модель ничего не стоит: локальная, подписка, «:free», явно нулевые цены."""
        if provider.get("free") or provider.get("is_local") or provider.get("subscription") or provider.get("kind") == "chatgpt":
            return True
        if str(model).endswith(":free"):
            return True
        from urllib.parse import urlsplit

        try:
            host = (urlsplit(str(provider.get("base_url") or "")).hostname or "").lower()
        except ValueError:
            host = ""
        if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".localhost"):
            return True
        pr = (provider.get("prices") or {}).get(model)
        return bool(pr) and not float(pr.get("in", 0) or 0) and not float(pr.get("out", 0) or 0)

    @staticmethod
    def price(provider: dict, model: str, usage: Usage) -> float:
        if usage.cost is not None:
            return float(usage.cost)
        if provider.get("free") or provider.get("is_local") or provider.get("subscription"):
            return 0.0  # подписка оплачена отдельно — ход денег не стоит
        prices = (provider.get("prices") or {}).get(model) or {}
        if not prices:
            return 0.0
        per = 1_000_000.0
        cost = (usage.input - usage.cache_read) * float(prices.get("in", 0)) / per
        cost += usage.output * float(prices.get("out", 0)) / per
        cost += usage.cache_read * float(prices.get("cache_read", prices.get("in", 0) * 0.1)) / per
        cost += usage.cache_write * float(prices.get("cache_write", prices.get("in", 0) * 1.25)) / per
        return max(0.0, cost)


def _why(klass: str) -> str:
    return {
        "rate_limit": "упёрлась в лимит", "overloaded": "перегружена", "server": "сбой сервера", "timeout": "не ответила вовремя",
        "network": "нет связи", "billing": "кончились деньги на ключе", "auth": "ключ не подошёл", "not_found": "нет такой модели",
    }.get(klass, klass or "ошибка")

