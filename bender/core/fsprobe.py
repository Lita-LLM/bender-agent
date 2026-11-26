"""Файловая система под данными Бендера: на чём она лежит и не зависает ли сама по себе.

Цикл вставал на 5–20 секунд на самых дешёвых вызовах — чтение настроек, stat файла секретов, realpath — при
свободном процессоре и iowait 0%. Замок базы к этому не причём: он справедливый, а держатель назван в отчёте.
Что именно висит — диск, файловая система, виртуалка или сам процесс, по стеку главного потока не видно: он везде
стоит в вызове, который «обычно мгновенный». Поэтому рядом с главным потоком живёт отдельный поток — канарейка.
Раз в секунду она делает stat базы, раз в пять секунд читает базу отдельным соединением SQLite, раз в минуту пишет
байт с fsync. О замках и цикле она ничего не знает: если канарейка зависла там же и тогда же, где цикл, — висит
файловая система (или носитель под ней), а не код Бендера; если она отвечала мгновенно — виноват не диск.

Здесь же — на чём лежат данные (тип файловой системы, носитель): сетевые и пользовательские файловые системы,
SD-карты, USB-накопители и вращающиеся диски засыпают и «задумываются» на секунды, а SQLite на сетевой ФС ещё и ломается.
Только стандартная библиотека; /proc и /sys подменяются в тестах."""
from __future__ import annotations

import os
import re
import sqlite3
import threading
import time
from collections import deque
from pathlib import Path
from urllib.parse import quote

from ..body import load as cpu

INTERVAL = 1.0           # stat базы — раз в секунду
READ_EVERY = 5           # чтение SQLite — раз в 5 секунд
WRITE_EVERY = 60         # запись с fsync — раз в минуту
KEEP = 720               # замеров каждого вида в памяти
EVENTS = 50              # медленных проб в памяти
SLOW = {'stat': 0.3, 'read': 0.5, 'fsync': 1.5}   # с какой задержки проба считается зависанием, секунд
NAMES = {'stat': 'stat базы', 'read': 'чтение SQLite', 'fsync': 'запись с fsync'}
PROBE_FILE = '.fs-probe'

SYSTEM_FUSE = frozenset({'fuse.lxcfs', 'fuse.gvfsd-fuse', 'fuse.portal'})   # служебные FUSE рабочего стола и контейнера: данные Бендера на них не лежат
NETWORK = frozenset({'nfs', 'nfs4', 'cifs', 'smb3', 'smbfs', '9p', 'afs', 'ceph', 'glusterfs', 'davfs', 'ncpfs', 'lustre', 'beegfs', 'vboxsf', 'virtiofs'})
KINDS = {'mmc': 'SD-карта или eMMC', 'usb': 'USB-накопитель', 'nvme': 'NVMe-накопитель', 'virtual': 'виртуальный диск', 'dm': 'LVM или шифрованный том',
         'md': 'программный RAID', 'zram': 'сжатая память', 'loop': 'файл-образ', 'sata': ''}


def _unescape(value: str) -> str:
    """Пробелы и прочее в путях mountinfo записаны восьмеричными \\040."""
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m[1], 8)), value)


def mounts(proc: Path = cpu.PROC) -> list[dict]:
    """Точки монтирования из /proc/self/mountinfo: {dev 'мажор:минор', mount, fstype, source}."""
    out = []
    for line in (cpu._read(proc / 'self' / 'mountinfo', 1 << 20) or '').splitlines():
        left, sep, right = line.partition(' - ')
        a, b = left.split(), right.split()
        if not sep or len(a) < 5 or len(b) < 2:
            continue
        out.append({'dev': a[2], 'mount': _unescape(a[4]), 'fstype': b[0], 'source': _unescape(b[1])})
    return out


def mount_of(path: str, proc: Path = cpu.PROC) -> dict | None:
    """Самая глубокая точка монтирования, под которой лежит путь (при равных — позднее монтирование: оно перекрывает раннее)."""
    best = None
    for item in mounts(proc):
        point = item['mount'].rstrip('/')
        if (path == item['mount'] or path.startswith(point + '/')) and (best is None or len(point) >= len(best['mount'].rstrip('/'))):
            best = item
    return best


