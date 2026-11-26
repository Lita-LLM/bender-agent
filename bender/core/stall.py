"""Сторож цикла: замечает, что главный цикл Бендера перестал отвечать, и запоминает, где он стоял.

Пока цикл занят чем-то блокирующим (замок базы, тяжёлый расчёт без потока), молчит всё сразу: Telegram,
пульт, сердце. Потом всё срабатывает разом. Отдельный поток раз в секунду стучится в цикл; не достучался
дольше STALL секунд — снимает стек главного потока. Когда цикл ожил — запись в Бортжурнал и полный стек
в local/stalls.txt.

Если цикл не ожил совсем (бесконечный цикл, тяжёлое регулярное выражение), запись «после» не появится никогда,
а процесс так и будет держать процессор. На этот случай — таймер faulthandler: цикл переводит его каждые
несколько секунд; не перевёл HANG секунд — стеки всех потоков пишутся в local/hang.txt и процесс выходит,
Сторож или systemd поднимают его заново. Таймер живёт в потоке C и срабатывает, даже когда занят GIL.

Главный поток часто невиновен: он ждёт в select, пока другой поток (asyncio.to_thread с тяжёлым регулярным
выражением, разбором JSON, SQLite) держит GIL. Пока цикл здоров, на каждом стуке запоминаются тики процессора
каждого потока (/proc/self/task, десятки микросекунд); после зависания разница показывает, кто на самом деле
жёг процессор, — и «где встал» называет этот поток. Тот же поток раз в секунду дёргает tick (анализ нагрузки
LoadWatch сам решает, пора ли замер): отдельного потока опроса нет."""
from __future__ import annotations

import faulthandler
import os
import sys
import threading
import time
import traceback
from collections import deque
from pathlib import Path

from ..body import load as cpu

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


# Общие подпорки: место в них ничего не говорит («стоял в store.py one», «в realpath») — к нему добавляется,
# кто их вызвал, до первого кадра, который уже говорит (/load 25.11: так виновник и не был виден).
PLUMBING = ('store.py', 'pathutil.py', 'settings.py', 'posixpath.py', 'genericpath.py', 'ntpath.py', 'pathlib.py', '_local.py',
            'os.py', 'decoder.py', 'encoder.py', 'contextlib.py', 'functools.py')


def _file(line: str) -> str:
    name = line.split(':', 1)[0]
    return name[len('<frozen '):-1] + '.py' if name.startswith('<frozen ') else name


def ours(stack: list[str]) -> str:
    """Самое глубокое место в коде Бендера: по нему видно, чья это блокировка. Если это подпорка (база, пути,
    настройки) — с цепочкой вызвавших: «store.py:145 one ← telegram.py:222 owner — …»."""
    for i in range(len(stack) - 1, -1, -1):
        line = stack[i]
        if line.split(':', 1)[0] in ('threading.py', 'selectors.py', 'base_events.py', 'events.py', 'runners.py', 'tasks.py', 'thread.py', 'queue.py'):
            continue
        chain = [line]
        j = i
        while _file(stack[j]) in PLUMBING and j > 0 and len(chain) < 5:
            j -= 1
            chain.append(stack[j])
        if len(chain) == 1:
            return line
        return ' ← '.join(item.split(' — ', 1)[0] for item in chain[:-1]) + ' ← ' + chain[-1]
    return stack[-1] if stack else 'неизвестно'


def told(record) -> str:
    """Зависание для сообщения: виновник найден — его фраза («поток … держал процессор»), иначе «цикл стоял в …»."""
    where = str(record.get('where') or 'неизвестно')
    return where if record.get('culprit') or where.startswith('неизвестно') else 'цикл стоял в ' + where


WAITING = ('select', 'poll', 'epoll', 'wait', 'acquire', '_worker', 'get', 'sleep', 'recv', 'recv_into', 'read', 'readline', 'communicate')
WAIT_FILES = ('threading.py', 'queue.py', 'thread.py', 'socket.py', 'ssl.py', 'subprocess.py', 'tasks.py', 'base_events.py')


