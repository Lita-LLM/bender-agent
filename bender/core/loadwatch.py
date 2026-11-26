"""Нагрузка: замечает, когда Бендер или весь узел упираются в процессор и память, и выясняет — кто.

Хозяин видел, как узел уходит в 100% ЦП и падает, причина неизвестна. Здесь — дешёвый постоянный замер
и дорогой разбор только по необычному:

- Замер раз в sample_seconds делает тот же поток, что стучится в цикл (LoopWatch.tick): своё время ЦП,
  занятость узла, средняя нагрузка, память, подкачка, давление (PSI), задержка цикла. Это десятки
  микросекунд, кольцо замеров за час живёт в памяти, на диск в простое не пишется ничего.
- Правила: Бендер держит больше cpu_percent ядра дольше minutes; узел занят больше node_percent или
  средняя нагрузка выше двух на ядро дольше node_minutes; кончается память; перегрев и троттлинг Raspberry Pi;
  три зависания цикла за 10 минут; нагрузка, необычная для этого часа.
- Необычное — снимок «кто»: ЦП каждого потока Бендера за 2 с и где они стоят (20 раз в секунду), задачи
  asyncio, которые всё время готовы к работе (бесконечный цикл без паузы), процессы узла по ЦП и памяти с
  пометкой, какие из них Бендера, и что Бендер делает. Отчёт — local/load/<время>.json и .txt (его можно
  целиком вставить в ИИ-чат), запись в журнал, короткое сообщение хозяину, разбор заведующего узлом.
- Сообщения не чаще cooldown_minutes о той же причине и не больше DAILY_ALERTS в сутки; когда нагрузка
  спала — одна короткая строка.

Заведующий узлом — агент, которого выбрал хозяин. Он получает отчёт недоверенным (имена и строки запуска
чужих процессов может подобрать кто угодно), ход безлюдный и заражённый: ничего не завершает и не меняет
сам, только объясняет хозяину и предлагает шаги.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import threading
import time
from collections import Counter, defaultdict, deque
from pathlib import Path

from ..body import load as cpu
from .errors import HumanError
from .stall import told

CAPTURE_GAP = 300.0     # снимок «кто грузит» — не чаще раза в 5 минут
CAPTURE_SECONDS = 2.0   # сколько длится снимок
STACK_HZ = 20           # стеки горячих потоков и пробы цикла — 20 раз в секунду
DAILY_ALERTS = 12       # сообщений хозяину о нагрузке в сутки (спад не считается)
HISTORY = 3600.0        # кольцо замеров — час
BASELINE = 1800.0       # «необычно для этого часа» — только когда за плечами полчаса замеров
ODD_FLOOR = 60.0        # …и только от 60% ядра: долгий ход с тулами законно берёт 30–50%
BOX_GAP = 60.0          # «чёрный ящик» под нагрузкой — не чаще раза в минуту
BOX = 'load-recent.json'          # local/: последние замеры под нагрузкой — на случай, если узел упадёт
BOX_LAST = 'load-before-exit.json'  # …он же после обрыва: что было перед ним
ICON = '📈'              # значок сообщений о нагрузке (Telegram, пульт)
BENDER = ('bender', 'stalls')
NODE = ('node', 'memory', 'heat')
TITLES = {'bender': 'процессор Бендера', 'stalls': 'зависания цикла', 'node': 'процессор узла', 'memory': 'память узла', 'heat': 'перегрев или питание узла'}
BUSY = (('backup', 'бекап'), ('update', 'обновление'), ('sleep', 'сон'), ('team', 'задачи команды'))  # известные тяжёлые дела
HEAD = ('[Заведующий узлом: хозяин назначил тебя следить за нагрузкой узла и самого Бендера. «Узел» здесь — только этот компьютер, '
        'на котором работает Бендер: его процессор, память, диск, температура и процессы. Не сеть и не другие устройства хозяина. '
        'Ниже — автоматический отчёт о необычной нагрузке: числа, потоки и задачи Бендера, процессы машины. Разбери его: найди '
        'вероятную причину, коротко и простыми словами объясни хозяину и предложи конкретные шаги. Опирайся только на отчёт и '
        'тул load_report: чего в них нет (состояние сети, роутеры, другие устройства, прошлые события), ты не знаешь — не гадай и не '
        'пиши об этом; если данных не хватает, так и скажи. Сам ничего не завершай, не меняй приоритеты процессов, '
        'настройки и файлы — только с согласия хозяина: хозяина рядом может не быть, такие действия будут отклонены. '
        'Свежие числа — тул load_report.]')


def _default(key):
    from .settings import SCHEMA
    return SCHEMA['load'].field(key).default


class LoadWatch:
    def __init__(self, app, *, proc: Path = cpu.PROC, sys_root: Path = cpu.SYS):
        self.app = app
        self.proc, self.sys_root = proc, sys_root
        self.loop = None
        self.watch = None
        self.cfg = {}
        self.alarm_temp = 85.0
        self.samples: deque = deque(maxlen=360)
        self.anomalies: deque = deque(maxlen=30)
        self.active: dict[str, dict] = {}
        self._since: dict[str, float] = {}
        self._prev = None
        self._next = 0.0
        self._cores = None
        self._pi = None            # есть ли у узла файл троттлинга Raspberry Pi (проверяется один раз)
        self._capturing = False
        self._last_capture = -CAPTURE_GAP
        self._last_report = (0.0, '')  # (monotonic, путь) последнего снимка
        self._stopped = False
        self._listening = False
        self._fallback = set()     # о недоступном заведующем — одна строка журнала
        self._manager_tasks: set = set()
        self._box_at = 0.0         # когда последний раз писался чёрный ящик (0 — его нет: нагрузка обычная)
        self._box_thread = None
        self._boot = None
        self._lock = threading.RLock()  # замер идёт в потоке сторожа цикла, настройки меняются в потоке цикла
        self.limits = {'date': '', 'alerts': 0, 'manager': 0, 'said': {}, 'asked': {}}
        self.reload()

    # ---------------------------------------------------------------- настройки и жизнь

    def reload(self) -> None:
        """Настройки читаются в потоке цикла; поток замера берёт готовый словарь и не ходит в базу."""
        try:
            cfg = dict(self.app.settings.group('load'))
            self.alarm_temp = float(self.app.settings.get('heart', 'alarm_temp_c', 85))
        except HumanError:
            from .settings import SCHEMA
            cfg = {f.key: f.default for f in SCHEMA['load'].fields}
        with self._lock:
            node_changed = bool(cfg.get('node')) != bool(self.cfg.get('node'))
            self.cfg = cfg
            size = max(12, int(HISTORY / max(1, int(cfg.get('sample_seconds') or 10))))
            if size != self.samples.maxlen:
                self.samples = deque(self.samples, maxlen=size)
            if self.watch is not None:
                self.watch.thread_cpu = bool(cfg.get('enabled'))
            if not cfg.get('enabled'):
                self._prev = None
                self.active.clear()
                self._since.clear()
            elif node_changed:
                self._prev = None  # прежний замер без /proc/stat (или с ним) для разницы не годится

    def start(self, loop, watch=None) -> None:
        self.loop = loop
        self.watch = watch
        self._stopped = False
        stored = self.app.store.meta_get('load:limits')
        if isinstance(stored, dict):
            self.limits.update({k: v for k, v in stored.items() if k in self.limits})
        if not self._listening:
            self.app.bus.listen(self._on_event)
            self._listening = True
        self.reload()
        if watch is not None:
            watch.tick = self.tick

    def stop(self) -> None:
        self._stopped = True
        if self.watch is not None:
            self.watch.tick = None
        for task in list(self._manager_tasks):
            task.cancel()
        try:
            run = self.app.store.meta_get('load:run')
            if isinstance(run, dict) and run.get('pid') == os.getpid():
                self.app.store.meta_set('load:run', {**run, 'clean': True, 'stopped': time.time()})
                if self._box_thread is not None:
                    self._box_thread.join(1.0)
                (self.app.dirs.local / BOX).unlink(missing_ok=True)  # штатная остановка: ящик не понадобился
        except Exception:  # noqa: BLE001 — остановка не должна падать из-за отметки
            pass

    def _on_event(self, event) -> None:
        if event.get('type') == 'settings' and event.get('group') in ('load', 'heart'):
            self.reload()

    def _post(self, fn, *args) -> None:
        """Из потока замера и снимка — в цикл; без цикла (тесты, Полигон) — сразу."""
        loop = self.loop
        if loop is None:
            fn(*args)
            return
        try:
            loop.call_soon_threadsafe(fn, *args)
        except RuntimeError:
            pass

    def boot(self, *, hang: bool = False) -> str:
        """Прошлый запуск оборвался без остановки? Зависание уже сообщено (hang.txt) — здесь машина и тихая смерть."""
        if not self.cfg.get('enabled'):
            return ''
        store = self.app.store
        previous = store.meta_get('load:run')
        current = self._boot = cpu.boot_id(self.proc)
        said = ''
        before = self._last_box(previous) if isinstance(previous, dict) and not previous.get('clean') else None
        if before and hang:
            self.app.journal.write('load', 'Перед зависанием: ' + before, data={'box': True})
        if isinstance(previous, dict) and not previous.get('clean') and not hang:
            crash = self.app.dirs.local / 'last_crash.txt'
            try:
                crashed = crash.stat().st_mtime > float(previous.get('started') or 0)
            except OSError:
                crashed = False
            if current and previous.get('boot') and previous['boot'] != current:
                said = ('Машина перезагрузилась, а Бендер не успел остановиться: пропадало питание, узел завис, перегрелся '
                        'или кончилась память. Отчёты о нагрузке перед этим — в local/load, если она была необычной.')
                said += (' Перед этим: ' + before) if before else ''
                self.app.journal.write('load', said, data={'reboot': True, 'box': bool(before)})
                if self.cfg.get('notify') and self.loop is not None:
                    self.loop.call_later(20, self.app.bus.publish, {'type': 'notify', 'kind': 'load', 'agent_id': '*', 'title': 'Нагрузка', 'text': said})
            elif not crashed:
                said = ('Прошлый запуск Бендера оборвался без остановки и без записи об ошибке: его могла завершить система '
                        '(кончилась память, kill). Отчёты о необычной нагрузке перед этим — в local/load, если она была.')
                said += (' Перед этим: ' + before) if before else ''
                self.app.journal.write('load', said, data={'silent_exit': True, 'box': bool(before)})
                if self.cfg.get('notify') and self.loop is not None:
                    self.loop.call_later(20, self.app.bus.publish, {'type': 'notify', 'kind': 'load', 'agent_id': '*', 'title': 'Нагрузка', 'text': said})
        if before and not said and not hang:
            self.app.journal.write('load', 'Перед падением ядра: ' + before, data={'box': True})  # о падении скажет last_crash.txt
        store.meta_set('load:run', {'boot': current, 'clean': False, 'started': time.time(), 'pid': os.getpid()})
        return said

    def _last_box(self, previous: dict) -> str:
        """Чёрный ящик оборвавшегося запуска: последние замеры под нагрузкой одной строкой ('' — нагрузка была обычной).
        Файл переносится в local/load-before-exit.json, чтобы следующий запуск не сказал о нём снова."""
        path = self.app.dirs.local / BOX
        try:
            box = json.loads(path.read_text(encoding='utf-8'))
            os.replace(path, self.app.dirs.local / BOX_LAST)
        except (OSError, ValueError):
            return ''
        if not isinstance(box, dict) or box.get('pid') != previous.get('pid') or not isinstance(box.get('samples'), list) or not box['samples']:
            return ''
        last = box['samples'][-1] if isinstance(box['samples'][-1], dict) else {}
        line = numbers_line(numbers(last), box.get('cores'))
        if not line or not isinstance(box.get('t'), (int, float)):
            return ''
        active = [TITLES.get(name, str(name)) for name in box.get('active') or []]
        return (f"в {cpu.stamp(box['t'])} {line}" + (f" — необычное: {', '.join(active)}" if active else '')
                + f'. Последние замеры — local/{BOX_LAST}.')

    # ---------------------------------------------------------------- замер (поток сторожа цикла)

    def tick(self) -> None:
        cfg = self.cfg
        if self._stopped or not cfg.get('enabled'):
            return
        now = time.monotonic()
        if now < self._next:
            return
        self._next = now + int(cfg.get('sample_seconds') or 10)
        self.sample()

    def cores(self) -> float:
        if self._cores is None:
            self._cores = cpu.effective_cores(self.sys_root)
        return self._cores

    def sample(self, now: float | None = None) -> dict | None:
        """Один замер. Первый только запоминает счётчики; дальше — проценты за промежуток и правила."""
        with self._lock:
            return self._sample(now)

    def _sample(self, now: float | None) -> dict | None:
        cfg = self.cfg
        if not cfg.get('enabled'):
            return None
        mono, wall = time.monotonic(), now if now is not None else time.time()
        node = bool(cfg.get('node'))
        current = {'mono': mono, 'own': cpu.own_cpu(), 'sys': cpu.system_cpu(self.proc) if node else None}
        previous, self._prev = self._prev, current
        if previous is None or mono - previous['mono'] <= 0:
            return None
        seconds = mono - previous['mono']
        s = {'t': round(wall, 1), 'own': round(100 * (current['own'] - previous['own']) / seconds, 1)}
        if node:
            busy = cpu.cpu_busy(previous['sys'], current['sys'])
            if busy:
                s.update(sys=busy['busy'], iowait=busy['iowait'], steal=busy['steal'])
            la = cpu.loadavg()
            if la:
                s['load1'] = la[0]
            mem = cpu.memory(self.proc)
            if mem:
                s.update(mem=mem.get('available_pct'), swap=mem.get('swap_used_mb'), swap_total=mem.get('swap_total_mb'))
                if 'oom_kill' in mem:
                    s['oom'] = mem['oom_kill']
            psi = cpu.pressure(self.proc)
            if psi:
                s.update(psi_cpu=psi.get('cpu_some'), psi_mem=psi.get('memory_full'), psi_io=psi.get('io_full'))
            if self._pi is None:
                self._pi = cpu.throttled(self.sys_root) is not None
            if self._pi:
                state = cpu.throttled(self.sys_root)
                if state and state['now']:
                    s['throttled'] = state['now']
            temp = cpu.temperature(self.sys_root)
            if temp is not None:
                s['temp'] = temp
        watch = self.watch
        if watch is not None:
            s['lag'] = round(watch.lag, 2)
            s['stalls'] = sum(1 for r in list(watch.recent) if r['started'] > wall - HISTORY)
        self.samples.append(s)
        self._keep_box(s, self._evaluate(s))
        return s

    def _keep_box(self, s: dict, elevated: bool) -> None:
        """Под нагрузкой — раз в минуту последние замеры в local/load-recent.json; спала — файл убирается.
        В простое и при обычной работе на диск не пишется ничего."""
        if elevated:
            if s['t'] - self._box_at >= BOX_GAP:
                self._box_at = s['t']
                self._write_box({'format': 1, 'pid': os.getpid(), 'boot': self._boot, 't': s['t'], 'cores': self.cores(),
                                 'active': sorted(self.active), 'samples': list(self.samples)[-12:]})
        elif self._box_at:
            self._box_at = 0.0
            self._write_box(None)  # нагрузка спала: ящик не нужен

    def _write_box(self, box: dict | None) -> None:
        """Чёрный ящик: если узел упадёт под нагрузкой, следующий запуск скажет, какой она была. Пишется отдельным
        коротким потоком: запись на занятый диск не должна задержать стук сторожа (это выглядело бы зависанием цикла)."""
        path = self.app.dirs.local / BOX

        def write():
            try:
                if box is None:
                    path.unlink(missing_ok=True)
                    return
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(BOX + '.tmp')
                tmp.write_text(json.dumps(box, ensure_ascii=False), encoding='utf-8')
                os.replace(tmp, path)
            except OSError:
                pass
        self._box_thread = threading.Thread(target=write, name='bender-load-box', daemon=True)
        self._box_thread.start()

    # ---------------------------------------------------------------- правила

    def _held(self, key: str, cond: bool, t: float, minutes: float) -> bool:
        """Условие держится подряд не меньше minutes (с учётом шага замера)."""
        if not cond:
            self._since.pop(key, None)
            return False
        since = self._since.setdefault(key, t)
        return t - since + int(self.cfg.get('sample_seconds') or 10) >= minutes * 60

    def _baseline(self, key: str, t: float) -> float | None:
        """Обычный уровень за час: медиана замеров. None — истории меньше получаса."""
        items = [s[key] for s in list(self.samples) if s.get(key) is not None]
        if not self.samples or t - self.samples[0]['t'] < BASELINE or len(items) < 10:
            return None
        items.sort()  # медиана без statistics: тот тянет decimal, fractions и random — лишняя память ядра
        middle = len(items) // 2
        return items[middle] if len(items) % 2 else (items[middle - 1] + items[middle]) / 2

    def _subjects(self, s: dict) -> dict[str, dict]:
        cfg, t, out = self.cfg, s['t'], {}
        own, limit, minutes = s['own'], float(cfg['cpu_percent']), float(cfg['minutes'])
        base = self._baseline('own', t) if own >= ODD_FLOOR else None
        unusual = max(ODD_FLOOR, 4 * base + 20) if base is not None else None
        high = self._held('bender:high', own >= limit, t, minutes)
        odd = self._held('bender:odd', unusual is not None and own >= unusual, t, minutes)
        floor = 0.8 * min(limit, self.active.get('bender', {}).get('floor') or limit)
        out['bender'] = {'due': high or odd, 'on': own >= floor, 'floor': min(limit, unusual) if odd and not high else limit,
                         'reason': (f'Бендер держит {own:.0f}% ядра дольше {minutes:g} мин (порог {limit:g}%)' if high else
                                    f'Бендер держит {own:.0f}% ядра — необычно: обычно за последний час {base or 0:.0f}%')}
        watch = self.watch
        if watch is not None:
            recent = [r for r in list(watch.recent) if r['started'] > t - 600]
            last = recent[-1] if recent else None
            out['stalls'] = {'due': len(recent) >= 3, 'on': bool(recent),
                             'reason': f'цикл Бендера вставал {len(recent)} раз за 10 минут' + (f", последний раз {last['seconds']} с: {told(last)}" if last else '')}
        if not cfg.get('node'):
            return out
        cores = self.cores()
        busy, la = s.get('sys'), s.get('load1')
        if busy is not None or la is not None:
            node_limit, node_minutes = float(cfg['node_percent']), float(cfg['node_minutes'])
            nbase = self._baseline('sys', t) if busy is not None and busy >= 60 else None
            nodd_limit = max(60.0, 3 * nbase + 25) if nbase is not None else None
            hot = self._held('node:high', busy is not None and busy >= node_limit, t, node_minutes)
            queue = self._held('node:load', la is not None and la >= 2 * cores, t, node_minutes)
            nodd = self._held('node:odd', nodd_limit is not None and busy is not None and busy >= nodd_limit, t, node_minutes)
            on = (busy is not None and busy >= 0.8 * min(node_limit, nodd_limit or node_limit)) or (la is not None and la >= 1.5 * cores)
            reason = (f'узел занят на {busy:.0f}% дольше {node_minutes:g} мин' if hot else
                      f'средняя нагрузка {la} на {cores:g} яд. дольше {node_minutes:g} мин' if queue else
                      f'узел занят на {busy or 0:.0f}% — необычно: обычно за последний час {nbase or 0:.0f}%')
            out['node'] = {'due': hot or queue or nodd, 'on': on, 'reason': reason}
        mem = s.get('mem')
        if mem is not None:
            earlier = next((x for x in reversed(list(self.samples)[:-3]) if x.get('swap') is not None), None)
            growing = not s.get('swap_total') or (earlier is not None and (s.get('swap') or 0) - (earlier.get('swap') or 0) > 16)
            low = self._held('memory:low', mem < 5 and growing, t, 0.01)
            pressed = (s.get('psi_mem') or 0) >= 10
            previous = list(self.samples)[-2] if len(self.samples) > 1 else None
            killed = previous is not None and s.get('oom') is not None and previous.get('oom') is not None and s['oom'] > previous['oom']
            reason = ('система убила процесс: кончилась память (OOM)' if killed else
                      f"память узла кончается: свободно {mem}%" + (', растёт подкачка' if s.get('swap_total') else '') if low else
                      f"задачи ждут память {s.get('psi_mem')}% времени")
            out['memory'] = {'due': low or pressed or killed, 'on': mem < 8 or (s.get('psi_mem') or 0) >= 5 or killed, 'reason': reason, 'critical': killed}
        if s.get('throttled') or s.get('temp') is not None:
            hot = s.get('temp') is not None and s['temp'] >= self.alarm_temp
            throttled = bool(s.get('throttled'))
            reason = ('Raspberry Pi: ' + ', '.join(s['throttled']) if throttled else '') + ('; ' if throttled and hot else '') + (f"температура {s['temp']} °C" if hot else '')
            # Тревога (её колонки говорят вслух и ночью) — только перегрев и настоящий троттлинг; недонапряжение у Pi
            # бывает неделями — это обычное сообщение о нагрузке.
            critical = hot or any(flag in ('троттлинг', 'мягкий предел температуры') for flag in s.get('throttled') or [])
            out['heat'] = {'due': hot or throttled, 'on': throttled or (s.get('temp') or 0) >= self.alarm_temp - 3, 'reason': reason or 'перегрев', 'critical': critical}
        return out

    def _evaluate(self, s: dict) -> bool:
        """Правила по замеру. True — нагрузка повышена (что-то из правил близко к порогу или уже необычно)."""
        subjects = self._subjects(s)
        for name, info in subjects.items():
            state = self.active.get(name)
            if state is not None:
                if info.get('critical') and info['due'] and not state['anomaly']['critical']:
                    # посреди «памяти мало» система убила процесс (или начался перегрев) — это уже тревога, сказать сразу
                    anomaly = state['anomaly']
                    anomaly.update(critical=True, reason=info['reason'], culprit_key=str(anomaly.get('culprit_key') or name) + ':critical')
                    self._post(self._announce, anomaly)
                if info['on'] or info['due']:
                    state['quiet'] = 0
                    state['anomaly']['peak'] = self._peak(state['anomaly'].get('peak'), s)
                else:
                    state['quiet'] += 1
                    if state['quiet'] >= 2:
                        self.active.pop(name)
                        self._end(name, state, s)
            elif info['due']:
                self._begin(name, info, s)
        for name in [n for n in self.active if n not in subjects]:  # узел выключили посреди необычного
            self.active.pop(name)
        return bool(self.active) or any(info['on'] or info['due'] for info in subjects.values())

    @staticmethod
    def _peak(peak, s):
        peak = dict(peak or {})
        for key in ('own', 'sys', 'load1', 'psi_cpu', 'psi_mem', 'psi_io', 'temp', 'swap'):
            if s.get(key) is not None:
                peak[key] = max(peak.get(key, s[key]), s[key])
        if s.get('mem') is not None:
            peak['mem'] = min(peak.get('mem', s['mem']), s['mem'])
        return peak

    def _begin(self, name: str, info: dict, s: dict) -> None:
        anomaly = {'id': time.strftime('%Y%m%d-%H%M%S', time.localtime(s['t'])) + '-' + name, 'subject': name, 'title': TITLES.get(name, name),
                   'started': s['t'], 'reason': info['reason'], 'critical': bool(info.get('critical')), 'numbers': dict(s), 'peak': self._peak(None, s),
                   'culprit': '', 'culprit_key': name, 'report': '', 'notified': False}
        self.active[name] = {'since': s['t'], 'quiet': 0, 'anomaly': anomaly, 'floor': info.get('floor')}
        self.anomalies.append(anomaly)
        if not self._capturing and time.monotonic() - self._last_capture >= CAPTURE_GAP:
            self._capturing = True
            self._last_capture = time.monotonic()
            threading.Thread(target=self._capture_run, args=(anomaly,), name='bender-load-capture', daemon=True).start()
        else:
            if name == 'stalls' and self.watch is not None and self.watch.recent:
                anomaly['culprit'] = told(self.watch.recent[-1])
            # свежий снимок (другое правило сработало только что) годится; вчерашний отчёт — нет
            when, path = self._last_report
            anomaly['report'] = path if path and time.monotonic() - when < 2 * CAPTURE_GAP else ''
            self._post(self._announce, anomaly)

    def _end(self, name: str, state: dict, s: dict) -> None:
        anomaly = state['anomaly']
        anomaly['ended'] = s['t']
        self._post(self._ended, anomaly)

    # ---------------------------------------------------------------- снимок «кто»

    def _capture_run(self, anomaly: dict) -> None:
        try:
            snap = self.capture()
            context = self._loop_context()
            report = self.report(anomaly, snap, context)
            anomaly['culprit'], anomaly['culprit_key'] = report['culprit'], report['culprit_key']
            anomaly['busy'] = [title for key, title in BUSY if context.get(key)]
            anomaly['report'] = self.save(report)
            self._last_report = (time.monotonic(), anomaly['report'])
        except Exception as exc:  # noqa: BLE001 — снимок не должен ронять сторожа; сообщение уйдёт и без него
            anomaly['error'] = f'{type(exc).__name__}: {exc}'[:300]
        finally:
            self._capturing = False
            self._post(self._announce, anomaly)

    def capture(self, seconds: float = CAPTURE_SECONDS) -> dict:
        """Кто грузит прямо сейчас: потоки Бендера и их места, готовые задачи цикла, процессы узла. Не из потока цикла."""
        node = bool(self.cfg.get('node', True))
        t0, own0, th0 = time.monotonic(), cpu.own_cpu(), cpu.thread_ticks(self.proc)
        sys0 = cpu.system_cpu(self.proc) if node else None
        scan0 = cpu.processes(self.proc) if node else None
        scanned = time.monotonic()  # проход по /proc сам занимает время: проценты процессов — по своему окну
        loop = self.loop if self.loop is not None and not self.loop.is_closed() else None
        try:
            if loop is not None and asyncio.get_running_loop() is loop:
                loop = None  # из самого цикла пробы не дождались бы ответа
        except RuntimeError:
            pass
        tasks, task_frames, probes = Counter(), {}, [0]

        def probe():
            try:
                ready = list(loop._ready)  # частный API asyncio: читается только в потоке цикла
            except Exception:  # noqa: BLE001
                return
            probes[0] += 1
            seen = set()
            for handle in ready:
                task = getattr(getattr(handle, '_callback', None), '__self__', None)
                if isinstance(task, asyncio.Task) and id(task) not in seen:
                    seen.add(id(task))
                    name = describe_task(task)
                    tasks[name] += 1
                    if name not in task_frames:
                        task_frames[name] = task_frame(task)

        from .stall import ours, place, waiting
        me = threading.get_ident()
        hot, stacks, modules, frames_seen = [], defaultdict(Counter), defaultdict(Counter), Counter()
        looked = False
        # Срок — по часам, а не по числу шагов: под чужим GIL сон в 50 мс растягивается, а снимок должен остаться коротким.
        while time.monotonic() - t0 < seconds:
            if loop is not None:
                try:
                    loop.call_soon_threadsafe(probe)
                except RuntimeError:
                    loop = None
            if not looked and time.monotonic() - t0 >= seconds / 4:  # четверть времени — понять, какие потоки горячие
                looked = True
                rows = cpu.thread_cpu(th0, cpu.thread_ticks(self.proc), time.monotonic() - t0)
                hot = [(row['ident'], row['name']) for row in rows if row['ident'] not in (None, me)][:2]
            if hot:
                frames = sys._current_frames()
                for ident, name in hot:
                    frame = frames.get(ident)
                    if frame is not None:
                        stack = place(frame, 30)
                        stacks[name]['ждёт событий или замка' if waiting(stack) else ours(stack)] += 1
                        spot = bender_frame(frame)
                        if spot:
                            modules[name][spot] += 1
                        frames_seen[name] += 1
                del frames
            time.sleep(1 / STACK_HZ)
        elapsed = time.monotonic() - t0
        own = round(100 * (cpu.own_cpu() - own0) / elapsed, 1)
        threads = cpu.thread_cpu(th0, cpu.thread_ticks(self.proc), elapsed)
        main = threading.main_thread().native_id
        out = {'seconds': round(elapsed, 2), 'own': own, 'probes': probes[0],
               'threads': [{'name': row['name'], 'pct': row['pct'], 'main': row['tid'] == main,
                            'places': [[spot, count] for spot, count in stacks.get(row['name'], Counter()).most_common(4)],
                            'frames': [[spot, count] for spot, count in modules.get(row['name'], Counter()).most_common(4)],
                            'samples': frames_seen.get(row['name'], 0)} for row in threads[:6] if row['ident'] != me],
               'tasks': [{'task': name, 'share': round(count / probes[0], 2), 'frame': task_frames.get(name, '')}
                         for name, count in tasks.most_common(5)] if probes[0] else []}
        if node:
            sys1, scan1 = cpu.system_cpu(self.proc), cpu.processes(self.proc)
            window = max(0.01, time.monotonic() - scanned)
            out['system'] = cpu.cpu_busy(sys0, sys1)
            tree = cpu.bender_tree(scan1, proc=self.proc)
            guard = getattr(self.app, 'guard', None)

            def row(item, extra):
                label = tree.get(item['pid'], '')
                return {'pid': item['pid'], 'comm': item['comm'], 'rss_mb': item['rss_mb'], **extra, 'bender': label,
                        'command': cpu.command_of(item['pid'], guard, self.proc)}
            out['top_cpu'] = [row(item, {'cpu_pct': item['cpu_pct']}) for item in cpu.top_cpu(scan0, scan1, window, 8)]
            out['top_rss'] = [row(item, {}) for item in cpu.top_rss(scan1, 6)]
            out['processes'] = len(scan1 or {})
            if scan1 is None:
                out['node_note'] = cpu.UNAVAILABLE
        return out

    def _loop_context(self, timeout: float = 2.0) -> dict:
        loop = self.loop
        if loop is None or loop.is_closed():
            return {}
        try:
            return asyncio.run_coroutine_threadsafe(self.context(), loop).result(timeout)
        except Exception:  # noqa: BLE001 — цикл занят или закрыт: отчёт без «что делает Бендер»
            return {'note': f'цикл не ответил за {timeout:g} с — он перегружен или стоит'}

    async def context(self) -> dict:
        """Что делает Бендер: ходы по агентам, фоновая полоса, бекап, сон, обновление, недавние зависания, свои процессы."""
        host = self.app
        out = {'turns': [], 'labels': {}}
        agents = getattr(host, 'agents', None)
        contexts = list(agents.loaded.items()) if agents is not None else [('main', host)]
        for aid, ctx in contexts:
            for sid, turn in list(ctx.mind.turns.items()):
                if not (turn.task and not turn.task.done()):
                    continue
                doing = turn.state.get('_doing') or {}
                out['turns'].append({'agent': aid, 'channel': turn.channel, 'actor': turn.actor, 'steps': turn.steps, 'unattended': turn.unattended,
                                     'seconds': round(time.monotonic() - turn.started), 'doing': str(doing.get('what') or '')[:120] if isinstance(doing, dict) else ''})
            for pid, label in self._labels(ctx).items():
                out['labels'][pid] = label + (f' (агент {aid})' if aid != 'main' else '')
        if agents is not None:
            free = getattr(agents.background_limit, '_value', None)
            if free is not None:
                out['background'] = {'used': agents.background_size - free, 'size': agents.background_size}
        store = host.store
        out['backup'] = bool(store.meta_get('backups.in_progress'))
        out['update'] = bool(store.meta_get('updates.in_progress'))
        out['sleep'] = bool(getattr(getattr(host, 'sleep', None), 'running', False))
        out['team'] = bool(getattr(getattr(host, 'team', None), 'running', None))
        watch = self.watch
        if watch is not None:
            out['lag'] = round(watch.lag, 2)
            out['stalls'] = [{'at': cpu.stamp(r['started']), 'seconds': r['seconds'], 'where': r['where']} for r in list(watch.recent)[-5:]]
        return out

    @staticmethod
    def _labels(ctx) -> dict:
        """Свои процессы из реестров: фоновые команды, MCP-серверы, браузер."""
        out = {}
        processes = getattr(ctx, '_processes', None)
        for key, item in list(getattr(processes, 'items', {}).items()):
            pid = getattr(item.get('process'), 'pid', None)
            if pid:
                out[pid] = f'фоновая команда {key}'
        mcp = getattr(ctx, '_mcp', None)
        for sid, entry in list(getattr(mcp, 'clients', {}).items()):
            client = entry[1] if isinstance(entry, tuple) and len(entry) > 1 else entry
            pid = getattr(getattr(getattr(client, 'transport', None), 'process', None), 'pid', None)
            if pid:
                out[pid] = f'MCP-сервер {sid}'
        browser = getattr(ctx, '_browser', None)
        pid = getattr(getattr(browser, 'proc', None), 'pid', None)
        if pid:
            out[pid] = 'браузер агента'
        return out

    # ---------------------------------------------------------------- отчёт

    def summary(self) -> dict:
        """Кольцо замеров одной строкой на величину: среднее и максимум за час."""
        items = list(self.samples)
        out = {'samples': len(items), 'minutes': round((items[-1]['t'] - items[0]['t']) / 60) if len(items) > 1 else 0}
        for key in ('own', 'sys', 'iowait', 'load1', 'mem', 'swap', 'psi_cpu', 'psi_mem', 'psi_io', 'lag', 'temp'):
            values = [s[key] for s in items if s.get(key) is not None]
            if values:
                out[key] = {'avg': round(sum(values) / len(values), 1), 'max': round(max(values), 1), 'min': round(min(values), 1)}
        return out

    def report(self, anomaly: dict, snap: dict, context: dict) -> dict:
        labels = {int(k): v for k, v in (context.get('labels') or {}).items()}
        for key in ('top_cpu', 'top_rss'):
            for item in snap.get(key) or []:
                if item['pid'] in labels:
                    item['bender'] = labels[item['pid']]
        culprit, key = blame(anomaly['subject'], snap, context, self.watch)
        return {'format': 1, 'id': anomaly['id'], 'at': anomaly['started'], 'subject': anomaly['subject'], 'title': anomaly['title'],
                'reason': anomaly['reason'], 'critical': anomaly['critical'], 'culprit': culprit, 'culprit_key': key,
                'numbers': anomaly['numbers'], 'cores': self.cores(), 'snapshot': snap, 'context': context, 'history': self.summary(),
                'recent': list(self.samples)[-30:], 'platform': cpu.platform_note()}

    def folder(self) -> Path:
        return self.app.dirs.local / 'load'

    def save(self, report: dict) -> str:
        folder = self.folder()
        folder.mkdir(parents=True, exist_ok=True)
        name = report['id']
        (folder / (name + '.json')).write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding='utf-8')
        (folder / (name + '.txt')).write_text(report_text(report), encoding='utf-8')
        keep = max(1, int(self.cfg.get('keep') or 30))
        for old in sorted(folder.glob('*.json'))[:-keep]:
            for path in (old, old.with_suffix('.txt')):
                try:
                    path.unlink()
                except OSError:
                    pass
        return f'local/load/{name}.txt'

    def latest_path(self) -> str | None:
        try:
            names = sorted(p.name for p in self.folder().glob('*.txt'))
        except OSError:
            return None
        return f'local/load/{names[-1]}' if names else None

    def latest(self) -> tuple[str, str] | None:
        path = self.latest_path()
        if not path:
            return None
        try:
            return path, (self.app.dirs.root / path).read_text(encoding='utf-8')
        except OSError:
            return None

    # ---------------------------------------------------------------- хозяину и заведующему (поток цикла)

    def _today(self) -> dict:
        today = time.strftime('%Y-%m-%d')
        if self.limits.get('date') != today:
            self.limits.update(date=today, alerts=0, manager=0)
        return self.limits

    def _remember(self) -> None:
        limits = self._today()
        cutoff = time.time() - 2 * 86400
        for key in ('said', 'asked'):
            limits[key] = {k: v for k, v in limits.get(key, {}).items() if v > cutoff}
        try:
            self.app.store.meta_set('load:limits', limits)
        except Exception:  # noqa: BLE001
            pass

    def _announce(self, anomaly: dict) -> None:
        cfg = self.cfg
        culprit = anomaly.get('culprit') or 'не определён'
        line = f"Необычная нагрузка — {anomaly['title']}: {anomaly['reason']}. Виновник: {culprit}." + (f" Отчёт: {anomaly['report']}" if anomaly.get('report') else '')
        self.app.journal.write('load', line[:600], data={'subject': anomaly['subject'], 'numbers': numbers(anomaly['numbers']), 'culprit': anomaly.get('culprit_key'),
                                                        'report': anomaly.get('report') or '', 'critical': anomaly['critical']})
        key = anomaly['subject'] + ':' + str(anomaly.get('culprit_key') or '')
        limits, now = self._today(), time.time()
        cooldown = 60 * int(cfg.get('cooldown_minutes') or 60)
        if cfg.get('notify') and now - limits['said'].get(key, 0) >= cooldown and limits['alerts'] < DAILY_ALERTS:
            limits['said'][key] = now
            limits['alerts'] += 1
            anomaly['notified'] = True
            self._remember()
            self.app.bus.publish({'type': 'notify', 'kind': 'alarm' if anomaly['critical'] else 'load', 'agent_id': '*', 'title': 'Нагрузка', 'text': alert_text(anomaly),
                                  'untrusted': foreign(anomaly)})
        if self._manager_allowed(anomaly, key):
            task = asyncio.ensure_future(self._manager(anomaly, key))
            self._manager_tasks.add(task)
            task.add_done_callback(self._manager_tasks.discard)

    def _ended(self, anomaly: dict) -> None:
        minutes = max(1, round((anomaly['ended'] - anomaly['started']) / 60))
        self.app.journal.write('load', f"Нагрузка спала через {minutes} мин — {anomaly['title']}", data={'subject': anomaly['subject'], 'minutes': minutes})
        if anomaly.get('notified') and self.cfg.get('notify'):
            self.app.bus.publish({'type': 'notify', 'kind': 'load', 'agent_id': '*', 'title': 'Нагрузка', 'untrusted': foreign(anomaly),
                                  'text': f"Нагрузка спала через {minutes} мин: {anomaly['title']}" + (f" (виновник был: {anomaly['culprit']})" if anomaly.get('culprit') else '') + '.'})

    def unavailable(self, agent: str) -> str | None:
        """None — заведующий доступен; 'gone' — удалён или в архиве; 'later' — временно (стая выключена, сбой загрузки)."""
        try:
            self.app.agents.get(agent)
            return None
        except HumanError as error:
            return 'gone' if error.code in ('agent_missing', 'agent_archived', 'agent_temporary_done', 'agent_id') else 'later'
        except Exception:  # noqa: BLE001
            return 'later'

    def _manager_allowed(self, anomaly: dict, key: str) -> bool:
        cfg = self.cfg
        agent, when = str(cfg.get('manager') or ''), cfg.get('manager_on') or 'all'
        if not agent or when == 'off' or self.app.safe or getattr(self.app.guard, 'halted', False):
            return False
        if when == 'bender' and anomaly['subject'] not in BENDER or when == 'node' and anomaly['subject'] not in NODE:
            return False
        limits = self._today()
        if limits['manager'] >= int(cfg.get('manager_per_day') or 0):
            return False
        if time.time() - limits['asked'].get(key, 0) < 60 * int(cfg.get('cooldown_minutes') or 60):
            return False
        state = self.unavailable(agent)
        if state:
            if (agent, state) not in self._fallback:
                self._fallback.add((agent, state))
                why = 'удалён или в архиве — выбери другого в «Теле» или Настройках' if state == 'gone' else 'сейчас недоступен (стая выключена или не загрузился)'
                self.app.journal.write('load', f'Заведующий узлом {agent} {why}; отчёт о нагрузке без разбора агентом', data={'agent': agent, 'state': state})
            return False
        self._fallback = {item for item in self._fallback if item[0] != agent}
        return True

    async def _manager(self, anomaly: dict, key: str) -> dict | None:
        agent = str(self.cfg.get('manager') or '')
        try:
            return await self._ask_manager(agent, anomaly, key)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 — разбор агентом не должен ронять анализ нагрузки
            title = error.title if isinstance(error, HumanError) else type(error).__name__
            self.app.journal.write('load', f'Заведующий узлом {agent} не разобрал отчёт: {title}'[:300], data={'agent': agent})
            return None

    async def _ask_manager(self, agent: str, anomaly: dict, key: str) -> dict | None:
        ctx = self.app.agents.get(agent)
        sid = node_session(ctx)
        if ctx.mind.running(sid):
            self.app.journal.write('load', f'Заведующий узлом {agent} ещё разбирает прошлый отчёт — новый не отправлен', data={'agent': agent})
            return None
        limits = self._today()
        limits['manager'] += 1
        limits['asked'][key] = time.time()
        self._remember()
        try:
            text = (self.app.dirs.root / anomaly['report']).read_text(encoding='utf-8') if anomaly.get('report') else ''
        except OSError:
            text = ''
        text = text or f"{anomaly['title']}: {anomaly['reason']}. Виновник: {anomaly.get('culprit') or 'не определён'}. Числа: {json.dumps(numbers(anomaly['numbers']), ensure_ascii=False)}"
        from ..tools.web import untrusted
        await ctx.mind.say(sid, HEAD + '\n\n' + untrusted('отчёт о нагрузке узла', text[:20000]), channel='node', actor='schedule',
                           tainted=True, unattended=True, wait=False, urgent=True, message_meta={'load': anomaly['id']})
        turn = ctx.mind.turns.get(sid)
        if turn is None or not turn.task:
            return None
        self.app.journal.write('load', f'Отчёт о нагрузке отдан заведующему узлом {agent}', data={'agent': agent, 'subject': anomaly['subject']})
        await asyncio.wait({turn.task})
        if turn.task.cancelled():
            return None
        result = turn.task.result() or {}
        answer = (result.get('text') or '').strip()
        if answer:
            await tell_owner(ctx, ICON + ' Заведующий узлом: ' + answer[:3500])
        elif result.get('error'):
            self.app.journal.write('load', f"Заведующий узлом {agent} не разобрал отчёт: {str((result['error'] or {}).get('title') or '')[:200]}",
                                   data={'agent': agent, 'error': (result['error'] or {}).get('code')})
        return result

    # ---------------------------------------------------------------- для поверхностей

    def recent(self, seconds: float) -> list[dict]:
        cutoff = time.time() - seconds
        return [s for s in list(self.samples) if s['t'] >= cutoff]

    def brief(self) -> dict:
        """Текущие числа: последний замер и средние за минуту и пять."""
        cfg = self.cfg
        with self._lock:
            active = sorted(self.active)
        out = {'enabled': bool(cfg.get('enabled')), 'node': bool(cfg.get('node')), 'cores': self.cores() if cfg.get('enabled') else None, 'active': active}
        items = list(self.samples)
        if items:
            last = items[-1]
            out.update({k: v for k, v in last.items() if k in ('own', 'sys', 'iowait', 'steal', 'load1', 'mem', 'swap', 'psi_cpu', 'psi_mem', 'psi_io', 'lag', 'temp', 'throttled')})
            for minutes in (1, 5):
                values = [s['own'] for s in self.recent(minutes * 60)]
                if values:
                    out[f'own_{minutes}m'] = round(sum(values) / len(values), 1)
            values = [s['sys'] for s in self.recent(60) if s.get('sys') is not None]
            if values:
                out['sys_1m'] = round(sum(values) / len(values), 1)
            out['at'] = last['t']
        last = self.anomalies[-1] if self.anomalies else None
        if last:
            out['last'] = {'at': last['started'], 'title': last['title'], 'reason': last['reason'], 'culprit': last.get('culprit') or '',
                           'report': last.get('report') or '', 'ended': last.get('ended')}
        return out

    def public(self) -> dict:
        reports = []
        try:
            reports = sorted((p.name for p in self.folder().glob('*.txt')), reverse=True)[:30]
        except OSError:
            pass
        return {'running': not self._stopped and self.loop is not None, 'settings': dict(self.cfg), 'now': self.brief(),
                'samples': [{k: s.get(k) for k in ('t', 'own', 'sys', 'mem', 'load1', 'lag')} for s in list(self.samples)],
                'anomalies': [{k: a.get(k) for k in ('id', 'title', 'reason', 'culprit', 'report', 'started', 'ended', 'notified', 'critical')} for a in reversed(list(self.anomalies))],
                'reports': reports, 'platform': cpu.platform_note(), 'safe': bool(self.app.safe)}

    def top_now(self, seconds: float = 0.5, limit: int = 3) -> list[dict]:
        """Кто сейчас берёт процессор узла — по требованию (/load), два прохода по /proc через полсекунды. Не из цикла."""
        if not self.cfg.get('enabled') or not self.cfg.get('node'):
            return []
        first = cpu.processes(self.proc)
        if first is None:
            return []
        time.sleep(seconds)
        second = cpu.processes(self.proc)
        tree = cpu.bender_tree(second, proc=self.proc)
        return [{**row, 'bender': tree.get(row['pid'], '')} for row in cpu.top_cpu(first, second, seconds, limit)]


# ------------------------------------------------------------------ вспомогательное

def describe_task(task) -> str:
    """Задача asyncio: имя, корутина и место — внешнее и самое глубокое в цепочке await."""
    try:
        coro = task.get_coro()
        name = getattr(coro, '__qualname__', None) or type(coro).__name__
        outer = _where(getattr(coro, 'cr_frame', None) or getattr(coro, 'gi_frame', None))
        inner, current = None, coro
        for _ in range(30):
            current = getattr(current, 'cr_await', None) or getattr(current, 'gi_yieldfrom', None)
            frame = getattr(current, 'cr_frame', None) or getattr(current, 'gi_frame', None)
            if frame is None:
                break
            inner = _where(frame)
        return f"{task.get_name()} · {name}" + (f" @ {outer}" if outer else '') + (f" → {inner}" if inner and inner != outer else '')
    except Exception:  # noqa: BLE001 — частные поля asyncio меняются от версии к версии
        return 'задача (не описать на этой версии Python)'


PACKAGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # папка пакета bender


def _module(frame) -> str:
    """bender/…/файл.py:строка — только для кода самого Бендера (для технического отчёта), иначе ''."""
    name = os.path.abspath(frame.f_code.co_filename)
    if not name.startswith(PACKAGE + os.sep):
        return ''
    return 'bender/' + os.path.relpath(name, PACKAGE).replace(os.sep, '/') + f':{frame.f_lineno}'


def bender_frame(frame) -> str:
    """Самый глубокий кадр кода Бендера в стеке потока: тяжёлый вызов библиотеки виден по своему вызывающему."""
    while frame is not None:
        spot = _module(frame)
        if spot:
            return spot
        frame = frame.f_back
    return ''


def task_frame(task) -> str:
    """Самое глубокое место кода Бендера в цепочке await задачи (у приостановленной корутины нет f_back)."""
    try:
        spot, current = '', task.get_coro()
        for _ in range(30):
            frame = getattr(current, 'cr_frame', None) or getattr(current, 'gi_frame', None)
            if frame is None:
                break
            spot = _module(frame) or spot
            current = getattr(current, 'cr_await', None) or getattr(current, 'gi_yieldfrom', None)
        return spot
    except Exception:  # noqa: BLE001 — частные поля корутин меняются от версии к версии
        return ''


def _where(frame) -> str:
    if frame is None:
        return ''
    name = frame.f_code.co_filename.replace('\\', '/')
    parts = name.split('/')
    short = '/'.join(parts[parts.index('bender') + 1:]) if 'bender' in parts else parts[-1]
    return f'{short}:{frame.f_lineno}'


def blame(subject: str, snap: dict, context: dict, watch=None) -> tuple[str, str]:
    """Главный подозреваемый: (как сказать хозяину, ключ для «не чаще» без чисел и pid)."""
    threads = snap.get('threads') or []
    tasks = snap.get('tasks') or []
    if subject == 'stalls' and watch is not None and watch.recent:
        last = watch.recent[-1]
        return told(last), 'stall:' + str(last.get('culprit') or last['where'].split(' — ')[0])[:80]
    if subject in ('node', 'memory', 'heat'):
        rows = snap.get('top_rss' if subject == 'memory' else 'top_cpu') or []
        if rows:
            top = rows[0]
            amount = f"{top['rss_mb']} МБ" if subject == 'memory' else f"{top.get('cpu_pct')}% ядра"
            who = top.get('bender') or 'не Бендер'
            # перегрев и троттлинг — свойство машины: о нём не чаще паузы, кто бы ни грел процессор сейчас
            return f"процесс {top['comm']} (pid {top['pid']}, {amount}, {who})", 'heat' if subject == 'heat' else 'process:' + top['comm']
        if snap.get('node_note'):
            return 'процессы узла недоступны на этой платформе', 'unknown'
    hot = next((t for t in threads if not t['main'] and t['pct'] >= 30), None)
    main = next((t for t in threads if t['main']), None)
    if hot and (main is None or hot['pct'] >= main['pct']):
        place = hot['places'][0][0] if hot.get('places') else ''
        return f"поток «{hot['name']}» ({hot['pct']}% ядра)" + (f": {place}" if place else ''), 'thread:' + hot['name'] + ':' + place.split(' — ')[0]
    busy_task = next((t for t in tasks if t['share'] >= 0.6), None)
    if busy_task:
        return f"задача цикла {busy_task['task']} — готова к работе в {round(busy_task['share'] * 100)}% проб (крутится без паузы)", 'task:' + busy_task['task'].split(' @ ')[0]
    if main is not None and main['pct'] >= 30:
        place = main['places'][0][0] if main.get('places') else ''
        return f"цикл Бендера ({main['pct']}% ядра)" + (f": {place}" if place else ''), 'loop:' + place.split(' — ')[0]
    rows = snap.get('top_cpu') or []
    if rows and rows[0].get('bender') and rows[0]['bender'] != 'ядро Бендера':
        top = rows[0]
        return f"процесс {top['comm']} (pid {top['pid']}, {top.get('cpu_pct')}% ядра, {top['bender']})", 'process:' + top['comm']
    return 'ядро Бендера, без явного потока или задачи', 'core'


def foreign(anomaly: dict) -> bool:
    """Виновник назван по имени чужого процесса: его может подобрать кто угодно на машине — сообщение помечается чужим."""
    return bool(anomaly.get('culprit')) and (anomaly.get('subject') in NODE or str(anomaly.get('culprit_key') or '').startswith('process:'))


def numbers(s: dict) -> dict:
    return {k: v for k, v in (s or {}).items() if k in ('own', 'sys', 'iowait', 'steal', 'load1', 'mem', 'swap', 'psi_cpu', 'psi_mem', 'psi_io', 'lag', 'temp', 'stalls')
            and isinstance(v, (int, float)) and not isinstance(v, bool)}


def numbers_line(s: dict, cores: float | None = None) -> str:
    parts = []
    if s.get('own') is not None:
        parts.append(f"Бендер {s['own']:.0f}% ядра")
    if s.get('sys') is not None:
        parts.append(f"узел занят {s['sys']:.0f}%" + (f" (iowait {s['iowait']:.0f}%)" if s.get('iowait') else ''))
    if s.get('load1') is not None:
        parts.append(f"load {s['load1']}" + (f" на {cores:g} яд." if cores else ''))
    if s.get('mem') is not None:
        parts.append(f"память свободна {s['mem']:.0f}%")
    if s.get('swap'):
        parts.append(f"подкачка {s['swap']} МБ")
    if s.get('temp') is not None:
        parts.append(f"{s['temp']} °C")
    return ' · '.join(parts)


def alert_text(anomaly: dict) -> str:
    text = f"{anomaly['reason']}."
    if anomaly.get('culprit'):
        text += f" Виновник: {anomaly['culprit']}."
    if anomaly.get('busy'):
        text += f" Сейчас идёт: {', '.join(anomaly['busy'])}."
    line = numbers_line(anomaly.get('numbers') or {})
    if line:
        text += f" {line}."
    if anomaly.get('report'):
        text += f" Отчёт: {anomaly['report']} · /load report"
    return text[:900]


def report_text(report: dict) -> str:
    """Отчёт человеческими словами: его можно целиком вставить в ИИ-чат."""
    snap, context = report.get('snapshot') or {}, report.get('context') or {}
    lines = [f"Отчёт о нагрузке Бендера · {cpu.stamp(report['at'])}",
             f"Что заметил: {report['title']} — {report['reason']}",
             f"Главный подозреваемый: {report['culprit']}",
             f"Числа в момент срабатывания: {numbers_line(report.get('numbers') or {}, report.get('cores'))}"]
    if report.get('platform'):
        lines.append('Платформа: ' + report['platform'])
    history = report.get('history') or {}
    if history.get('samples'):
        parts = [f"{title} ср {history[key]['avg']} / макс {history[key]['max']}" for key, title in (('own', 'Бендер %'), ('sys', 'узел %'), ('load1', 'load'), ('lag', 'задержка цикла, с'))
                 if key in history]
        lines.append(f"За последние {history.get('minutes', 0)} мин: " + '; '.join(parts))
    lines.append('')
    lines.append(f"Потоки Бендера за {snap.get('seconds', 0)} с (весь процесс {snap.get('own', '?')}% ядра):")
    for t in snap.get('threads') or []:
        lines.append(f"  {t['name']}{' (цикл)' if t['main'] else ''} — {t['pct']}% ядра")
        for spot, count in t.get('places') or []:
            lines.append(f"      {spot} — {count} из {t.get('samples') or count}")
    if snap.get('tasks'):
        lines.append(f"Задачи цикла, готовые к работе (из {snap.get('probes')} проб; почти всегда готова — крутится без паузы):")
        for item in snap['tasks']:
            lines.append(f"  {item['task']} — {round(item['share'] * 100)}%")
    if snap.get('system'):
        sysinfo = snap['system']
        lines.append(f"Узел за то же время: занят {sysinfo['busy']}%, iowait {sysinfo['iowait']}%, steal {sysinfo['steal']}%")
    for key, title in (('top_cpu', 'Процессы по ЦП'), ('top_rss', 'Процессы по памяти')):
        rows = snap.get(key) or []
        if rows:
            lines.append(title + ':')
            for row in rows:
                lines.append(f"  {row['pid']} {row['comm']}" + (f" — {row['cpu_pct']}% ядра" if 'cpu_pct' in row else '') + f" · {row['rss_mb']} МБ"
                             + (f" · {row['bender']}" if row.get('bender') else '') + (f" · {row['command']}" if row.get('command') else ''))
    if snap.get('node_note'):
        lines.append('Процессы узла: ' + snap['node_note'])
    lines.append('')
    lines.append('Что делает Бендер:')
    turns = context.get('turns') or []
    for turn in turns:
        lines.append(f"  ход агента {turn['agent']} ({turn['channel']}{', без хозяина' if turn['unattended'] else ''}): шагов {turn['steps']}, {turn['seconds']} с"
                     + (f", {turn['doing']}" if turn.get('doing') else ''))
    if not turns:
        lines.append('  ходов нет')
    if context.get('background'):
        lines.append(f"  безлюдные ходы: занято {context['background']['used']} из {context['background']['size']}")
    flags = [title for key, title in (('backup', 'идёт бекап'), ('update', 'идёт обновление'), ('sleep', 'идёт сон'), ('team', 'работает команда')) if context.get(key)]
    if flags:
        lines.append('  ' + ', '.join(flags))
    if context.get('note'):
        lines.append('  ' + context['note'])
    for stall in context.get('stalls') or []:
        lines.append(f"  зависание {stall['at']}: {stall['seconds']} с — {stall['where']}")
    recent = report.get('recent') or []
    if recent:
        lines.append('')
        lines.append('Последние замеры (время · Бендер % · узел % · load · память свободна % · задержка цикла):')
        for s in recent[-12:]:
            lines.append(f"  {time.strftime('%H:%M:%S', time.localtime(s['t']))} · {s.get('own')} · {s.get('sys', '—')} · {s.get('load1', '—')} · {s.get('mem', '—')} · {s.get('lag', '—')}")
    lines.append('')
    lines.append('Имена и строки запуска чужих процессов — как их видит система, без похожего на пароли и ключи.')
    return '\n'.join(lines) + '\n'


def node_session(ctx) -> str:
    """Один разговор заведующего «Узел» на все отчёты: разбор помнит прошлые случаи."""
    sid = ctx.store.meta_get('load:node_session')
    if not sid or not ctx.store.session(sid):
        sid = ctx.store.new_session('node', 'Узел')
        ctx.store.meta_set('load:node_session', sid)
    return sid


async def tell_owner(ctx, text: str) -> None:
    """Вывод заведующего — хозяину в Telegram (и в пульт), с меткой агента; строкой в его главный разговор.

    Вывод сделан из недоверенного отчёта (имена чужих процессов подбирает кто угодно): в Telegram он помечен
    чужим — ответ хозяина на него цитатой идёт заражённым ходом в главный разговор агента (с текстом вывода), и
    «сделай, как он сказал» не обходит Предохранитель."""
    from ..guard.policy import Action
    from ..mind.passport import clean
    text = clean(ctx, text)
    sid = ctx.main_session()
    decision = await ctx.guard.check(Action('msg.send', 'owner', actor='system', to_owner=True, summary='load'), session_id=sid)
    if not decision.allowed:
        return
    msg = {'role': 'event', 'content': text, 'kind': 'load'}
    msg['id'] = ctx.store.add_message(sid, msg, {'actor': 'system', 'untrusted': True})
    ctx.bus.publish({'type': 'message', 'session': sid, 'message': msg})
    ctx.bus.publish({'type': 'notify', 'kind': 'load', 'channel': 'telegram', 'title': 'Заведующий узлом', 'text': text, 'untrusted': True})
