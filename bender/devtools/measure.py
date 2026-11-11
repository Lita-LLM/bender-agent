"""Замеры по таблице целей: `bender try measure [секунд простоя]`.

Запускает настоящего Бендера (Сторож + ядро) на временной папке данных и меряет:
размер кода, время старта до ответа пульта, память Сторожа и ядра, ЦП и запись на диск в простое,
время остановки. Память, ЦП и запись — по /proc (Linux, Android); где /proc нет, эти строки — «—».

Запись на диск считается двумя способами: wchar — всё, что процесс отдал системным вызовам записи
(строже: ловит и запись в tmpfs), и write_bytes — что дошло до диска.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

from ..core.paths import PROGRAM_DIR
from ..core.errors import HumanError


def _proc(pid: int, name: str) -> str:
    try:
        return Path(f"/proc/{pid}/{name}").read_text()
    except OSError:
        return ""


def rss_mb(pid: int) -> float | None:
    for ln in _proc(pid, "status").splitlines():
        if ln.startswith("VmRSS:"):
            return int(ln.split()[1]) / 1024
    return None


def cpu_ticks(pid: int) -> int | None:
    st = _proc(pid, "stat")
    if not st:
        return None
    parts = st.rsplit(")", 1)[1].split()
    return int(parts[11]) + int(parts[12])  # utime + stime


def io_counters(pid: int) -> dict:
    out = {}
    for ln in _proc(pid, "io").splitlines():
        k, _, v = ln.partition(":")
        out[k.strip()] = int(v.strip() or 0)
    return out


def children(pid: int) -> list[int]:
    out = []
    for d in Path("/proc").iterdir() if Path("/proc").is_dir() else []:
        if d.name.isdigit():
            st = _proc(int(d.name), "stat")
            if st and int(st.rsplit(")", 1)[1].split()[1]) == pid:
                out.append(int(d.name))
    return out


def code_size() -> tuple[int, int]:
    """(байт в архиве zip, байт как есть) — пакет bender без __pycache__."""
    raw = 0
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for root, dirs, files in os.walk(PROGRAM_DIR / "bender"):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for f in files:
                p = Path(root) / f
                raw += p.stat().st_size
                z.write(p, p.relative_to(PROGRAM_DIR))
    return len(buf.getvalue()), raw


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url: str, timeout: float = 1.0, data=None):
    from ..core.web_auth import headers
    req = urllib.request.Request(url, headers=headers(data) if data is not None else {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read())


def start(data: Path, port: int) -> tuple[subprocess.Popen, float]:
    env = dict(os.environ, BENDER_NO_ANIM="1")
    for k in ("BENDER_CORE", "BENDER_WATCHED", "BENDER_SAFE", "BENDER_RESTARTED"):
        env.pop(k, None)
    env["PYTHONPATH"] = str(PROGRAM_DIR)
    t0 = time.monotonic()
    p = subprocess.Popen([sys.executable, "-m", "bender", "--data", str(data), "--no-anim", "run", "--port", str(port), "--no-browser"],
                         env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(300):
        try:
            _get(f"http://127.0.0.1:{port}/api/state",data=data)
            return p, time.monotonic() - t0
        except (OSError, HumanError):
            if p.poll() is not None:
                raise RuntimeError(f"Бендер не запустился (код {p.returncode})")
            time.sleep(0.02)
    p.kill()
    raise RuntimeError("пульт не ответил за 6 секунд")


def stop(p: subprocess.Popen) -> float:
    t0 = time.monotonic()
    p.send_signal(signal.SIGTERM)
    try:
        p.wait(timeout=20)
    except subprocess.TimeoutExpired:
        p.kill()
    return time.monotonic() - t0


def measure(idle: float = 30, progress=print) -> dict:
    res: dict = {"python": sys.version.split()[0], "platform": sys.platform, "idle_seconds": idle}
    zipped, raw = code_size()
    res["code_zip_mb"] = round(zipped / 1e6, 2)
    res["code_raw_mb"] = round(raw / 1e6, 2)
    bare = subprocess.run([sys.executable, "-c", "print(open('/proc/self/status').read())"], capture_output=True, text=True).stdout
    for ln in bare.splitlines():
        if ln.startswith("VmRSS:"):
            res["python_bare_mb"] = round(int(ln.split()[1]) / 1024, 1)
    tmp = Path(tempfile.mkdtemp(prefix="bender-measure-"))
    try:
        port = free_port()
        progress("Первый запуск (новая папка данных)…")
        p, t_first = start(tmp / "data", port)
        stop(p)
        res["start_first_s"] = round(t_first, 2)
        progress("Обычный запуск…")
        times = []
        for _ in range(2):
            p, t = start(tmp / "data", port)
            times.append(t)
            if _ < 1:
                stop(p)
        res["start_s"] = round(min(times), 2)
        kids = children(p.pid)
        core = kids[0] if kids else None
        if core:
            time.sleep(3)  # дать ядру успокоиться после старта
            progress(f"Простой {idle:.0f} с без событий…")
            c0, i0, t0 = cpu_ticks(core), io_counters(core), time.monotonic()
            w0 = cpu_ticks(p.pid)
            time.sleep(idle)
            c1, i1, t1 = cpu_ticks(core), io_counters(core), time.monotonic()
            w1 = cpu_ticks(p.pid)
            hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
            res["core_rss_mb"] = round(rss_mb(core) or 0, 1)
            res["watchdog_rss_mb"] = round(rss_mb(p.pid) or 0, 1)
            if res.get("python_bare_mb"):
                res["watchdog_extra_mb"] = round(res["watchdog_rss_mb"] - res["python_bare_mb"], 1)
            res["core_cpu_pct"] = round(100 * (c1 - c0) / hz / (t1 - t0), 3) if c0 is not None else None
            res["watchdog_cpu_pct"] = round(100 * (w1 - w0) / hz / (t1 - t0), 3) if w0 is not None else None
            res["idle_wchar_bytes"] = i1.get("wchar", 0) - i0.get("wchar", 0) if i0 else None
            res["idle_write_bytes"] = i1.get("write_bytes", 0) - i0.get("write_bytes", 0) if i0 else None
        res["stop_s"] = round(stop(p), 2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return res


TARGETS = [
    ("code_zip_mb", "Код + интерфейс (zip)", "≤ 3 МБ", lambda v: v <= 3, "МБ"),
    ("core_rss_mb", "Память ядра в простое", "≤ 40 МБ", lambda v: v <= 40, "МБ"),
    ("watchdog_rss_mb", "Память Сторожа", "≤ 10 МБ", lambda v: v <= 10, "МБ"),
    ("watchdog_extra_mb", "Сторож сверх голого Python", "≤ 1.5 МБ", lambda v: v <= 1.5, "МБ"),
    ("core_cpu_pct", "ЦП ядра в простое", "< 0.5%", lambda v: v < 0.5, "%"),
    ("idle_wchar_bytes", "Запись в простое (системные вызовы)", "0 байт", lambda v: v == 0, "байт"),
    ("idle_write_bytes", "Запись в простое (до диска)", "0 байт", lambda v: v == 0, "байт"),
    ("start_s", "Старт до ответа пульта", "≤ 1.5 с на ПК", lambda v: v <= 1.5, "с"),
]


def table(res: dict) -> list[tuple[str, str, str, str]]:
    rows = []
    for key, title, goal, ok, unit in TARGETS:
        v = res.get(key)
        if v is None:
            rows.append((title, goal, "—", " "))
            continue
        mark = "✓" if ok(v) else "✗"
        if key == "watchdog_rss_mb" and mark == "✗" and (res.get("watchdog_extra_mb") or 99) <= 1.5:
            mark = "~"  # сам голый Python этой сборки тяжелее цели — Сторож тут ни при чём
        rows.append((title, goal, f"{v} {unit}", mark))
    return rows


def main(rest: list[str], term) -> int:
    idle = float(rest[0]) if rest and rest[0].replace(".", "", 1).isdigit() else 30.0
    as_json = "--json" in rest
    res = measure(idle, progress=(lambda s: None) if as_json else (lambda s: term.line(term.dim("  " + s))))
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=1))
        return 0
    term.line()
    for title, goal, got, mark in table(res):
        color = term.green if mark == "✓" else term.red if mark == "✗" else term.amber if mark == "~" else term.dim
        term.line(f"  {color(mark)} {title:<38} {got:>14}   {term.dim('цель ' + goal)}")
    term.line(term.dim(f"  Первый запуск: {res.get('start_first_s')} с · остановка: {res.get('stop_s')} с · голый Python: {res.get('python_bare_mb')} МБ · "
                       f"Python {res['python']} · простой {res['idle_seconds']:.0f} с"))
    return 0 if all(m != "✗" for *_, m in table(res)) else 1
