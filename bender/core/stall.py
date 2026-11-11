"""Сторож цикла: замечает, что главный цикл Бендера перестал отвечать, и запоминает, где он стоял.

Пока цикл занят чем-то блокирующим (замок базы, тяжёлый расчёт без потока), молчит всё сразу: Telegram,
пульт, сердце. Потом всё срабатывает разом. Отдельный поток раз в секунду стучится в цикл; не достучался
дольше STALL секунд — снимает стек главного потока. Когда цикл ожил — запись в Бортжурнал и полный стек
в local/stalls.txt.

Если цикл не ожил совсем (бесконечный цикл, тяжёлое регулярное выражение), запись «после» не появится никогда,
а процесс так и будет держать процессор. На этот случай — таймер faulthandler: цикл переводит его каждые
несколько секунд; не перевёл HANG секунд — стеки всех потоков пишутся в local/hang.txt и процесс выходит,
Сторож или systemd поднимают его заново. Таймер живёт в потоке C и срабатывает, даже когда занят GIL."""
from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path

STALL = 5.0      # с какого простоя это зависание, а не просто медленный шаг
INTERVAL = 1.0
KEEP = 20        # сколько последних зависаний помнить в памяти
HANG = 180.0     # цикл молчит столько — процесс выходит сам (BENDER_HANG_SECONDS; 0 — не выходить)
REARM = 10.0
SAMPLES = 30   # сколько последних ответов цикла помнить: полминуты при опросе раз в секунду
BUSY = 1.0     # цикл отвечает на стук дольше этого — он перегружен


def hang_limit() -> float:
    try:
        return max(0.0, float(os.environ.get('BENDER_HANG_SECONDS', HANG)))
    except ValueError:
        return HANG


def place(frame, limit=40) -> list[str]:
    """Стек кадра сверху вниз, как в traceback: последняя строка — где цикл стоит прямо сейчас."""
    return [f"{Path(f.filename).name}:{f.lineno} {f.name}" + (f" — {f.line.strip()}" if f.line else "") for f in traceback.extract_stack(frame, limit=limit)]


def ours(stack: list[str]) -> str:
    """Самое глубокое место в коде Бендера: по нему видно, чья это блокировка."""
    for line in reversed(stack):
        if not line.split(':', 1)[0] in ('threading.py', 'selectors.py', 'base_events.py', 'events.py', 'runners.py', 'tasks.py'):
            return line
    return stack[-1] if stack else 'неизвестно'


