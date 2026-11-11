"""Вход через ChatGPT (подписка Plus/Pro/…) и модели по подписке — тот же путь, что у Codex CLI и OpenClaw.

OpenAI разрешает OAuth подписки во внешних инструментах (адреса и форматы — как у Codex CLI). Бендер представляется своим именем (`originator: bender`),
без маскировки под чужой клиент.

Вход:
- по коду устройства — Бендер получает код, хозяин открывает auth.openai.com/codex/device на любом
  устройстве и вводит его (удобно для телефона и машины без браузера);
- через браузер (PKCE) — страница входа, обратный вызов на localhost:1455; если браузер на другом
  устройстве, адрес из адресной строки вставляется руками.

Токены — в сейфе одной строкой JSON {access, refresh, expires, account_id, email}; access обновляется
сам за 5 минут до истечения (refresh-токен при этом может смениться — сохраняется новый).

Запросы — Responses API на chatgpt.com/backend-api/codex/responses, только потоком.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import platform
import secrets
import time
import uuid
from typing import AsyncIterator
from urllib.parse import parse_qs, urlencode, urlsplit

from .. import __version__
from ..core.errors import HumanError
from ..net.http_client import HttpClient, NetError, Response
from ..net.sse import parse_sse
from .errors import ProviderError, api_message, classify
from .types import ToolSpec, Usage

AUTH_URL = "https://auth.openai.com"
BACKEND_URL = "https://chatgpt.com/backend-api/codex"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
SCOPE = "openid profile email offline_access"
CALLBACK_PORT = 1455
CALLBACK_PATH = "/auth/callback"
DEVICE_TIMEOUT = 900  # код живёт 15 минут
REFRESH_MARGIN = 300  # обновлять access за 5 минут до истечения

# Модели по подписке (октябрь 2026, по документации OpenClaw 2026.10.5). Списка моделей аккаунта
# по HTTP нет — держим свой, плюс ручной ввод в пульте.
MODELS = [
    {"id": "gpt-6-astra", "note": "по умолчанию у подписки"},
    {"id": "gpt-6.1-sol", "note": "сильнейшая, размышления не выключаются"},
    {"id": "gpt-6-sol"},
    {"id": "gpt-6-luna", "note": "дешевле — для роли «Быстрая»"},
    {"id": "gpt-5.6-sol"},
    {"id": "gpt-5.6-terra"},
    {"id": "gpt-5.6-luna"},
    {"id": "gpt-5.5", "note": "если у аккаунта нет 5.6"},
]
DEFAULT_MODEL = "gpt-6-astra"
FAST_MODEL = "gpt-6-luna"
for _model in MODELS:_model.update(vision=True,audio_input=None)


def auth_base() -> str:
    """Адрес входа OpenAI. BENDER_CHATGPT_AUTH_URL — для проверок на фальшивом сервере."""
    return (os.environ.get("BENDER_CHATGPT_AUTH_URL") or AUTH_URL).rstrip("/")


def backend_base() -> str:
    return (os.environ.get("BENDER_CHATGPT_BACKEND_URL") or BACKEND_URL).rstrip("/")


def client_headers() -> dict:
    return {"originator": "bender", "User-Agent": f"bender/{__version__} ({platform.system()} {platform.release()}; {platform.machine()})"}


class LoginError(HumanError):
    def __init__(self, title: str, hint: str = "", detail: str | None = None, code: str = "chatgpt_login"):
        super().__init__(code, title, hint, detail=detail, status=400)


# --- токены ------------------------------------------------------------------------------


def jwt_claims(token: str) -> dict:
    parts = (token or "").split(".")
    if len(parts) != 3:
        return {}
    try:
        pad = "=" * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(parts[1] + pad).decode("utf-8"))
        return data if isinstance(data, dict) else {}
    except (ValueError, UnicodeDecodeError):
        return {}


def account_id(access: str) -> str | None:
    auth = jwt_claims(access).get("https://api.openai.com/auth")
    v = auth.get("chatgpt_account_id") if isinstance(auth, dict) else None
    return v if isinstance(v, str) and v else None


def pack(tokens: dict) -> str:
    return json.dumps(tokens, ensure_ascii=False, separators=(",", ":"))


def unpack(secret: str | None) -> dict | None:
    if not secret:
        return None
    try:
        d = json.loads(secret)
    except ValueError:
        return None
    return d if isinstance(d, dict) and d.get("access") else None


def _tokens_from(body: dict, old: dict | None = None) -> dict:
    access = body.get("access_token")
    if not isinstance(access, str) or not access:
        raise LoginError("OpenAI не выдал токен", "Попробуй войти ещё раз.", detail=json.dumps(body, ensure_ascii=False)[:500])
    refresh = body.get("refresh_token") or (old or {}).get("refresh")
    exp = body.get("expires_in")
    if isinstance(exp, (int, float)) and exp > 0:
        expires = time.time() + float(exp)
    else:
        expires = float(jwt_claims(access).get("exp") or (time.time() + 3600))
    out = {"access": access, "refresh": refresh, "expires": round(expires), "account_id": account_id(access) or (old or {}).get("account_id")}
    email = jwt_claims(body.get("id_token") or "").get("email") or (old or {}).get("email")
    if email:
        out["email"] = email
    return out


async def _post(http: HttpClient, url: str, *, json_body: dict | None = None, form: dict | None = None) -> tuple[int, dict | str]:
    headers = dict(client_headers())
    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urlencode(form).encode()
    else:
        headers["Content-Type"] = "application/json"
        data = json.dumps(json_body or {}).encode()
    try:
        resp = await http.request("POST", url, headers=headers, body=data)
    except NetError as e:
        raise LoginError("Нет связи с OpenAI", "Проверь интернет (и прокси, если он нужен).", detail=str(e), code="chatgpt_network") from None
    async with resp:
        text = await resp.text()
        try:
            return resp.status, json.loads(text)
        except ValueError:
            return resp.status, text


def _error_code(body) -> str:
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return str(err.get("code") or err.get("type") or "")
        if isinstance(err, str):
            return err
    return ""


# --- вход по коду устройства --------------------------------------------------------------


async def device_start(http: HttpClient, auth_url: str | None = None) -> dict:
    auth_url = auth_url or auth_base()
    status, body = await _post(http, f"{auth_url}/api/accounts/deviceauth/usercode", json_body={"client_id": CLIENT_ID})
    if status == 404:
        raise LoginError("Вход по коду сейчас недоступен", "Войди через браузер — кнопка рядом.", code="chatgpt_no_device")
    if status != 200 or not isinstance(body, dict) or not body.get("device_auth_id"):
        raise LoginError("OpenAI не выдал код входа", "Попробуй ещё раз или войди через браузер.", detail=str(body)[:500])
    try:
        interval = max(1.0, float(body.get("interval") or 5))
    except (TypeError, ValueError):
        interval = 5.0
    return {"device_auth_id": body["device_auth_id"], "user_code": body.get("user_code") or body.get("usercode"),
            "verification_url": f"{auth_url}/codex/device", "interval": interval, "started": time.time()}


async def device_poll(http: HttpClient, device: dict, auth_url: str | None = None) -> dict | None:
    """Один опрос: None — хозяин ещё не ввёл код; иначе {authorization_code, code_verifier}."""
    auth_url = auth_url or auth_base()
    status, body = await _post(http, f"{auth_url}/api/accounts/deviceauth/token",
                               json_body={"device_auth_id": device["device_auth_id"], "user_code": device["user_code"]})
    if status in (403, 404):
        return None
    if status != 200 or not isinstance(body, dict):
        raise LoginError("Вход не подтвердился", "Попробуй ещё раз.", detail=str(body)[:500])
    if not body.get("authorization_code") or not body.get("code_verifier"):
        raise LoginError("OpenAI ответил без кода обмена", "Попробуй ещё раз.", detail=str(body)[:500])
    return {"authorization_code": body["authorization_code"], "code_verifier": body["code_verifier"]}


async def device_wait(http: HttpClient, device: dict, auth_url: str | None = None, *, sleep=asyncio.sleep) -> dict:
    auth_url = auth_url or auth_base()
    deadline = device.get("started", time.time()) + DEVICE_TIMEOUT
    while time.time() < deadline:
        try:
            got = await device_poll(http, device, auth_url)
        except LoginError as e:
            if e.code != "chatgpt_network":
                raise
            got = None  # сеть моргнула — пробуем дальше
        if got:
            return await exchange(http, got["authorization_code"], got["code_verifier"], f"{auth_url}/deviceauth/callback", auth_url)
        await sleep(device.get("interval", 5))
    raise LoginError("Код входа истёк", "Прошло 15 минут. Начни вход заново.", code="chatgpt_expired")


async def exchange(http: HttpClient, code: str, verifier: str, redirect_uri: str, auth_url: str | None = None) -> dict:
    auth_url = auth_url or auth_base()
    status, body = await _post(http, f"{auth_url}/oauth/token", form={
        "grant_type": "authorization_code", "client_id": CLIENT_ID, "code": code, "code_verifier": verifier, "redirect_uri": redirect_uri})
    if status != 200 or not isinstance(body, dict):
        raise LoginError("OpenAI не обменял код на токен", "Начни вход заново.", detail=str(body)[:500])
    return _tokens_from(body)


async def refresh(http: HttpClient, tokens: dict, auth_url: str | None = None) -> dict:
    auth_url = auth_url or auth_base()
    if not tokens.get("refresh"):
        raise LoginError("Вход в ChatGPT устарел", "Войди заново: «Модели» → ChatGPT → «Войти».", code="chatgpt_relogin")
    status, body = await _post(http, f"{auth_url}/oauth/token", form={"grant_type": "refresh_token", "refresh_token": tokens["refresh"], "client_id": CLIENT_ID})
    if status != 200 or not isinstance(body, dict):
        code = _error_code(body)
        raise LoginError("Вход в ChatGPT устарел" if status in (400, 401) else "Не удалось обновить вход в ChatGPT",
                         "Войди заново: «Модели» → ChatGPT → «Войти»." if status in (400, 401) else "Попробуй позже.",
                         detail=f"HTTP {status} {code}".strip(), code="chatgpt_relogin" if status in (400, 401) else "chatgpt_refresh")
    return _tokens_from(body, tokens)


async def ensure_fresh(http: HttpClient, tokens: dict, auth_url: str | None = None, *, force: bool = False) -> tuple[dict, bool]:
    auth_url = auth_url or auth_base()
    if not force and float(tokens.get("expires") or 0) - time.time() > REFRESH_MARGIN:
        return tokens, False
    return await refresh(http, tokens, auth_url), True


# --- вход через браузер (PKCE) -------------------------------------------------------------


def pkce() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def redirect_uri(host: str = "localhost") -> str:
    return f"http://{host}:{CALLBACK_PORT}{CALLBACK_PATH}"


def authorize_url(challenge: str, state: str, auth_url: str | None = None, redirect: str | None = None) -> str:
    auth_url = auth_url or auth_base()
    q = {"response_type": "code", "client_id": CLIENT_ID, "redirect_uri": redirect or redirect_uri(), "scope": SCOPE,
         "code_challenge": challenge, "code_challenge_method": "S256", "state": state, "id_token_add_organizations": "true",
         "codex_cli_simplified_flow": "true", "originator": "bender"}
    return f"{auth_url}/oauth/authorize?{urlencode(q)}"


def parse_callback(text: str) -> tuple[str | None, str | None]:
    """Код и state из того, что вставил хозяин: полный адрес, строка запроса или голый код."""
    text = (text or "").strip()
    if not text:
        return None, None
    if "code=" in text:
        query = urlsplit(text).query if "://" in text else text.lstrip("?")
        q = parse_qs(query)
        return (q.get("code") or [None])[0], (q.get("state") or [None])[0]
    if "#" in text:
        code, _, state = text.partition("#")
        return code or None, state or None
    return text, None


class BrowserLogin:
    """Вход через браузер: страница OpenAI → обратный вызов на localhost:1455 (или код, вставленный руками)."""

    def __init__(self, http: HttpClient, auth_url: str | None = None, port: int = CALLBACK_PORT):
        self.http = http
        self.auth_url = auth_url or auth_base()
        self.port = port
        self.verifier, challenge = pkce()
        self.state = secrets.token_hex(16)
        self.redirect = f"http://localhost:{port}{CALLBACK_PATH}"
        self.url = authorize_url(challenge, self.state, auth_url, self.redirect)
        self.code: asyncio.Future = asyncio.get_running_loop().create_future()
        self.server = None
        self.listening = False

    async def start(self) -> None:
        from ..net.http_server import HttpServer, Router, TextResponse

        r = Router()

        @r.route("GET", CALLBACK_PATH)
        async def cb(req):
            code, state = req.q("code"), req.q("state")
            if code and state == self.state and not self.code.done():
                self.code.set_result(code)
                return TextResponse("<!doctype html><meta charset=utf-8><title>Бендер</title><body style='font:16px system-ui;padding:40px;background:#16181c;color:#e8eaee'>"
                                    "<h2>◉ ◉ Вход выполнен</h2><p>Можно закрыть вкладку и вернуться в Бендер.</p>", content_type="text/html; charset=utf-8")
            return TextResponse("Неверный или устаревший ответ входа. Начни вход заново в Бендере.", status=400)

        self.server = HttpServer(r)
        try:
            await self.server.start("127.0.0.1", self.port)
            self.listening = True
        except OSError:
            self.server = None  # порт занят (например, Codex CLI) — останется вставка руками

    def paste(self, text: str) -> bool:
        code, state = parse_callback(text)
        if not code or (state and state != self.state):
            return False
        if not self.code.done():
            self.code.set_result(code)
        return True

    async def wait(self, timeout: float = 600) -> dict:
        try:
            code = await asyncio.wait_for(asyncio.shield(self.code), timeout)
        except asyncio.TimeoutError:
            raise LoginError("Вход не завершён", "Прошло 10 минут. Начни вход заново.", code="chatgpt_expired") from None
        finally:
            await self.close()
        return await exchange(self.http, code, self.verifier, self.redirect, self.auth_url)

    async def close(self) -> None:
        if self.server is not None:
            await self.server.stop()
            self.server = None


# --- запросы к модели: Responses API -------------------------------------------------------


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return "" if content is None else str(content)


def to_input(messages: list[dict], provider_id: str, model: str = "") -> tuple[str, list[dict]]:
    """Внутренняя история → (instructions, input Responses API).

    В instructions — только первое системное сообщение (постоянный системный промпт). Следующие системные
    вставки (пересказ после сжатия, «ещё N сообщ. не показаны») идут во вход сообщением developer на своём
    месте: иначе каждое сжатие меняло бы instructions и ключ кэша, и провайдер пересчитывал бы весь промпт."""
    system: list[str] = []
    out: list[dict] = []
    for m in messages:
        role = m.get("role")
        if role == "system" and system:
            text = _text(m.get("content"))
            if text:
                out.append({"type": "message", "role": "developer", "content": [{"type": "input_text", "text": text}]})
        elif role == "system":
            system.append(_text(m.get("content")))
        elif role == "user":
            c = m.get("content")
            parts = []
            if isinstance(c, list):
                for p in c:
                    if p.get("type") == "text":
                        parts.append({"type": "input_text", "text": p.get("text", "")})
                    elif p.get("type") == "image":
                        parts.append({"type": "input_image", "image_url": f"data:{p.get('media_type', 'image/png')};base64,{p.get('data', '')}"})
                    elif p.get('type')=='audio':
                        parts.append({'type':'input_audio','input_audio':{'data':p.get('data',''),'format':p.get('format','wav')}})
            else:
                parts.append({"type": "input_text", "text": c or ""})
            out.append({"type": "message", "role": "user", "content": parts})
        elif role == "assistant":
            if m.get("provider_id") == provider_id and (not model or m.get('provider_model') == model):
                # размышления (зашифрованные) — вернуть как есть: так модель продолжает мысль между тулами
                out.extend(b for b in m.get("provider_blocks") or [] if isinstance(b, dict) and b.get("type") == "reasoning")
            text = _text(m.get("content"))
            if text:
                out.append({"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]})
            for c in m.get("tool_calls") or []:
                out.append({"type": "function_call", "call_id": c["id"], "name": c["name"], "arguments": json.dumps(c.get("arguments") or {}, ensure_ascii=False)})
        elif role == "tool":
            out.append({"type": "function_call_output", "call_id": m.get("tool_call_id", ""), "output": _text(m.get("content"))})
    return (system[0] if system else ""), out


def conversation_id(fallback: str) -> str:
    """Постоянный id разговора для заголовка session_id, как у Codex: все шаги одного разговора — один id.
    Случайный id на каждый запрос сервер считает новой сессией, и кэш начала запроса почти не срабатывает.
    Внутри хода — по сессии Бендера (хеш, сам id наружу не уходит), вне хода — по системному промпту."""
    from .cli import TURN
    turn = TURN.get()
    sid = str(getattr(turn, "session_id", "") or "")
    seed = ("bender-session:" + sid) if sid else ("bender-prompt:" + fallback)
    return str(uuid.UUID(hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]))


class ChatGPTAdapter:
    kind = "chatgpt"

    def __init__(self, provider: dict):
        self.p = provider
        self.conversation = ""  # постоянный id разговора для заголовка session_id; задаёт body()

    def url(self) -> str:
        base = (self.p.get("base_url") or backend_base()).rstrip("/")
        if base.endswith("/responses"):
            return base
        if base.endswith("/codex"):
            return base + "/responses"
        return base + "/codex/responses"

    def headers(self, key: str | None) -> dict:
        tokens = unpack(key) or {}
        h = {"Content-Type": "application/json", "accept": "text/event-stream", "OpenAI-Beta": "responses=experimental",
             **client_headers(), "session_id": self.conversation or uuid.uuid4().hex}
        if tokens.get("access"):
            h["Authorization"] = f"Bearer {tokens['access']}"
        if tokens.get("account_id"):
            h["chatgpt-account-id"] = tokens["account_id"]
        h.update(self.p.get("headers") or {})
        return h

    def body(self, model: str, messages: list[dict], tools: list[ToolSpec] | None, params: dict, stream: bool) -> dict:
        from .effort import project
        params = project(self.p,model,params)
        instructions, items = to_input(messages, self.p.get("id", ""), model)
        cache_key = hashlib.sha256(instructions.encode("utf-8")).hexdigest()[:32]
        self.conversation = conversation_id(cache_key)
        b: dict = {"model": model, "store": False, "stream": True, "instructions": instructions or "You are a helpful assistant.",
                   "input": items, "include": ["reasoning.encrypted_content"],
                   # один и тот же системный промпт — один ключ кэша: начало запроса не пересчитывается
                   "prompt_cache_key": cache_key}
        if tools:
            b["tools"] = [{"type": "function", "name": t.name, "description": t.description, "parameters": t.parameters, "strict": False} for t in tools]
            b["tool_choice"] = "auto"
            b["parallel_tool_calls"] = True
        verbosity = params.pop('verbosity', None)
        if verbosity: b['text'] = {'verbosity': verbosity}
        effort = params.pop("reasoning_effort", None)
        summary = params.pop('reasoning_summary', None)
        if effort or summary:
            b['reasoning'] = {}
            if effort: b['reasoning']['effort'] = effort
            if summary and effort != 'none': b['reasoning']['summary'] = summary
        params.pop("max_tokens", None)  # у этого пути нет такого параметра
        from .adapters import RESERVED
        for k, v in params.items():
            if v is not None and k not in RESERVED:
                b[k] = v
        return b

    async def events(self, resp: Response, provider_name: str, model: str) -> AsyncIterator[tuple]:
        idx: dict[str, int] = {}  # id элемента вызова → номер
        streamed: set[int] = set()
        finished = False
        async for e in parse_sse(resp.iter_lines()):
            data = e.data.strip()
            if not data or data == "[DONE]":
                continue
            try:
                ev = json.loads(data)
            except ValueError:
                continue
            t = ev.get("type", "")
            if t == "response.output_text.delta":
                if ev.get("delta"):
                    yield ("text", ev["delta"])
            elif t in ("response.reasoning_summary_text.delta", "response.reasoning_text.delta"):
                if ev.get("delta"):
                    yield ("reasoning", ev["delta"])
            elif t == "response.reasoning_summary_part.done":
                yield ("reasoning", "\n\n")
            elif t == "response.output_item.added":
                item = ev.get("item") or {}
                if item.get("type") == "function_call":
                    i = idx.setdefault(item.get("id") or item.get("call_id") or str(len(idx)), len(idx))
                    yield ("tool", i, item.get("call_id"), item.get("name"), "")
            elif t == "response.function_call_arguments.delta":
                key = ev.get("item_id") or ""
                i = idx.setdefault(key, len(idx))
                streamed.add(i)
                yield ("tool", i, None, None, ev.get("delta") or "")
            elif t == "response.output_item.done":
                item = ev.get("item") or {}
                if item.get("type") == "function_call":
                    i = idx.setdefault(item.get("id") or item.get("call_id") or str(len(idx)), len(idx))
                    yield ("tool", i, item.get("call_id"), item.get("name"), "" if i in streamed else (item.get("arguments") or ""))
                elif item.get("type") == "reasoning" and item.get("encrypted_content"):
                    yield ("block", {k: item[k] for k in ("type", "id", "summary", "encrypted_content") if k in item})
            elif t in ("response.completed", "response.done", "response.incomplete"):
                r = ev.get("response") or {}
                u = r.get("usage") or {}
                if u:
                    yield ("usage", Usage(input=int(u.get("input_tokens") or 0), output=int(u.get("output_tokens") or 0),
                                          cache_read=int((u.get("input_tokens_details") or {}).get("cached_tokens") or 0),
                                          reasoning=int((u.get("output_tokens_details") or {}).get("reasoning_tokens") or 0)))
                if t == "response.incomplete":
                    reason = (r.get("incomplete_details") or {}).get("reason") or ""
                    yield ("finish", "content_filter" if "content" in reason else "length")
                else:
                    yield ("finish", "tool_calls" if idx else "stop")
                finished = True
                break
            elif t in ("error", "response.failed"):
                err = ev.get("error") if t == "error" else ((ev.get("response") or {}).get("error") or {})
                err = err if isinstance(err, dict) else {"message": str(err)}
                msg = str(err.get("message") or err.get("code") or data[:300])
                code = str(err.get("code") or err.get("type") or "")
                raise ProviderError(classify(None, f"{code} {msg}"), provider=provider_name, model=model, message=msg, raw=data[:4000])
        if not finished:
            raise ProviderError("network", provider=provider_name, model=model, message="Поток оборвался до конца ответа (нет response.completed)")


def retry_after_from(body: str) -> float | None:
    """ChatGPT при исчерпании лимита подписки говорит, через сколько он сбросится."""
    try:
        doc = json.loads(body)
    except ValueError:
        return None
    err = doc.get("error") if isinstance(doc, dict) else None
    if isinstance(err, dict):
        for k in ("resets_in_seconds", "retry_after"):
            if isinstance(err.get(k), (int, float)):
                return float(err[k])
        if isinstance(err.get("resets_at"), (int, float)):
            return max(0.0, float(err["resets_at"]) - time.time())
    return None


_ = api_message