def block_of(dev: str, sys_root: Path = cpu.SYS) -> dict | None:
    """Носитель по номеру устройства: name (раздел), whole (диск), rotational, bus, model. None — виртуальная ФС или нет /sys."""
    real = os.path.realpath(os.path.join(os.fspath(sys_root), 'dev', 'block', dev))
    if not os.path.isdir(real):
        return None
    partition = os.path.exists(os.path.join(real, 'partition'))
    whole = os.path.dirname(real) if partition else real
    name, wname = os.path.basename(real), os.path.basename(whole)

    def read(rel):
        return (cpu._read(os.path.join(whole, rel), 256) or '').strip()

    if wname.startswith('mmcblk'):
        bus = 'mmc'
    elif wname.startswith('nvme'):
        bus = 'nvme'
    elif '/usb' in real:
        bus = 'usb'
    elif wname.startswith(('vd', 'xvd')) or 'virtio' in real:
        bus = 'virtual'
    elif wname.startswith(('dm-', 'md', 'zram', 'loop')):
        bus = 'dm' if wname.startswith('dm-') else 'md' if wname.startswith('md') else 'zram' if wname.startswith('zram') else 'loop'
    else:
        bus = 'sata' if '/ata' in real else ''
    # Виртуальные диски и тома по умолчанию называют себя вращающимися; настоящий HDD — только на SATA или USB.
    return {'name': name, 'whole': wname, 'rotational': read('queue/rotational') == '1' and bus in ('sata', 'usb'), 'bus': bus, 'model': read('device/model')}


def describe(mount: dict | None, block: dict | None) -> tuple[str, str]:
    """(строка для людей, предупреждение или ''). Предупреждение — только для носителей и файловых систем, которые подолгу молчат."""
    if not mount:
        return 'неизвестна', ''
    fstype, source = mount['fstype'], mount['source']
    if source.startswith('/dev/'):
        base = f'{fstype} на {source}'
    elif source and source != 'none' and fstype not in ('tmpfs', 'overlay', 'overlayfs'):
        base = f'{fstype} ({source})'
    else:
        base = fstype
    if fstype in NETWORK:
        return f'{base} — сетевая файловая система', ('данные лежат на сетевой файловой системе: SQLite на ней подвисает на секунды и может испортить базу — '
                                                          'перенеси каталог данных на локальный диск')
    if fstype.startswith('fuse') and fstype not in SYSTEM_FUSE:
        return f'{base} — пользовательская файловая система (FUSE)', 'данные лежат на FUSE: каждый вызов идёт через другой процесс и подвисает вместе с ним'
    if fstype in ('overlay', 'overlayfs'):
        base += ' — слой контейнера'
    warn = ''
    if block:
        kind = ''
        if block['rotational']:
            kind = 'вращающийся USB-диск' if block['bus'] == 'usb' else 'вращающийся диск'
            warn = 'вращающийся диск засыпает: первое обращение после простоя ждёт раскрутки — это секунды'
        else:
            kind = KINDS.get(block['bus']) or ('SSD' if block['bus'] == 'sata' else '')
            if block['bus'] == 'usb':
                warn = 'USB-накопитель отключает питание в простое и «задумывается» на секунды'
            elif block['bus'] == 'mmc':
                warn = 'SD-карта и eMMC иногда замирают на секунды при сборке мусора под записью'
        if kind:
            base += f', {kind}' + (f" «{block['model']}»" if block['model'] else '')
    return base, warn


def where(path, proc: Path = cpu.PROC, sys_root: Path = cpu.SYS) -> dict:
    """На чём лежит путь: {'text', 'warn', 'hard', 'fstype', 'device'}. hard — сетевая или пользовательская ФС (хозяину скажут сразу);
    device — имя раздела в /proc/diskstats ('' — не блочное устройство)."""
    try:
        real = os.path.realpath(os.fspath(path))
    except OSError:
        real = os.path.abspath(os.fspath(path))
    mount = mount_of(real, proc)
    block = None
    if mount and mount['fstype'] not in NETWORK and not mount['fstype'].startswith(('tmpfs', 'proc', 'sysfs', 'devtmpfs')):
        try:
            block = block_of(mount['dev'], sys_root)
        except OSError:
            block = None
    text, warn = describe(mount, block)
    hard = bool(mount) and (mount['fstype'] in NETWORK or (mount['fstype'].startswith('fuse') and mount['fstype'] not in SYSTEM_FUSE))
    return {'text': text, 'warn': warn, 'hard': hard, 'fstype': mount['fstype'] if mount else '', 'device': block['name'] if block else ''}