class LoopWatch:
    def __init__(self, loop, on_stall, path: Path | None = None, *, stall: float = STALL, interval: float = INTERVAL):
        self.loop = loop
        self.on_stall = on_stall
        self.path = path
        self.stall = stall
        self.interval = interval
        self.ident = threading.get_ident()  # создаётся в потоке цикла
        self.seen = time.monotonic()
        self.current = None
        self.recent = deque(maxlen=KEEP)
        self.delays = deque(maxlen=SAMPLES)  # за сколько цикл откликнулся на каждый стук
        self._stop = threading.Event()
        self._thread = None
        self.hang = hang_limit()
        self.hang_path = path.with_name('hang.txt') if path else None
        self._hang_file = None
        self._armed = 0.0

    def previous_hang(self) -> str:
        """Прошлый процесс вышел по таймеру зависания: забрать его стеки (файл переносится в hang-last.txt)."""
        if not self.hang_path:
            return ''
        try:
            text = self.hang_path.read_text(encoding='utf-8', errors='replace')
            if not text.strip():
                return ''
            os.replace(self.hang_path, self.hang_path.with_name('hang-last.txt'))
            return text
        except OSError:
            return ''

    def _arm(self) -> None:
        if self._hang_file is None:
            return
        try:
            faulthandler.dump_traceback_later(self.hang, exit=True, file=self._hang_file)
            self._armed = time.monotonic()
        except (OSError, ValueError, RuntimeError):
            self._hang_file = None

    def start(self) -> None:
        if self.hang and self.hang_path:
            try:
                self.hang_path.parent.mkdir(parents=True, exist_ok=True)
                self._hang_file = self.hang_path.open('a', encoding='utf-8')
                self._arm()
            except OSError:
                self._hang_file = None
        self._thread = threading.Thread(target=self._run, name='bender-loop-watch', daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._hang_file is not None:
            faulthandler.cancel_dump_traceback_later()
            try:
                self._hang_file.close()
            except OSError:
                pass
            self._hang_file = None

    @property
    def lag(self) -> float:
        """Самая долгая задержка отклика цикла за последние полминуты, секунд."""
        return max(self.delays, default=0.0)

    def busy(self, limit: float = BUSY) -> bool:
        return self.lag >= limit

    def _beat(self, sent: float | None = None) -> None:
        self.seen = time.monotonic()
        if sent is not None:
            self.delays.append(max(0.0, self.seen - sent))
        if self._hang_file is not None and self.seen - self._armed > REARM:
            self._arm()

    def _sample(self) -> list[str]:
        frame = sys._current_frames().get(self.ident)
        return place(frame) if frame is not None else []

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.loop.call_soon_threadsafe(self._beat, time.monotonic())
            except RuntimeError:
                return  # цикл закрыт
            behind = time.monotonic() - self.seen
            if behind > self.stall:
                stack = self._sample()
                if self.current is None:
                    self.current = {'started': time.time() - behind, 'first': stack, 'last': stack}
                else:
                    self.current['last'] = stack  # стоит дольше — смотрим, не сдвинулось ли место
            elif self.current is not None:
                record, self.current = self.current, None
                record['seconds'] = round(time.time() - record['started'] - behind, 1)
                record['where'] = ours(record['first'])
                self.recent.append(record)
                self._dump(record)
                try:
                    self.loop.call_soon_threadsafe(self.on_stall, record)
                except RuntimeError:
                    return

    def _dump(self, record) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            text = (f"=== {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(record['started']))}: цикл стоял {record['seconds']} с\n"
                    "Где встал:\n  " + '\n  '.join(record['first']) + '\n')
            if record['last'] != record['first']:
                text += 'Где был перед тем, как ожить:\n  ' + '\n  '.join(record['last']) + '\n'
            with self.path.open('a', encoding='utf-8') as stream:
                stream.write(text + '\n')
            if self.path.stat().st_size > 2 * 1024 * 1024:  # не растёт бесконечно: оставляем свежую половину
                data = self.path.read_bytes()
                self.path.write_bytes(data[len(data) // 2:])
        except OSError:
            pass

    def between(self, start: float, end: float) -> list[dict]:
        """Зависания, задевшие отрезок времени (time.time())."""
        found = [r for r in self.recent if r['started'] < end and r['started'] + r['seconds'] > start]
        if self.current and self.current['started'] < end:
            found.append({**self.current, 'seconds': round(time.time() - self.current['started'], 1), 'where': ours(self.current['first'])})
        return found


def hang_place(text: str) -> str:
    """Из записи faulthandler — верхний кадр главного потока в коде Бендера: «файл:строка функция»."""
    import re

    main = text.rsplit('Timeout (', 1)[-1]
    blocks = [b for b in main.split('\n\n') if 'most recent call first' in b]
    chosen = next((b for b in blocks if b.lstrip().startswith('Current thread')), None) or next((b for b in blocks if '_run_once' in b or 'run_forever' in b), blocks[-1] if blocks else '')
    frames = re.findall(r'File "([^"\n]+)", line (\d+) in (\S+)', chosen)
    for name, line, func in frames:
        if 'bender' in name.replace('\\', '/').split('/'):
            return f"{Path(name).name}:{line} {func}"
    return (f"{Path(frames[0][0]).name}:{frames[0][1]} {frames[0][2]}" if frames else 'неизвестно')
