"""Адаптеры API: внутренний формат ↔ OpenAI Chat Completions и Anthropic Messages.

Адаптер собирает запрос (адрес, заголовки, тело) и разбирает поток ответа в события:

    ("text", str)                       кусок ответа
    ("reasoning", str)                  кусок размышлений
    ("tool", index, id, name, args)     кусок вызова тула (аргументы приходят частями)
    ("usage", Usage)                    расход токенов
    ("finish", str)                     причина окончания
    ("block", dict)                     сырой блок, который надо вернуть провайдеру как есть
"""

from __future__ import annotations

import json
from typing import AsyncIterator

from ..net.http_client import Response
from ..net.sse import parse_sse
from .errors import ProviderError, api_message, classify
from .types import ToolSpec, Usage



# Поля тела, которые задаёт сам адаптер: параметры кандидата их не перезаписывают (иначе платная модель прошла бы мимо проверки «бесплатной»).
RESERVED = frozenset({"model", "messages", "input", "instructions", "system", "tools", "stream", "stream_options"})

def _text_of(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text")
    return "" if content is None else str(content)


class ThinkSplitter:
    """Локальные модели (DeepSeek-R1 и др. через llama.cpp) пишут размышления прямо в текст: <think>…</think>.
    Разделяем на лету, учитывая, что тег может разорваться между кусками."""

    OPEN, CLOSE = "<think>", "</think>"

    def __init__(self):
        self.inside = False
        self.buf = ""
        self.started = False

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        self.buf += chunk
        while self.buf:
            if not self.inside:
                if not self.started:
                    stripped = self.buf.lstrip()
                    if stripped and not self.OPEN.startswith(stripped[: len(self.OPEN)]) and not stripped.startswith(self.OPEN):
                        self.started = True  # текст начался не с <think> — дальше обычный текст
                    elif stripped.startswith(self.OPEN):
                        self.inside = True
                        self.started = True
                        self.buf = stripped[len(self.OPEN):]
                        continue
                    else:
                        return out  # ждём, вдруг это начало тега
                out.append(("text", self.buf))
                self.buf = ""
            else:
                idx = self.buf.find(self.CLOSE)
                if idx >= 0:
                    if idx:
                        out.append(("reasoning", self.buf[:idx]))
                    self.buf = self.buf[idx + len(self.CLOSE):].lstrip("\n")
                    self.inside = False
                    continue
                # держим хвост, который может оказаться началом </think>
                keep = 0
                for k in range(1, len(self.CLOSE)):
                    if self.buf.endswith(self.CLOSE[:k]):
                        keep = k
                emit = self.buf[: len(self.buf) - keep]
                if emit:
                    out.append(("reasoning", emit))
                self.buf = self.buf[len(self.buf) - keep:]
                return out
        return out

    def flush(self) -> list[tuple[str, str]]:
        if not self.buf:
            return []
        kind = "reasoning" if self.inside else "text"
        b, self.buf = self.buf, ""
        return [(kind, b)]


# --- OpenAI Chat Completions ------------------------------------------------------------


class OpenAIAdapter:
    kind = "openai"

    def __init__(self, provider: dict):
        self.p = provider

    def url(self) -> str:
        return self.p["base_url"].rstrip("/") + "/chat/completions"

    def headers(self, key: str | None) -> dict:
        h = {"Content-Type": "application/json", "Accept": "text/event-stream, application/json"}
        if key:
            h["Authorization"] = f"Bearer {key}"
        if "openrouter.ai" in self.p.get("base_url", ""):
            from .. import PROJECT_URL
            if PROJECT_URL:
                h["HTTP-Referer"] = PROJECT_URL
            h["X-Title"] = "Bender"
        h.update(self.p.get("headers") or {})
        return h

    def messages(self, messages: list[dict]) -> list[dict]:
        out = []
        for m in messages:
            role = m.get("role")
            if role == "system":
                out.append({"role": "system", "content": _text_of(m.get("content"))})
            elif role == "user":
                c = m.get("content")
                if isinstance(c, list):
                    parts = []
                    for p in c:
                        if p.get("type") == "text":
                            parts.append({"type": "text", "text": p.get("text", "")})
                        elif p.get("type") == "image":
                            parts.append({"type": "image_url", "image_url": {"url": f"data:{p.get('media_type', 'image/png')};base64,{p.get('data', '')}"}})
                        elif p.get('type')=='audio':
                            parts.append({'type':'input_audio','input_audio':{'data':p.get('data',''),'format':p.get('format','wav')}})
                    out.append({"role": "user", "content": '\n\n'.join(p['text'] for p in parts) if all(p.get('type')=='text' for p in parts) else parts})
                else:
                    out.append({"role": "user", "content": c or ""})
            elif role == "assistant":
                msg: dict = {"role": "assistant", "content": _text_of(m.get("content")) or None}
                calls = m.get("tool_calls") or []
                if calls:
                    msg["tool_calls"] = [
                        {"id": c["id"], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c.get("arguments") or {}, ensure_ascii=False)}}
                        for c in calls
                    ]
                if msg["content"] is None and not calls:
                    msg["content"] = ""
                out.append(msg)
            elif role == "tool":
                out.append({"role": "tool", "tool_call_id": m.get("tool_call_id", ""), "content": _text_of(m.get("content"))})
        return out

    def body(self, model: str, messages: list[dict], tools: list[ToolSpec] | None, params: dict, stream: bool) -> dict:
        from .effort import project
        params=project(self.p,model,params)
        b: dict = {"model": model, "messages": self.messages(messages), "stream": stream}
        if stream and not self.p.get("no_stream_options"):
            b["stream_options"] = {"include_usage": True}
        if tools:
            b["tools"] = [t.openai() for t in tools]
        if "openrouter.ai" in self.p.get("base_url", ""):
            b["usage"] = {"include": True}  # OpenRouter вернёт стоимость
        for k, v in (params or {}).items():
            if v is not None and k not in RESERVED:
                b[k] = v
        return b

    async def events(self, resp: Response, provider_name: str, model: str) -> AsyncIterator[tuple]:
        ctype = (resp.headers.get("content-type") or "").lower()
        if "text/event-stream" not in ctype:
            data = json.loads(await resp.text())
            for ev in self._full(data, provider_name, model):
                yield ev
            return
        splitter = ThinkSplitter()
        finished = False
        async for e in parse_sse(resp.iter_lines()):
            if e.data.strip() == "[DONE]":
                finished = True
                break
            try:
                chunk = json.loads(e.data)
            except ValueError:
                continue
            if isinstance(chunk, dict) and chunk.get("error"):
                err = chunk["error"]
                msg = err.get("message", "") if isinstance(err, dict) else str(err)
                code = err.get("code") if isinstance(err, dict) else None
                status = code if isinstance(code, int) else None
                raise ProviderError(classify(status, msg), provider=provider_name, model=model, status=status, message=msg, raw=e.data)
            if chunk.get("usage"):
                yield ("usage", _openai_usage(chunk["usage"]))
            for ch in chunk.get("choices") or []:
                delta = ch.get("delta") or {}
                rc = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(rc, str) and rc:
                    yield ("reasoning", rc)
                c = delta.get("content")
                if isinstance(c, str) and c:
                    for kind, piece in splitter.feed(c):
                        yield (kind, piece)
                for tc in delta.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    yield ("tool", tc.get("index", 0), tc.get("id"), fn.get("name"), fn.get("arguments") or "")
                if ch.get("finish_reason"):
                    finished = True
                    yield ("finish", ch["finish_reason"])
        for kind, piece in splitter.flush():
            yield (kind, piece)
        if not finished:
            raise ProviderError("network", provider=provider_name, model=model, message="Поток оборвался до конца ответа (нет finish_reason и [DONE])")

    def _full(self, data: dict, provider_name: str, model: str):
        if data.get("error"):
            msg = api_message(json.dumps(data))
            raise ProviderError(classify(None, msg), provider=provider_name, model=model, message=msg, raw=json.dumps(data)[:4000])
        if data.get("usage"):
            yield ("usage", _openai_usage(data["usage"]))
        for ch in data.get("choices") or []:
            msg = ch.get("message") or {}
            rc = msg.get("reasoning_content") or msg.get("reasoning")
            if isinstance(rc, str) and rc:
                yield ("reasoning", rc)
            c = msg.get("content")
            if isinstance(c, str) and c:
                splitter = ThinkSplitter()
                for kind, piece in splitter.feed(c) + splitter.flush():
                    yield (kind, piece)
            for i, tc in enumerate(msg.get("tool_calls") or []):
                fn = tc.get("function") or {}
                yield ("tool", i, tc.get("id"), fn.get("name"), fn.get("arguments") or "")
            if ch.get("finish_reason"):
                yield ("finish", ch["finish_reason"])


