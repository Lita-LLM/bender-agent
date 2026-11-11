"""Шина событий: всё, что происходит, — событие.

Подписчики двух видов: асинхронные очереди (вкладки пульта по SSE, каналы) и обычные
функции внутри процесса. Медленная вкладка не тормозит остальных: её очередь просто
теряет старые события, а вкладка перезапрашивает состояние.
"""

from __future__ import annotations

import asyncio
import itertools
import time
from typing import Callable


class EventBus:
    def __init__(self):
        self._queues: set[asyncio.Queue] = set()
        self._listeners: list[Callable[[dict], None]] = []
        self._loop: asyncio.AbstractEventLoop | None = None
        self._seq = itertools.count(1)

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscribe(self, maxsize: int = 1000) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._queues.discard(q)

    def listen(self, fn: Callable[[dict], None]) -> None:
        self._listeners.append(fn)

    def publish(self, event: dict) -> None:
        """Опубликовать. Из чужого потока (перенос, тулы в потоке) — через цикл событий."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                running = asyncio.get_running_loop()
            except RuntimeError:
                running = None
            if running is not loop:
                loop.call_soon_threadsafe(self._publish, event)
                return
        self._publish(event)

    def _publish(self, event: dict) -> None:
        event = dict(event)
        event.setdefault("ts", time.time())
        event["seq"] = next(self._seq)
        for fn in list(self._listeners):
            try:
                fn(event)
            except Exception:  # noqa: BLE001 — слушатель не должен ронять шину
                pass
        for q in list(self._queues):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait({"type": "overflow", "seq": event["seq"]})
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    pass

    def publish_threadsafe(self, event: dict) -> None:
        self.publish(event)