class FsCanary:
    """Поток-канарейка: проверяет, отвечает ли файловая система с данными, независимо от цикла, замков и главного потока."""

    def __init__(self, db: Path | str, folder: Path | str | None = None, *, interval: float = INTERVAL, proc: Path = cpu.PROC, sys_root: Path = cpu.SYS):
        self.db = Path(db)
        self.folder = Path(folder) if folder else self.db.parent
        self.interval = interval
        self.proc, self.sys_root = proc, sys_root
        self.enabled = True
        self.series = {kind: deque(maxlen=KEEP) for kind in SLOW}    # (когда, сколько секунд)
        self.slow: deque = deque(maxlen=EVENTS)                       # {'at', 'kind', 'seconds'} — проба дольше порога
        self.errors = 0
        self.info: dict = {}
        self._busy = None            # (вид, monotonic начала) — проба, которая идёт прямо сейчас
        self._stop = threading.Event()
        self._thread = None

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name='bender-fs-canary', daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        try:
            self.info = where(self.db, self.proc, self.sys_root)
        except Exception:  # noqa: BLE001 — описание носителя не должно мешать пробам
            self.info = {}
        tick = 1
        while not self._stop.wait(self.interval):
            if self.enabled:
                self.probe('stat', self._stat)
                if tick % READ_EVERY == 0:
                    self.probe('read', self._read)
                if tick % WRITE_EVERY == 0:
                    self.probe('fsync', self._write)
            tick += 1

    def probe(self, kind: str, action) -> float:
        """Одна проба с замером времени. Идущая проба видна в status(): повисшая канарейка — главная улика."""
        started = time.monotonic()
        self._busy = (kind, started)
        try:
            action()
        except (OSError, sqlite3.Error):
            self.errors += 1
        finally:
            self._busy = None
        seconds = time.monotonic() - started
        now = time.time()
        self.series[kind].append((now, seconds))
        if seconds >= SLOW[kind]:
            self.slow.append({'at': now - seconds, 'kind': kind, 'seconds': round(seconds, 2)})
        return seconds

    def _stat(self) -> None:
        # База и каталог рядом с ней (там же — stat файла секретов, отчёты, копии): зависание в соседнем каталоге тоже видно.
        try:
            os.stat(self.db)
        except FileNotFoundError:
            pass
        os.stat(self.folder)

    def _read(self) -> None:
        # Отдельное соединение только для чтения: ни общего замка Бендера, ни его соединения оно не касается.
        conn = sqlite3.connect('file:' + quote(os.fspath(self.db)) + '?mode=ro', uri=True, timeout=0.5, check_same_thread=False)
        try:
            conn.execute('SELECT count(*) FROM sqlite_master').fetchone()
        finally:
            conn.close()

    def _write(self) -> None:
        fd = os.open(os.fspath(self.folder / PROBE_FILE), os.O_WRONLY | os.O_CREAT, 0o600)
        try:
            os.write(fd, b'1')
            os.fsync(fd)
        finally:
            os.close(fd)

    # ---------------------------------------------------------------- что видно снаружи

    def status(self) -> dict | None:
        """Проба, которая идёт сейчас: {'kind', 'seconds'}; None — канарейка отвечает."""
        busy = self._busy
        if busy is None:
            return None
        return {'kind': busy[0], 'seconds': round(time.monotonic() - busy[1], 1)}

    def during(self, start: float, end: float) -> list[dict]:
        """Пробы, которые шли дольше порога за отрезок (time.time()), и идущая сейчас: {'kind', 'seconds', 'ongoing'}."""
        found = [{'kind': e['kind'], 'seconds': e['seconds'], 'ongoing': False} for e in list(self.slow) if e['at'] < end and e['at'] + e['seconds'] > start]
        now = self.status()
        if now and now['seconds'] >= SLOW[now['kind']]:
            found.append({'kind': now['kind'], 'seconds': now['seconds'], 'ongoing': True})
        return found

    def worst(self, kind: str, seconds: float = 3600.0) -> float:
        cutoff = time.time() - seconds
        return max((s for t, s in list(self.series[kind]) if t >= cutoff), default=0.0)

    def line(self) -> str:
        """Одна строка для /load: на чём лежат данные и как отвечала файловая система за час."""
        info = self.info
        if not info:
            return ''
        text = f"Данные: {info['text']}. " if info.get('text') != 'неизвестна' else ''   # ОС без /proc о файловой системе молчит
        if not self.enabled:
            return text + 'Проба файловой системы выключена.'
        worst = {kind: self.worst(kind) for kind in SLOW}
        text += 'Проба за час, самое долгое: ' + ', '.join(f"{NAMES[kind]} {ms(worst[kind])}" for kind in SLOW if self.series[kind]) + '.'
        hangs = [e for e in list(self.slow) if e['at'] >= time.time() - 3600]
        if hangs:
            top = max(hangs, key=lambda e: e['seconds'])
            text += f" Зависаний файловой системы за час: {len(hangs)} (дольше всех {top['seconds']:g} с, {NAMES[top['kind']]}, {time.strftime('%H:%M', time.localtime(top['at']))})."
        else:
            text += ' Зависаний не было.'
        if info.get('warn'):
            text += ' Внимание: ' + info['warn'] + '.'
        return text


def ms(seconds: float) -> str:
    return f'{seconds * 1000:.1f} мс'.replace('.', ',') if seconds < 1 else f'{seconds:.1f} с'.replace('.', ',')