def _openai_usage(u: dict) -> Usage:
    details = u.get("prompt_tokens_details") or {}
    cdetails = u.get("completion_tokens_details") or {}
    cost = u.get("cost")
    return Usage(
        input=int(u.get("prompt_tokens") or 0),
        output=int(u.get("completion_tokens") or 0),
        cache_read=int(details.get("cached_tokens") or u.get("cache_read_input_tokens") or 0),
        reasoning=int(cdetails.get("reasoning_tokens") or 0),
        cost=float(cost) if isinstance(cost, (int, float)) else None,
    )


# --- Anthropic Messages -----------------------------------------------------------------


class AnthropicAdapter:
    kind = "anthropic"
    VERSION = "2023-06-01"

    def __init__(self, provider: dict):
        self.p = provider

    def url(self) -> str:
        return self.p["base_url"].rstrip("/") + "/messages"

    def headers(self, key: str | None) -> dict:
        h = {"Content-Type": "application/json", "anthropic-version": self.VERSION, "Accept": "text/event-stream, application/json"}
        if key:
            h["x-api-key"] = key
        h.update(self.p.get("headers") or {})
        return h

    def convert(self, messages: list[dict], cache: bool) -> tuple[list[dict], list[dict]]:
        system: list[dict] = []
        out: list[dict] = []

        def push(role: str, blocks: list[dict]):
            if not blocks:
                return
            if out and out[-1]["role"] == role:
                out[-1]["content"].extend(blocks)
            else:
                out.append({"role": role, "content": list(blocks)})

        for m in messages:
            role = m.get("role")
            if role == "system":
                t = _text_of(m.get("content"))
                if t:
                    system.append({"type": "text", "text": t})
            elif role == "user":
                c = m.get("content")
                blocks = []
                if isinstance(c, list):
                    for p in c:
                        if p.get("type") == "text" and p.get("text"):
                            blocks.append({"type": "text", "text": p["text"]})
                        elif p.get("type") == "image":
                            blocks.append({"type": "image", "source": {"type": "base64", "media_type": p.get("media_type", "image/png"), "data": p.get("data", "")}})
                elif c:
                    blocks.append({"type": "text", "text": c})
                push("user", blocks or [{"type": "text", "text": "…"}])
            elif role == "assistant":
                blocks = []
                if m.get("provider_blocks"):
                    blocks.extend(b for b in m["provider_blocks"] if isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"))
                t = _text_of(m.get("content"))
                if t:
                    blocks.append({"type": "text", "text": t})
                for c in m.get("tool_calls") or []:
                    blocks.append({"type": "tool_use", "id": c["id"], "name": c["name"], "input": c.get("arguments") or {}})
                push("assistant", blocks or [{"type": "text", "text": "…"}])
            elif role == "tool":
                block = {"type": "tool_result", "tool_use_id": m.get("tool_call_id", ""), "content": _text_of(m.get("content"))}
                if m.get("is_error"):
                    block["is_error"] = True
                push("user", [block])
        if out and out[0]["role"] != "user":
            out.insert(0, {"role": "user", "content": [{"type": "text", "text": "(начало разговора)"}]})
        if cache:
            if system:
                system[-1]["cache_control"] = {"type": "ephemeral"}
            # последнее сообщение человека — точка кэша для следующего хода
            for msg in reversed(out):
                if msg["role"] == "user" and msg["content"]:
                    msg["content"][-1] = dict(msg["content"][-1], cache_control={"type": "ephemeral"})
                    break
        return system, out

    def body(self, model: str, messages: list[dict], tools: list[ToolSpec] | None, params: dict, stream: bool) -> dict:
        from .effort import project
        params = project(self.p,model,params)
        cache = params.pop("cache", True)
        system, msgs = self.convert(messages, cache)
        b: dict = {"model": model, "messages": msgs, "max_tokens": int(params.pop("max_tokens", 0) or 16000), "stream": stream}
        if system:
            b["system"] = system
        if tools:
            specs = [t.anthropic() for t in tools]
            if cache:
                specs[-1] = dict(specs[-1], cache_control={"type": "ephemeral"})
            b["tools"] = specs
        for k, v in params.items():
            if v is not None and k not in RESERVED:
                b[k] = v
        return b

    async def events(self, resp: Response, provider_name: str, model: str) -> AsyncIterator[tuple]:
        ctype = (resp.headers.get("content-type") or "").lower()
        if "text/event-stream" not in ctype:
            data = json.loads(await resp.text())
            for ev in self._full(data, provider_name, model):
                yield ev
            return
        blocks: dict[int, dict] = {}
        usage = Usage()
        tool_index = {}
        stopped = False
        async for e in parse_sse(resp.iter_lines()):
            try:
                d = json.loads(e.data) if e.data else {}
            except ValueError:
                continue
            t = d.get("type") or e.event
            if t == "message_start":
                u = (d.get("message") or {}).get("usage") or {}
                usage.input = int(u.get("input_tokens") or 0)
                usage.cache_read = int(u.get("cache_read_input_tokens") or 0)
                usage.cache_write = int(u.get("cache_creation_input_tokens") or 0)
            elif t == "content_block_start":
                i = d.get("index", 0)
                cb = dict(d.get("content_block") or {})
                blocks[i] = cb
                if cb.get("type") == "tool_use":
                    tool_index[i] = len(tool_index)
                    yield ("tool", tool_index[i], cb.get("id"), cb.get("name"), "")
                elif cb.get("type") == "text" and cb.get("text"):
                    yield ("text", cb["text"])
            elif t == "content_block_delta":
                i = d.get("index", 0)
                delta = d.get("delta") or {}
                dt = delta.get("type")
                if dt == "text_delta":
                    yield ("text", delta.get("text", ""))
                elif dt == "thinking_delta":
                    blocks.setdefault(i, {"type": "thinking", "thinking": ""})
                    blocks[i]["thinking"] = blocks[i].get("thinking", "") + delta.get("thinking", "")
                    yield ("reasoning", delta.get("thinking", ""))
                elif dt == "signature_delta":
                    blocks.setdefault(i, {"type": "thinking", "thinking": ""})
                    blocks[i]["signature"] = blocks[i].get("signature", "") + delta.get("signature", "")
                elif dt == "input_json_delta":
                    yield ("tool", tool_index.get(i, 0), None, None, delta.get("partial_json", ""))
            elif t == "content_block_stop":
                cb = blocks.get(d.get("index", 0)) or {}
                if cb.get("type") in ("thinking", "redacted_thinking"):
                    yield ("block", cb)
            elif t == "message_delta":
                u = d.get("usage") or {}
                if u.get("output_tokens") is not None:
                    usage.output = int(u["output_tokens"])
                sr = (d.get("delta") or {}).get("stop_reason")
                if sr:
                    yield ("finish", _anthropic_finish(sr))
            elif t == "error":
                err = d.get("error") or {}
                msg = err.get("message", "")
                status = {"overloaded_error": 529, "rate_limit_error": 429, "api_error": 500}.get(err.get("type", ""))
                raise ProviderError(classify(status, msg + " " + err.get("type", "")), provider=provider_name, model=model, status=status, message=msg, raw=e.data)
            elif t == "message_stop":
                stopped = True
                break
        yield ("usage", usage)
        if not stopped:
            raise ProviderError("network", provider=provider_name, model=model, message="Поток оборвался до конца ответа (нет message_stop)")

    def _full(self, data: dict, provider_name: str, model: str):
        if data.get("type") == "error":
            msg = api_message(json.dumps(data))
            raise ProviderError(classify(None, msg), provider=provider_name, model=model, message=msg)
        u = data.get("usage") or {}
        idx = 0
        for b in data.get("content") or []:
            if b.get("type") == "text":
                yield ("text", b.get("text", ""))
            elif b.get("type") == "thinking":
                yield ("reasoning", b.get("thinking", ""))
                yield ("block", b)
            elif b.get("type") == "redacted_thinking":
                yield ("block", b)
            elif b.get("type") == "tool_use":
                yield ("tool", idx, b.get("id"), b.get("name"), json.dumps(b.get("input") or {}, ensure_ascii=False))
                idx += 1
        if data.get("stop_reason"):
            yield ("finish", _anthropic_finish(data["stop_reason"]))
        yield ("usage", Usage(input=int(u.get("input_tokens") or 0), output=int(u.get("output_tokens") or 0),
                              cache_read=int(u.get("cache_read_input_tokens") or 0), cache_write=int(u.get("cache_creation_input_tokens") or 0)))


def _anthropic_finish(sr: str) -> str:
    return {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length", "stop_sequence": "stop", "refusal": "refused", "pause_turn": "pause"}.get(sr, sr)


def adapter_for(provider: dict):
    kind = provider.get("kind", "openai")
    if kind == "anthropic":
        return AnthropicAdapter(provider)
    if kind == "chatgpt":
        from .chatgpt import ChatGPTAdapter

        return ChatGPTAdapter(provider)
    return OpenAIAdapter(provider)
