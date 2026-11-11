"""Текстовый протокол тулов — для моделей, которые не умеют вызывать тулы сами (старые и маленькие
локальные модели, серверы без шаблона function calling).

Формат — как у Hermes и Qwen, на нём обучено большинство открытых моделей:

    <tool_call>
    {"name": "read_file", "arguments": {"path": "заметка.txt"}}
    </tool_call>

Результат возвращается сообщением хозяина: <tool_response name="read_file">…</tool_response>.

Внутри Бендера всё остаётся как обычно (tool_calls у ассистента, role=tool у результатов) —
переводится только то, что уходит в модель и приходит из неё. Поэтому протокол можно менять
на лету: та же история уходит одной модели нативно, другой — текстом.

Когда включается: параметр роли `tool_protocol: "text"`; или сам — если каталог провайдера
говорит, что тулов у модели нет, или провайдер ответил «tools not supported».
"""

from __future__ import annotations

import json
import re
import secrets

from ..core import json5
from .types import ToolCall, ToolSpec

OPEN, CLOSE = "<tool_call>", "</tool_call>"

PROTOCOL = """# Как вызывать тулы

Ты вызываешь тулы текстом. Чтобы вызвать тул, напиши блок (JSON внутри — строго один объект):

<tool_call>
{"name": "имя_тула", "arguments": {"параметр": "значение"}}
</tool_call>

Можно несколько блоков подряд. После блоков ничего не пиши и не придумывай результат — он придёт
следующим сообщением в <tool_response>. Если тул не нужен — просто отвечай текстом.

Доступные тулы:
"""

# признаки ответа «эта модель не умеет тулы» у разных серверов (Ollama, llama.cpp, vLLM, OpenRouter, LM Studio…)
UNSUPPORTED = re.compile(
    r"(does not support tools|tools? (are|is) not supported|not support(ed)? (for )?(tool|function)|"
    r"tool[_ ]?(use|calling|choice)[^.]{0,40}(not|un)support|function calling[^.]{0,40}(not|un)support|"
    r"no endpoints found that support tool|--jinja|unknown (field|parameter)[^.]{0,20}tool|"
    r"unrecognized request argument[^.]{0,30}tool|extra inputs are not permitted[^.]{0,40}tool)",
    re.I,
)


def unsupported(message: str) -> bool:
    return bool(UNSUPPORTED.search(message or ""))


def describe_tools(tools: list[ToolSpec]) -> str:
    lines = []
    for t in tools:
        props = (t.parameters or {}).get("properties") or {}
        req = set((t.parameters or {}).get("required") or [])
        args = ", ".join(f"{k}{'' if k in req else '?'}: {v.get('type', 'any')}" + (f" — {v['description']}" if v.get("description") else "")
                         for k, v in props.items())
        lines.append(f"- {t.name}({args})\n  {t.description.strip()}")
    return "\n".join(lines)


def _text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return str(content or "")


def encode(messages: list[dict], tools: list[ToolSpec]) -> list[dict]:
    """История во внутреннем формате → история для модели без тулов."""
    proto = PROTOCOL + describe_tools(tools)
    out: list[dict] = []
    has_system = any(m.get("role") == "system" for m in messages)
    if not has_system:
        out.append({"role": "system", "content": proto})
    for m in messages:
        role = m.get("role")
        if role == "system":
            out.append({"role": "system", "content": _text(m.get("content")).rstrip() + "\n\n---\n\n" + proto})
        elif role == "assistant":
            text = _text(m.get("content")).strip()
            blocks = [f"{OPEN}\n{json.dumps({'name': c.get('name'), 'arguments': c.get('arguments') or {}}, ensure_ascii=False)}\n{CLOSE}"
                      for c in m.get("tool_calls") or []]
            out.append({"role": "assistant", "content": "\n".join(([text] if text else []) + blocks)})
        elif role == "tool":
            body = _text(m.get("content"))
            name = m.get("name") or ""
            piece = f'<tool_response name="{name}">\n{body}\n</tool_response>'
            if out and out[-1]["role"] == "user" and out[-1].get("_tool"):
                out[-1]["content"] += "\n" + piece
            else:
                out.append({"role": "user", "content": piece, "_tool": True})
        else:
            content = m.get("content")
            if out and out[-1]["role"] == "user" and isinstance(content, str):
                out[-1]["content"] += "\n\n" + content  # строгие шаблоны не любят два сообщения хозяина подряд
            else:
                out.append({"role": role, "content": content})
    for m in out:
        m.pop("_tool", None)
    return out


_BLOCK = re.compile(re.escape(OPEN) + r"\s*(.*?)\s*(?:" + re.escape(CLOSE) + r"|\Z)", re.S)


def _parse_obj(raw: str):
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\s*|\s*```$", "", raw)
    for parse in (json.loads, json5.loads):
        try:
            return parse(raw)
        except (ValueError,RecursionError):
            continue
    m = re.search(r"\{.*\}", raw, re.S)
    if m and m.group(0) != raw:
        return _parse_obj(m.group(0))
    return None


def decode(text: str) -> tuple[str, list[ToolCall]]:
    """Ответ модели → (текст без блоков, вызовы тулов)."""
    calls: list[ToolCall] = []
    if OPEN not in text:
        return text, calls
    for m in _BLOCK.finditer(text):
        raw = m.group(1)
        obj = _parse_obj(raw)
        cid = "txt_" + secrets.token_hex(4)
        if not isinstance(obj, dict) or not obj.get("name"):
            calls.append(ToolCall(id=cid, name=str(obj.get("name") if isinstance(obj, dict) else "") or "?", arguments={}, raw_arguments=raw, bad_json=True))
            continue
        args = obj.get("arguments", obj.get("parameters", obj.get("args", {})))
        bad = False
        if isinstance(args, str):
            parsed = _parse_obj(args)
            args, bad = (parsed, False) if isinstance(parsed, dict) else ({}, True)
        if not isinstance(args, dict):
            args, bad = {}, True
        calls.append(ToolCall(id=cid, name=str(obj["name"]), arguments=args, raw_arguments=json.dumps(args, ensure_ascii=False), bad_json=bad))
    clean = _BLOCK.sub("", text).strip()
    return clean, calls


class Hider:
    """Прячет блоки <tool_call> из потока текста, который видит человек (они станут карточками тулов)."""

    def __init__(self):
        self.buf = ""
        self.inside = False

    def feed(self, chunk: str) -> str:
        self.buf += chunk
        out = []
        while self.buf:
            if self.inside:
                i = self.buf.find(CLOSE)
                if i < 0:
                    self.buf = self.buf[-(len(CLOSE) - 1):]
                    break
                self.buf = self.buf[i + len(CLOSE):]
                self.inside = False
            else:
                i = self.buf.find(OPEN)
                if i >= 0:
                    out.append(self.buf[:i])
                    self.buf = self.buf[i + len(OPEN):]
                    self.inside = True
                    continue
                # хвост может оказаться началом "<tool_call>" — придержать
                keep = 0
                for k in range(min(len(OPEN) - 1, len(self.buf)), 0, -1):
                    if OPEN.startswith(self.buf[-k:]):
                        keep = k
                        break
                out.append(self.buf[:len(self.buf) - keep])
                self.buf = self.buf[len(self.buf) - keep:]
                break
        return "".join(out)

    def flush(self) -> str:
        rest = "" if self.inside else self.buf
        self.buf = ""
        return rest
