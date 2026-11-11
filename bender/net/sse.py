"""Разбор потока Server-Sent Events (так модели присылают ответ кусками)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import AsyncIterator


@dataclass
class Event:
    event: str
    data: str
    id: str | None = None


async def parse_sse(lines: AsyncIterator[str]) -> AsyncIterator[Event]:
    """События из строк потока. Комментарии (`: ping`) пропускаются, многострочный data склеивается."""
    event = ""
    data: list[str] = []
    ev_id = None
    size = 0
    async for line in lines:
        if len(line)>1024*1024:raise ValueError("Слишком длинная строка SSE")
        if line == "":
            if data or event:
                yield Event(event or "message", "\n".join(data), ev_id)
            event, data, ev_id = "", [], None
            size=0
            continue
        if line.startswith(":"):
            continue
        name, sep, value = line.partition(":")
        if sep and value.startswith(" "):
            value = value[1:]
        if name == "data":
            size+=len(value)
            if size>1024*1024 or len(data)>=4096:raise ValueError("Слишком большое событие SSE")
            data.append(value)
        elif name == "event":
            event = value
        elif name == "id":
            ev_id = value
    if data:
        yield Event(event or "message", "\n".join(data), ev_id)
