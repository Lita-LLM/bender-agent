"""Почему цикл встал: счётчики ядра до остановки и после, и вывод по их разнице.

Стек главного потока говорит, где цикл стоял, но не почему вызов «обычно мгновенный» занял восемь секунд:
stat, чтение настроек, realpath. Причина лежит в ядре и вокруг него. Сторож раз в секунду запоминает счётчики
(это десятки микросекунд), а после остановки сравнивает: сколько поток был готов, но не получал процессор; сколько
узел вообще переключал контекст (замерший узел не переключает ничего); сколько задачи ждали диск и память; шла ли
подкачка; ограничивала ли процессор группа; сколько длилась сборка мусора; не опаздывал ли сам сторож (опоздал он —
не выполнялся весь процесс); отвечала ли канарейка файловой системы (fsprobe). Из них — одно предложение «причина:».

Каждое число — только то, что видело ядро; чего нет в /proc (нет PSI, нет cgroup), пропускается, а не выдумывается.
Только стандартная библиотека; /proc и /sys подменяются в тестах."""
from __future__ import annotations

import gc
import time
from collections import deque
from pathlib import Path

from ..body import load as cpu

GC_MIN = 0.05   # сборки короче не запоминаются
NAMES = {'stat': 'stat базы', 'read': 'чтение SQLite', 'fsync': 'запись с fsync'}


class GcPauses:
    """Сколько длилась каждая сборка мусора Python: во время неё стоят все потоки (GIL), и для сторожа это выглядит как «цикл встал»."""

    def __init__(self):
        self.events: deque = deque(maxlen=20)   # {'at', 'seconds', 'generation'}
        self._started = None
        self._installed = False

    def _callback(self, phase, info) -> None:
        if phase == 'start':
            self._started = time.monotonic()
        elif phase == 'stop' and self._started is not None:
            seconds, self._started = time.monotonic() - self._started, None
            if seconds >= GC_MIN:
                self.events.append({'at': time.time() - seconds, 'seconds': round(seconds, 2), 'generation': (info or {}).get('generation')})

    def install(self) -> None:
        if not self._installed:
            gc.callbacks.append(self._callback)
            self._installed = True

    def remove(self) -> None:
        if self._installed:
            try:
                gc.callbacks.remove(self._callback)
            except ValueError:
                pass
            self._installed = False

    def during(self, start: float, end: float) -> float:
        """Сколько секунд сборки мусора пришлось на отрезок (time.time())."""
        return round(sum(min(e['at'] + e['seconds'], end) - max(e['at'], start) for e in list(self.events) if e['at'] < end and e['at'] + e['seconds'] > start), 2)


class Sensors:
    """Снимки счётчиков ядра для одного потока (цикла) и узла."""

    def __init__(self, native: int, proc: Path = cpu.PROC, sys_root: Path = cpu.SYS):
        self.native = native
        self.proc, self.sys_root = proc, sys_root
        self.disk = ''                 # имя раздела в /proc/diskstats, где лежат данные ('' — неизвестно)
        self._cgroup = False           # каталог cgroup ищется один раз (False — ещё не искали)

    def cgroup(self):
        if self._cgroup is False:
            self._cgroup = cpu.own_cgroup(self.proc, self.sys_root)
        return self._cgroup

    def snapshot(self) -> dict:
        proc = self.proc
        return {'mono': time.monotonic(), 'wall': time.time(), 'task': cpu.task_stat(self.native, proc), 'sched': cpu.task_sched(self.native, proc),
                'proc_cpu': cpu.own_cpu(), 'psi': cpu.pressure_totals(proc), 'sys': cpu.system_cpu(proc), 'ctxt': cpu.system_ctxt(proc),
                'vm': cpu.vm_counters(proc), 'throttle': cpu.cgroup_throttle(self.cgroup()),
                'disk': cpu.disk_counters(self.disk, proc) if self.disk else None}

    def stuck(self) -> dict:
        """Что делал поток цикла в тот момент, когда сторож заметил остановку: состояние и функция ядра, где он спит."""
        task = cpu.task_stat(self.native, self.proc)
        return {'state': task['state'] if task else '', 'wchan': cpu.task_wchan(self.native, self.proc)}


