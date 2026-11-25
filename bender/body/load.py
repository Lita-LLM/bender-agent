"""Нагрузка: дешёвые счётчики процессора и памяти — свои, по потокам, всего узла и чужих процессов.

Только стандартная библиотека. Каждое чтение стоит микросекунды; любое из них может быть недоступно
(нет /proc на Windows и macOS, Android не даёт /proc/stat и чужие процессы) — тогда функция возвращает
None, а не падает. Корень /proc и /sys подменяется в тестах (proc=, sys_root=).

Чужие имена и строки запуска — недоверенные данные: в них бывают токены и пароли, и их может подобрать
кто угодно на машине. redact() маскирует похожее на секреты и ключи из Сейфа; модели они уходят только
внутри untrusted().
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from pathlib import Path

PROC = Path('/proc')
SYS = Path('/sys')
try:
    HZ = os.sysconf('SC_CLK_TCK') if hasattr(os, 'sysconf') else 100
except (OSError, ValueError):
    HZ = 100
try:
    PAGE = os.sysconf('SC_PAGE_SIZE') if hasattr(os, 'sysconf') else 4096
except (OSError, ValueError):
    PAGE = 4096
UNAVAILABLE = 'недоступно'
THROTTLE = Path('devices/platform/soc/soc:firmware/get_throttled')  # Raspberry Pi: прошивка сообщает о недонапряжении и перегреве


def _read(path, size: int = 65536) -> str | None:
    """Небольшой файл /proc или /sys одним системным вызовом (в разы дешевле pathlib); None — нет или нельзя."""
    try:
        fd = os.open(os.fspath(path), os.O_RDONLY)
    except (OSError, ValueError):
        return None
    try:
        return os.read(fd, size).decode('utf-8', 'replace')
    except OSError:
        return None
    finally:
        os.close(fd)


# ------------------------------------------------------------------ сам Бендер

def own_cpu() -> float:
    """Секунды процессора своего процесса (пользователь + ядро). Работает везде, где есть os.times."""
    t = os.times()
    return t.user + t.system


def thread_ticks(proc: Path = PROC) -> dict[int, int] | None:
    """Тики процессора каждого потока своего процесса по /proc/self/task: {native_id: тики}."""
    base = os.fspath(proc / 'self' / 'task') + os.sep
    try:
        names = os.listdir(base)
    except OSError:
        return None
    out = {}
    for name in names:
        text = _read(base + name + '/stat', 1024)
        if not text:
            continue
        try:
            parts = text.rsplit(')', 1)[1].split()
            out[int(name)] = int(parts[11]) + int(parts[12])
        except (IndexError, ValueError):
            continue
    return out


def python_threads() -> dict[int, tuple[str, int]]:
    """{native_id: (имя потока, ident)} — по ним тики /proc сопоставляются с потоками Python
    (в /proc все потоки называются одинаково, имя знает только threading)."""
    out = {}
    for thread in threading.enumerate():
        native = getattr(thread, 'native_id', None)
        if native is not None and thread.ident is not None:
            out[native] = (thread.name, thread.ident)
    return out


def thread_cpu(before: dict | None, after: dict | None, seconds: float) -> list[dict]:
    """Потоки по убыванию ЦП между двумя замерами thread_ticks: имя, ident, % одного ядра и секунды."""
    if not before or not after or seconds <= 0:
        return []
    names = python_threads()
    rows = []
    for tid, ticks in after.items():
        spent = (ticks - before.get(tid, 0)) / HZ  # поток родился после первого замера — всё его время новое
        if spent <= 0:
            continue
        name, ident = names.get(tid, (f'поток {tid} (не Python)', None))
        rows.append({'tid': tid, 'name': name, 'ident': ident, 'seconds': round(spent, 2), 'pct': round(100 * spent / seconds, 1)})
    rows.sort(key=lambda row: row['seconds'], reverse=True)
    return rows


# ------------------------------------------------------------------ узел

CPU_FIELDS = ('user', 'nice', 'system', 'idle', 'iowait', 'irq', 'softirq', 'steal')


def system_cpu(proc: Path = PROC) -> dict | None:
    """Счётчики первой строки /proc/stat. None — нет /proc или Android не даёт его читать."""
    text = _read(proc / 'stat', 512)  # первая строка; дальше на многоядерной машине — килобайты прерываний
    if not text or not text.startswith('cpu '):
        return None
    try:
        values = [int(x) for x in text.split('\n', 1)[0].split()[1:9]]
    except ValueError:
        return None
    values += [0] * (len(CPU_FIELDS) - len(values))
    return dict(zip(CPU_FIELDS, values))


def cpu_busy(before: dict | None, after: dict | None) -> dict | None:
    """Доли занятости узла между двумя system_cpu: busy (всё, кроме простоя и ожидания диска), iowait, steal — в %."""
    if not before or not after:
        return None
    delta = {key: after[key] - before.get(key, 0) for key in CPU_FIELDS}
    total = sum(delta.values())
    if total <= 0:
        return None
    idle = delta['idle'] + delta['iowait']
    return {'busy': round(100 * (total - idle) / total, 1), 'iowait': round(100 * delta['iowait'] / total, 1), 'steal': round(100 * delta['steal'] / total, 1)}


def loadavg() -> tuple[float, float, float] | None:
    try:
        return tuple(round(x, 2) for x in os.getloadavg())
    except (OSError, AttributeError):
        return None


def pressure(proc: Path = PROC) -> dict | None:
    """PSI: какая доля времени задачи ждали процессора, памяти и диска (avg10, %). None — ядро без PSI."""
    out = {}
    for resource in ('cpu', 'memory', 'io'):
        text = _read(proc / 'pressure' / resource)
        if not text:
            continue
        for line in text.splitlines():
            kind, _, rest = line.partition(' ')
            for item in rest.split():
                key, _, value = item.partition('=')
                if key == 'avg10':
                    try:
                        out[f'{resource}_{kind}'] = float(value)
                    except ValueError:
                        pass
    return out or None


def memory(proc: Path = PROC) -> dict | None:
    """Память и подкачка узла (МБ) и счётчик убийств OOM из /proc/vmstat."""
    text = _read(proc / 'meminfo')
    if not text:
        return None
    info = {}
    for line in text.splitlines():
        key, _, value = line.partition(':')
        if key in ('MemTotal', 'MemAvailable', 'SwapTotal', 'SwapFree'):
            try:
                info[key] = int(value.split()[0]) // 1024
            except (ValueError, IndexError):
                pass
    if not info.get('MemTotal'):
        return None
    out = {'total_mb': info['MemTotal'], 'available_mb': info.get('MemAvailable'), 'swap_total_mb': info.get('SwapTotal', 0),
           'swap_used_mb': info.get('SwapTotal', 0) - info.get('SwapFree', 0)}
    if out['available_mb'] is not None:
        out['available_pct'] = round(100 * out['available_mb'] / info['MemTotal'], 1)
    vm = _read(proc / 'vmstat') or ''
    for line in vm.splitlines():
        if line.startswith('oom_kill '):
            try:
                out['oom_kill'] = int(line.split()[1])
            except (ValueError, IndexError):
                pass
    return out


def effective_cores(sys_root: Path = SYS) -> float:
    """Сколько ядер действительно доступно: привязка процесса и квота cgroup (контейнер, systemd CPUQuota)."""
    try:
        cores = float(len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        cores = float(os.cpu_count() or 1)
    text = _read(sys_root / 'fs' / 'cgroup' / 'cpu.max')
    if text:
        parts = text.split()
        try:
            if len(parts) == 2 and parts[0] != 'max':
                cores = min(cores, max(0.1, int(parts[0]) / int(parts[1])))
        except (ValueError, ZeroDivisionError):
            pass
    return round(cores, 2)


def throttled(sys_root: Path = SYS) -> dict | None:
    """Raspberry Pi: недонапряжение, урезанная частота и перегрев — сейчас и с момента загрузки. None — файла нет."""
    text = _read(sys_root / THROTTLE)
    if not text:
        return None
    try:
        value = int(text.strip(), 16)
    except ValueError:
        return None
    names = ('недонапряжение', 'частота урезана', 'троттлинг', 'мягкий предел температуры')
    return {'raw': value, 'now': [names[i] for i in range(4) if value >> i & 1], 'since_boot': [names[i] for i in range(4) if value >> (16 + i) & 1]}


_zones: dict[str, list[str]] = {}


def temperature(sys_root: Path = SYS) -> float | None:
    """Самый горячий датчик /sys/class/thermal, °C. Список датчиков ищется один раз: каждый замер — только чтения."""
    key = os.fspath(sys_root)
    zones = _zones.get(key)
    if zones is None:
        base = os.path.join(key, 'class', 'thermal')
        try:
            zones = [os.path.join(base, name, 'temp') for name in sorted(os.listdir(base)) if name.startswith('thermal_zone')]
        except OSError:
            zones = []
        _zones[key] = zones
    values = []
    for path in zones:
        text = _read(path, 64)
        try:
            value = int((text or '').strip()) / 1000
        except ValueError:
            continue
        if 0 < value < 150:
            values.append(value)
    return round(max(values), 1) if values else None


def boot_id(proc: Path = PROC) -> str | None:
    text = _read(proc / 'sys' / 'kernel' / 'random' / 'boot_id')
    return text.strip() if text else None


# ------------------------------------------------------------------ процессы

def processes(proc: Path = PROC) -> dict[int, dict] | None:
    """Один проход по /proc/[pid]/stat: имя, родитель, тики, время старта, память. None — /proc недоступен.
    Чужие процессы, которые не дают читать (Android), просто пропускаются."""
    try:
        names = os.listdir(proc)
    except OSError:
        return None
    out = {}
    base = os.fspath(proc) + os.sep
    for name in names:
        if not name.isdigit():
            continue
        text = _read(base + name + '/stat', 1024)
        if not text:
            continue
        try:
            head, tail = text.rsplit(')', 1)
            parts = tail.split()
            out[int(name)] = {'pid': int(name), 'comm': head.split('(', 1)[1][:32], 'ppid': int(parts[1]), 'ticks': int(parts[11]) + int(parts[12]),
                              'start': int(parts[19]), 'rss_mb': round(int(parts[21]) * PAGE / 2**20, 1)}
        except (IndexError, ValueError):
            continue
    return out


def top_cpu(before: dict | None, after: dict | None, seconds: float, limit: int = 8) -> list[dict]:
    """Процессы по ЦП между двумя проходами. Номер процесса сверяется со временем старта: переиспользованный pid — новый процесс."""
    if not before or not after or seconds <= 0:
        return []
    rows = []
    for pid, now in after.items():
        old = before.get(pid)
        if not old or old['start'] != now['start']:
            continue
        spent = (now['ticks'] - old['ticks']) / HZ
        if spent > 0:
            rows.append({**now, 'cpu_pct': round(100 * spent / seconds, 1)})
    rows.sort(key=lambda row: row['cpu_pct'], reverse=True)
    return rows[:limit]


def top_rss(scan: dict | None, limit: int = 8) -> list[dict]:
    return sorted((scan or {}).values(), key=lambda row: row['rss_mb'], reverse=True)[:limit]


def cmdline(pid: int, proc: Path = PROC) -> list[str]:
    try:
        raw = (proc / str(pid) / 'cmdline').read_bytes()
    except OSError:
        return []
    return [part.decode('utf-8', 'replace') for part in raw.split(b'\0') if part][:64]


def bender_tree(scan: dict | None, core: int | None = None, proc: Path = PROC) -> dict[int, str]:
    """Процессы Бендера: ядро, все его потомки, Сторож-родитель и служба локального голоса (она не потомок ядра)."""
    core = core or os.getpid()
    labels = {core: 'ядро Бендера'}
    if not scan:
        return labels
    if os.environ.get('BENDER_WATCHED') and core == os.getpid():
        labels[os.getppid()] = 'Сторож Бендера'
    children: dict[int, list[int]] = {}
    for pid, row in scan.items():
        children.setdefault(row['ppid'], []).append(pid)
    queue = [core]
    while queue:
        for child in children.get(queue.pop(), []):
            if child not in labels:
                labels[child] = 'запущен Бендером'
                queue.append(child)
    for pid, row in scan.items():
        if pid in labels or not row['comm'].lower().startswith('python'):
            continue
        args = cmdline(pid, proc)
        if any(arg.endswith('local_server.py') for arg in args) or ('bender' in args and 'voice' in args and 'serve' in args):
            labels[pid] = 'служба голоса Бендера'
    return labels


# ------------------------------------------------------------------ чужие строки

_SECRETS = [
    (re.compile(r'(?i)(bearer|token|basic)(\s+)[A-Za-z0-9._~+/=-]{8,}'), r'\1\2[скрыто]'),
    (re.compile(r'(?i)(?<![\w.-])(--?[\w.-]*(?:pass(?:word)?|passwd|pwd|token|secret|key|auth|cred\w*|cookie|session)[\w.-]*)(=|\s+)(\S+)'), r'\1\2[скрыто]'),
    (re.compile(r'(?i)(?<![\w.-])([\w.-]*(?:pass(?:word)?|passwd|pwd|token|secret|apikey|api_key|key|auth|cookie|session)[\w.-]*)=(\S+)'), r'\1=[скрыто]'),
    (re.compile(r'(?i)(\w+://)[^/\s:@]+:[^/\s@]*@'), r'\1[скрыто]@'),
    (re.compile(r'(?i)(\w+://)[^/\s:@]+@'), r'\1[скрыто]@'),
    (re.compile(r'\b(?:sk|pk|rk|ghp|gho|ghs|xox[abprs]|glpat|AIza)[-_A-Za-z0-9]{10,}'), '[скрыто]'),
    (re.compile(r'\b\d{6,}:[A-Za-z0-9_-]{20,}\b'), '[скрыто]'),  # токен Telegram-бота
    # длинные base64/hex: ключи, хеши, подписи (буквы вместе с цифрами; пути с «/» и словами остаются)
    (re.compile(r'(?=[A-Za-z0-9+=_-]*\d)(?=[A-Za-z0-9+=_-]*[A-Za-z])[A-Za-z0-9+=_-]{24,}'), '[скрыто]'),
]


def redact(text, guard=None, limit: int = 300) -> str:
    """Строка запуска чужого процесса для отчёта: без похожего на пароли, токены и ключи; ключи Сейфа — через guard.scrub."""
    if isinstance(text, (list, tuple)):
        text = ' '.join(str(part) for part in text)
    text = str(text or '')
    cut = len(text) > 3 * limit
    text = re.sub(r'[\x00-\x1f\x7f]+', ' ', text[:3 * limit])  # аргумент бывает мегабайтным: маски — только по видимому началу
    for pattern, replacement in _SECRETS:
        text = pattern.sub(replacement, text)
    if guard is not None:
        try:
            text = guard.scrub(text)
        except Exception:  # noqa: BLE001 — маскировка не должна ронять отчёт
            pass
    return text[:limit] + ('…' if cut or len(text) > limit else '')


def command_of(pid: int, guard=None, proc: Path = PROC) -> str:
    """Короткая строка запуска: имя программы и несколько аргументов, без секретов."""
    args = cmdline(pid, proc)
    if not args:
        return ''
    args = [Path(args[0]).name] + args[1:8]
    return redact(args, guard, limit=200)


def platform_note() -> str:
    if sys.platform.startswith('win'):
        return 'Windows: только ЦП самого Бендера и средняя нагрузка недоступна'
    if sys.platform == 'darwin':
        return 'macOS: только ЦП самого Бендера и средняя нагрузка'
    if os.environ.get('TERMUX_VERSION'):
        return 'Android: система может не давать читать нагрузку узла и чужие процессы'
    return ''


def stamp(ts: float | None = None) -> str:
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ts if ts is not None else time.time()))
