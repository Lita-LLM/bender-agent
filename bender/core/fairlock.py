"""Справедливый рекурсивный замок: кто ждёт дольше, тот и берёт.

Обычный threading.RLock не выдаёт замок по очереди: поток, который только что его отпустил, берёт его снова
раньше, чем успеет проснуться ждущий. Рабочий поток, который пачку за пачкой берёт замок базы (индекс памяти,
уборка, перенос), так мог не пускать главный цикл десятки секунд — а пока цикл ждёт, молчит всё: Telegram,
пульт, сердце. Замер на Python 3.11, 3.12 и 3.13: 8–25 с ожидания при пачках в 50–500 мс без пауз.

Здесь замок при освобождении сразу передаётся первому в очереди; кто его отпустил, при новом взятии встаёт
в хвост. Ожидание любого потока — не дольше пачек тех, кто был перед ним в очереди.

Рекурсия, `with`, acquire(blocking, timeout) и release() — как у RLock. Для диагностики зависаний замок помнит,
кто его держит и с какого момента (holder), и сколько потоков ждут (waiting)."""
from __future__ import annotations

import threading
import time
from collections import deque


class FairRLock:
    __slots__ = ('_mutex', '_owner', '_depth', '_queue', '_since')

    def __init__(self):
        self._mutex = threading.Lock()  # только поля замка, и ненадолго
        self._owner = None              # номер потока-хозяина (threading.get_ident)
        self._depth = 0
        self._queue = deque()           # [номер потока, затвор] — по порядку прихода
        self._since = 0.0               # monotonic: с какого момента держит нынешний хозяин

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        me = threading.get_ident()
        with self._mutex:
            if self._owner == me:
                self._depth += 1
                return True
            if self._owner is None and not self._queue:
                self._owner, self._depth, self._since = me, 1, time.monotonic()
                return True
            if not blocking:
                return False
            gate = threading.Lock()
            gate.acquire()  # откроет тот, кто будет отпускать замок
            entry = [me, gate]
            self._queue.append(entry)
        if timeout is None or timeout < 0:
            gate.acquire()
            return True
        if gate.acquire(True, timeout):
            return True
        with self._mutex:
            if entry in self._queue:  # не дождались — выходим из очереди
                self._queue.remove(entry)
                return False
        gate.acquire()  # замок передали за мгновение до конца срока: он уже наш
        return True

    def release(self) -> None:
        me = threading.get_ident()
        with self._mutex:
            if self._owner != me:
                raise RuntimeError('cannot release un-acquired lock')
            self._depth -= 1
            if self._depth:
                return
            if self._queue:
                ident, gate = self._queue.popleft()
                self._owner, self._depth, self._since = ident, 1, time.monotonic()
                gate.release()
            else:
                self._owner = None

    __enter__ = acquire

    def __exit__(self, *exc) -> None:
        self.release()

    def holder(self) -> tuple[int | None, float]:
        """(номер потока, сколько секунд держит) — без замка самого замка: диагностика не должна его ждать."""
        owner, since = self._owner, self._since
        return (owner, max(0.0, time.monotonic() - since)) if owner is not None else (None, 0.0)

    def waiting(self) -> int:
        return len(self._queue)

    def _is_owned(self) -> bool:
        return self._owner == threading.get_ident()
