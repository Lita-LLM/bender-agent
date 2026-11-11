"""Общие типы Коммутатора: сообщения, тулы, результат вызова модели.

Внутренний формат сообщений не привязан к провайдеру:

    {"role": "system" | "user" | "assistant" | "tool",
     "content": str | [{"type": "text", "text": …}, {"type": "image", "media_type": …, "data": base64}],
     # у ассистента:
     "tool_calls": [{"id": …, "name": …, "arguments": {…}}],
     "reasoning": "…",                 # размышления (для показа)
     "provider_blocks": [...],         # сырые блоки провайдера, которые надо вернуть как есть (подписи размышлений Anthropic)
     # у результата тула:
     "tool_call_id": …, "name": …}

Адаптеры переводят его в формат OpenAI или Anthropic и обратно.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict  # JSON Schema аргументов

    def openai(self) -> dict:
        return {"type": "function", "function": {"name": self.name, "description": self.description, "parameters": self.parameters}}

    def anthropic(self) -> dict:
        return {"name": self.name, "description": self.description, "input_schema": self.parameters}


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict
    raw_arguments: str = ""
    bad_json: bool = False  # модель прислала аргументы, которые не разобрались как JSON


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    reasoning: int = 0
    cost: float | None = None  # если провайдер сам сообщил стоимость

    def add(self, other: "Usage") -> None:
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write
        self.reasoning += other.reasoning
        if other.cost is not None:
            self.cost = (self.cost or 0.0) + other.cost

    def to_dict(self) -> dict:
        d = {"input": self.input, "output": self.output}
        for k in ("cache_read", "cache_write", "reasoning"):
            if getattr(self, k):
                d[k] = getattr(self, k)
        if self.cost is not None:
            d["cost"] = self.cost
        return d


@dataclass
class Result:
    text: str = ""
    reasoning: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish: str = ""  # stop | tool_calls | length | content_filter | …
    provider_id: str = ""
    provider_name: str = ""
    model: str = ""
    cost: float = 0.0  # итоговая стоимость (своя оценка или от провайдера)
    provider_blocks: list = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)  # кто пробовался и чем кончилось
    notice: str | None = None  # «ответила запасная модель X, потому что Y»
    seconds: float = 0.0

    def assistant_message(self) -> dict:
        msg: dict = {"role": "assistant", "content": self.text}
        if self.tool_calls:
            msg["tool_calls"] = [{"id": c.id, "name": c.name, "arguments": c.arguments} for c in self.tool_calls]
        if self.reasoning:
            msg["reasoning"] = self.reasoning
        if self.provider_blocks:
            msg["provider_blocks"] = self.provider_blocks
            msg["provider_id"] = self.provider_id
            msg["provider_model"] = self.model
        return msg