def _diff(before: dict, after: dict, *path):
    """after - before по ключам path; None, если счётчика нет в одном из снимков."""
    a, b = after, before
    for key in path:
        a = a.get(key) if isinstance(a, dict) else None
        b = b.get(key) if isinstance(b, dict) else None
    return None if a is None or b is None else a - b


def explain(before: dict | None, after: dict | None, seconds: float, notes: dict) -> dict:
    """{'cause': одно предложение или '', 'facts': строки с числами}. notes: stuck (state, wchan), canary (пробы дольше порога за остановку),
    canary_fast (канарейка работала и отвечала), fs (на чём лежат данные), gc (секунд сборки мусора), self_late (секунд, на которые опоздал сторож)."""
    facts, causes = [], []
    seconds = max(seconds, 0.1)
    stuck = notes.get('stuck') or {}
    window = (after['mono'] - before['mono']) if before and after else 0.0
    d = {}
    if before and after and window > 0:
        d['main_cpu'] = _diff(before, after, 'task', 'ticks')
        d['majflt'] = _diff(before, after, 'task', 'majflt')
        d['ready'] = _diff(before, after, 'sched', 'wait')
        d['proc_cpu'] = _diff(before, after, 'proc_cpu')
        d['ctxt'] = _diff(before, after, 'ctxt')
        d['throttle'] = _diff(before, after, 'throttle')
        d['steal'] = _diff(before, after, 'sys', 'steal')
        d['iowait'] = _diff(before, after, 'sys', 'iowait')
        for key in ('cpu_some', 'io_some', 'io_full', 'memory_some', 'memory_full'):
            d[key] = _diff(before, after, 'psi', key)
        d['swap'] = sum(v for v in (_diff(before, after, 'vm', 'pswpin'), _diff(before, after, 'vm', 'pswpout')) if v is not None) if (after.get('vm') and before.get('vm')) else None
        d['sysfault'] = _diff(before, after, 'vm', 'pgmajfault')
        d['disk'] = _diff(before, after, 'disk', 'busy')
        if d['main_cpu'] is not None:
            d['main_cpu'] /= cpu.HZ
        for key in ('steal', 'iowait'):
            if d[key] is not None:
                d[key] /= cpu.HZ
    canary = notes.get('canary') or []
    gc_seconds = float(notes.get('gc') or 0.0)
    late = float(notes.get('self_late') or 0.0)

    def num(value, unit=' с'):
        return f'{value:.1f}'.replace('.', ',') + unit

    # --- причины, в порядке уверенности
    hung = [e for e in canary if e['seconds'] >= 0.5 * seconds]
    if hung:
        top = max(hung, key=lambda e: e['seconds'])
        where = f" ({notes['fs']})" if notes.get('fs') else ''
        causes.append(f"файловая система не отвечала{where}: {NAMES.get(top['kind'], top['kind'])} из отдельного потока висел {num(top['seconds'])}"
                      + (' и ещё не вернулся' if top.get('ongoing') else ''))
    memory = (d.get('memory_some') or 0) >= 0.3 * seconds or (d.get('swap') or 0) >= 1000 or (d.get('majflt') or 0) >= 200
    if memory:
        causes.append(f"память: поток ждал страницы с диска или из подкачки (ошибок страниц {d.get('majflt') or 0}, подкачка {d.get('swap') or 0} стр., "
                      f"узел ждал память {num(d.get('memory_some') or 0)})")
    starved = (d.get('ready') or 0) >= 0.5 * seconds or (d.get('throttle') or 0) >= 0.5 * seconds or (d.get('steal') or 0) >= 0.3 * seconds
    if starved:
        parts = []
        if (d.get('ready') or 0) >= 0.2 * seconds:
            parts.append(f"поток цикла был готов, но стоял в очереди за процессором {num(d['ready'])}")
        if (d.get('throttle') or 0) >= 0.2 * seconds:
            parts.append(f"группа ограничивала процессор {num(d['throttle'])}")
        if (d.get('steal') or 0) >= 0.2 * seconds:
            parts.append(f"гипервизор отнял процессор {num(d['steal'])}")
        causes.append('процессор: ' + ', '.join(parts or ['очередь за процессором']))
    if late >= 0.5 * seconds:
        if d.get('ctxt') is not None and window > 0 and d['ctxt'] / window < 20:
            causes.append(f"узел замирал целиком: сторож опоздал на {num(late)}, а переключений контекста на всём узле — {d['ctxt'] / window:.0f} в секунду "
                          '(виртуальная машина встала, узел засыпал или завис на питании)')
        elif not starved and not memory and (d.get('proc_cpu') or 0) < 0.3 * seconds:
            causes.append(f"весь процесс Бендера не выполнялся {num(late)}, а узел жил: его останавливала система или отнимала процессор (заморозка группы, SIGSTOP)")
    if gc_seconds >= 0.5 * seconds:
        causes.append(f'сборщик мусора Python держал все потоки {num(gc_seconds)}')
    if stuck.get('state') == 'D' or (d.get('io_some') or 0) >= 0.3 * seconds or (d.get('disk') or 0) >= 0.7 * seconds:
        parts = []
        if stuck.get('state') == 'D':
            parts.append('поток цикла в ядре ждал ввода-вывода (состояние D' + (f", {stuck['wchan']}" if stuck.get('wchan') else '') + ')')
        if (d.get('io_some') or 0) >= 0.3 * seconds:
            parts.append(f"задачи узла ждали диск {num(d['io_some'])}")
        if (d.get('disk') or 0) >= 0.7 * seconds:
            parts.append(f"диск с данными был занят {num(d['disk'])}")
        causes.append('диск: ' + ', '.join(parts))

    if not causes and d.get('ready') is not None and d.get('ctxt') is not None and notes.get('canary_fast'):
        # Всё, что видит ядро, в порядке, а канарейка файловой системы отвечала: значит, не носитель, не процессор и не память.
        causes.append('ядро, процессор, память и диск в норме — цикл ждал сам код Бендера (замок, вызов без отдачи цикла; стек выше)')

    # --- числа для отчёта: все, что удалось прочитать
    if d:
        line = [f"за {num(window)} цикл потратил процессора {num(d['main_cpu'] or 0, ' с')}" if d.get('main_cpu') is not None else '']
        if d.get('ready') is not None:
            line.append(f"был готов, но ждал процессор {num(d['ready'])}")
        if d.get('majflt') is not None:
            line.append(f"ошибок страниц с диском {d['majflt']}")
        if d.get('cpu_some') is not None:
            line.append(f"узел ждал процессор {num(d['cpu_some'])}, диск {num(d.get('io_some') or 0)}, память {num(d.get('memory_some') or 0)}")
        if d.get('steal'):
            line.append(f"steal {num(d['steal'])}")
        if d.get('throttle') is not None:
            line.append(f"ограничение процессора группой {num(d['throttle'])}")
        if d.get('swap'):
            line.append(f"подкачка {d['swap']} стр.")
        if d.get('disk') is not None:
            line.append(f"диск с данными занят {num(d['disk'])}")
        if d.get('ctxt') is not None and window > 0:
            line.append(f"переключений контекста на узле {d['ctxt'] / window:.0f}/с")
        facts.append('Ядро: ' + '; '.join(x for x in line if x))
    if stuck.get('state'):
        facts.append(f"Поток цикла в момент, когда сторож заметил остановку: состояние {stuck['state']}" + (f", спит в {stuck['wchan']}" if stuck.get('wchan') else '')
                     + {'R': ' (работает или ждёт процессор)', 'S': ' (спит)', 'D': ' (ждёт ввода-вывода в ядре)', 'T': ' (остановлен)'}.get(stuck['state'], ''))
    if late:
        facts.append(f'Сторож за это время опоздал на {num(late)}: сам он ничего не ждёт — значит, не выполнялся весь процесс или кто-то держал GIL.')
    if gc_seconds:
        facts.append(f'Сборка мусора Python: {num(gc_seconds)}.')
    if canary:
        facts.append('Канарейка файловой системы (отдельный поток): ' + '; '.join(f"{NAMES.get(e['kind'], e['kind'])} — {num(e['seconds'])}" + (' и ещё идёт' if e.get('ongoing') else '') for e in canary) + '.')
    elif notes.get('canary_fast'):
        facts.append(f"Канарейка файловой системы отвечала быстро (самая долгая проба {notes['canary_fast']}): диск и файловая система соседнему потоку не мешали.")
    if notes.get('fs'):
        facts.append('Данные лежат на: ' + notes['fs'] + '.')
    return {'cause': '; '.join(causes[:2]), 'facts': facts, 'all': causes}