def waiting(stack: list[str]) -> bool:
    """Поток стоит в ожидании (select, замок, очередь стандартной библиотеки), а не считает сам.
    Имя функции сверяется только для файлов стандартной библиотеки: свой get или read в коде Бендера — работа."""
    if not stack:
        return True
    top = stack[-1].split(' — ', 1)[0]
    name = top.split(':', 1)[0]
    return name == 'selectors.py' or (name in WAIT_FILES and top.rsplit(' ', 1)[-1] in WAITING)


def thread_stacks(limit: int = 25) -> dict[int, tuple[str, list[str]]]:
    """Стеки всех потоков Python сейчас: {ident: (имя, стек сверху вниз)}."""
    names = {t.ident: t.name for t in threading.enumerate()}
    return {ident: (names.get(ident, f'поток {ident}'), place(frame, limit)) for ident, frame in sys._current_frames().items()}


class LoopWatch:
    def __init__(self, loop, on_stall, path: Path | None = None, *, stall: float = STALL, interval: float = INTERVAL):
        self.loop = loop
        self.on_stall = on_stall
        self.path = path
        self.stall = stall
        self.interval = interval
        self.ident = threading.get_ident()  # создаётся в потоке цикла
        self.native = threading.get_native_id()
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
        self.tick = None          # раз в секунду с этого же потока: замер нагрузки (LoadWatch.tick), сам решает, пора ли
        self.lock_probe = None    # () -> (номер потока, держащего замок базы, сколько секунд, сколько потоков ждут); читает без ожидания
        self.thread_cpu = True    # тики потоков на каждом стуке — для виновника зависания; выключается с анализом нагрузки
        self._baselines = deque(maxlen=3)  # (monotonic, тики потоков) последних здоровых стуков
        self._late = None         # (когда послан, задержка) стука, на который цикл ответил позже STALL
        self._covered = 0.0       # стуки, посланные раньше этого (monotonic), относятся к уже записанному зависанию
        self._own = None          # native id самого сторожа: себя виновником не называет
        self._own_ident = None    # и его номер в Python (ключ sys._current_frames)

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
        now = time.monotonic()
        if sent is not None:
            delay = max(0.0, now - sent)
            # Поздний ответ запоминается раньше, чем обновится seen: сторож, увидевший свежий seen, увидит и его.
            if delay > self.stall and sent > self._covered and self._late is None:
                self._late = (sent, delay)
            self.delays.append(delay)
        self.seen = now
        if self._hang_file is not None and self.seen - self._armed > REARM:
            self._arm()

    def _sample(self) -> list[str]:
        frame = sys._current_frames().get(self.ident)
        return place(frame) if frame is not None else []

    def _run(self) -> None:
        self._own = threading.get_native_id()
        self._own_ident = threading.get_ident()
        while not self._stop.wait(self.interval):
            # Сначала — сколько цикл молчит, потом новый стук: иначе освободившийся после чужого тяжёлого
            # вызова цикл успевает ответить на свежий стук раньше замера, и зависание теряется.
            behind = time.monotonic() - self.seen
            try:
                self.loop.call_soon_threadsafe(self._beat, time.monotonic())
            except RuntimeError:
                return  # цикл закрыт
            if behind > self.stall:
                stack = self._sample()
                if self.current is None:
                    started = time.monotonic() - behind
                    before = self._baseline(started)
                    self.current = {'started': time.time() - behind, 'first': stack, 'last': stack, 'lock': self._lock_state(),
                                    'stacks': thread_stacks(), 'before': before}
                else:
                    self.current['last'] = stack  # стоит дольше — смотрим, не сдвинулось ли место
            elif self.current is not None:
                record, self.current = self.current, None
                record['seconds'] = round(time.time() - record['started'] - behind, 1)
                record['where'] = ours(record['first'])
                if not self._finish(record):
                    return
            elif self._late is not None:
                # Чужой поток держал GIL тяжёлым вызовом C: молчал и сам сторож, а цикл ответил на стук раньше, чем
                # сторож проснулся. Зависание видно только по позднему ответу — записать его задним числом.
                sent, delay = self._late
                self._late = None
                if sent > self._covered:
                    before = self._baseline(sent)
                    record = {'started': time.time() - (time.monotonic() - sent), 'first': [], 'last': [], 'seconds': round(delay, 1),
                              'where': 'неизвестно: цикл и сторож ждали GIL', 'stacks': thread_stacks() if before else {}, 'before': before}
                    if not self._finish(record):
                        return
            elif behind < self.interval + 1.0:
                if self.thread_cpu:
                    # цикл здоров: точка отсчёта для следующего зависания (если /proc нет — больше не пробуем)
                    ticks = cpu.thread_ticks()
                    if ticks is None:
                        self.thread_cpu = False
                    else:
                        self._baselines.append((time.monotonic(), ticks))
                elif self._baselines:
                    self._baselines.clear()  # анализ выключен: старая точка отсчёта обвинила бы не того
            if self.tick is not None:
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 — замер нагрузки не должен останавливать сторожа цикла
                    pass

    def _lock_state(self):
        """Кто держит замок базы и сколько потоков его ждут: цикл часто стоит не сам, а в очереди за ним."""
        probe = self.lock_probe
        if probe is None:
            return None
        try:
            ident, held, waiting = probe()
        except Exception:  # noqa: BLE001 — диагностика не должна ронять сторожа
            return None
        if ident is None and not waiting:
            return None
        names = {t.ident: t.name for t in threading.enumerate()}
        return {'ident': ident, 'name': names.get(ident, f'поток {ident}') if ident is not None else '', 'held': round(held, 1),
                'waiting': waiting, 'main': ident == self.ident}

    def _baseline(self, started: float):
        """Тики потоков последнего здорового стука до начала зависания (monotonic): (когда, тики) или None."""
        chosen = None
        for item in list(self._baselines):
            if item[0] <= started or chosen is None:
                chosen = item
        return chosen

    def _finish(self, record) -> bool:
        """Зависание кончилось: виновник, память, stalls.txt, сообщение циклу. False — цикл уже закрыт."""
        self._covered = time.monotonic()
        self._late = None
        self._blame(record)
        self.recent.append(record)
        self._dump(record)
        try:
            self.loop.call_soon_threadsafe(self.on_stall, record)
        except RuntimeError:
            return False
        return True

    def _blame(self, record) -> None:
        """Кто жёг процессор, пока цикл молчал: тики потоков от последнего здорового стука до оживления.
        Главный поток ждал, а другой работал — «где встал» называет этот поток. Не нашёлся такой — смотрим,
        не стоял ли цикл в очереди за замком базы: тогда виновен тот, кто замок держал."""
        before, stacks = record.pop('before', None), record.pop('stacks', {})
        record['threads'] = []
        # Потоки, которые в начале зависания не ждали, а работали (или сидели в базе без процессора): их стеки — в отчёт.
        record['active'] = [{'name': name, 'ident': ident, 'stack': stack[-14:]} for ident, (name, stack) in stacks.items()
                            if ident not in (self.ident, self._own_ident) and stack and not waiting(stack)][:8]
        if before:
            self._blame_cpu(record, before, stacks)
        lock = record.get('lock')
        if (not record.get('culprit') and lock and lock.get('ident') is not None and not lock['main'] and lock['waiting']
                and lock['held'] >= 0.5 * self.stall and lock['ident'] != self._own_ident):
            # Цикл стоит в очереди за замком базы, а держит его другой поток: он и назван — с местом, где он сейчас.
            stack = stacks.get(lock['ident'], ('', []))[1]
            spot = ours(stack) if stack and not waiting(stack) else ''
            record['culprit'] = lock['name']
            record['where'] = f"поток «{lock['name']}» держал замок базы {lock['held']} с, цикл ждал" + (f" — {spot}" if spot else '')

    def _blame_cpu(self, record, before, stacks) -> None:
        at, ticks = before
        rows = cpu.thread_cpu(ticks, cpu.thread_ticks(), time.monotonic() - at)
        for row in rows[:6]:
            stack = stacks.get(row['ident'], ('', []))[1] if row['ident'] is not None else []
            record['threads'].append({'name': row['name'], 'seconds': row['seconds'], 'pct': row['pct'], 'main': row['tid'] == self.native,
                                      'stack': [] if row['tid'] == self._own else stack[-12:]})
        main = next((row for row in rows if row['tid'] == self.native), None)
        other = next((row for row in rows if row['tid'] not in (self.native, self._own)), None)
        spent = main['seconds'] if main else 0.0
        total = sum(row['seconds'] for row in rows if row['tid'] != self._own)
        enough = max(0.5, 0.2 * record['seconds'])
        if not record['first'] and spent >= enough and spent >= 0.6 * total:
            # Сторож не застал цикл (ждал GIL), а процессор за это время взял сам цикл: один долгий вызов C без
            # отдачи GIL прямо в цикле — регулярное выражение, разбор, сжатие. Места не видно, но виновник — не другой поток.
            record['culprit'] = main['name']
            record['where'] = f"цикл сам держал процессор {spent} с одним долгим вызовом без отдачи GIL — места не видно"
            return
        if other is None:
            return
        # Процессорное время, а не стена: на занятой узле поток с GIL получает лишь часть ядра. Виновник — тот, кто
        # взял большую часть процессора процесса за зависание (не меньше 0,5 с и пятой части его длины), а цикл за
        # то же время сам потратил меньше половины от него.
        if other['seconds'] >= enough and other['seconds'] >= 0.6 * total and spent < 0.5 * other['seconds']:
            stack = stacks.get(other['ident'], ('', []))[1] if other['ident'] is not None else []
            spot = ours(stack) if stack and not waiting(stack) else ''
            record['culprit'] = other['name']
            record['where'] = f"поток «{other['name']}» держал процессор {other['seconds']} с, цикл ждал" + (f" — {spot}" if spot else '')

    def _dump(self, record) -> None:
        if not self.path:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            text = (f"=== {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(record['started']))}: цикл стоял {record['seconds']} с\n"
                    + (f"Виновник: {record['where']}\n" if record.get('culprit') else '')
                    + "Где встал главный поток:\n  " + ('\n  '.join(record['first']) or 'не видно: GIL держал другой поток, сторож проснулся уже после') + '\n')
            if record['last'] != record['first']:
                text += 'Где был перед тем, как ожить:\n  ' + '\n  '.join(record['last']) + '\n'
            lock = record.get('lock')
            if lock:
                who = 'главный поток (цикл)' if lock['main'] else (f"поток «{lock['name']}»" if lock['ident'] is not None else 'никто')
                text += f"Замок базы в начале зависания: держит {who}" + (f" {lock['held']} с" if lock['ident'] is not None else '') + f"; ждут потоков: {lock['waiting']}\n"
            printed = set()
            if record.get('threads'):
                text += 'Процессор потоков за время зависания:\n' + ''.join(
                    f"  {t['name']}{' (цикл)' if t['main'] else ''}: {t['seconds']} с ({t['pct']}%)\n" for t in record['threads'])
                for t in record['threads'][:3]:
                    if t['stack'] and not t['main']:
                        note = ' (уже ждал работы: тяжёлый вызов закончился раньше, чем сторож смог посмотреть)' if waiting(t['stack']) else ''
                        text += f"Стек потока {t['name']} в начале зависания{note}:\n  " + '\n  '.join(t['stack']) + '\n'
                        printed.add(t['name'])
            # Остальные работавшие потоки: тот, кто держит замок или сидит в базе, процессор может и не жечь.
            for item in record.get('active') or []:
                if item['name'] not in printed:
                    text += f"Стек потока {item['name']} (не ждал) в начале зависания:\n  " + '\n  '.join(item['stack']) + '\n'
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
            current = {k: v for k, v in self.current.items() if k not in ('stacks', 'before')}
            found.append({**current, 'seconds': round(time.time() - self.current['started'], 1), 'where': ours(self.current['first'])})
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
