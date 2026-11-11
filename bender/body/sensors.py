"""Датчики тела: машина, на которой живёт агент, и он сам. Читаются дёшево и только по запросу.

Без циклов опроса: пульс или тул status спросили — прочитали. На Linux и Termux — из /proc и /sys,
батарея в Termux — termux-battery-status (если стоит termux-api), остальное — переносимо.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
import time
from pathlib import Path

_START = time.time()


def _read(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def rss_mb(pid: int | None = None) -> float | None:
    status = _read(f"/proc/{pid or 'self'}/status")
    for ln in status.splitlines():
        if ln.startswith("VmRSS:"):
            return round(int(ln.split()[1]) / 1024, 1)
    if pid is None:
        try:
            import resource

            r = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            return round(r / (1024 * 1024 if sys.platform == "darwin" else 1024), 1)
        except Exception:  # noqa: BLE001
            return None
    return None


def own_io() -> dict:
    out = {}
    for ln in _read("/proc/self/io").splitlines():
        k, _, v = ln.partition(":")
        if k in ("read_bytes", "write_bytes"):
            out[k] = int(v.strip() or 0)
    return out


def memory() -> dict:
    info = {}
    for ln in _read("/proc/meminfo").splitlines():
        k, _, v = ln.partition(":")
        if k in ("MemTotal", "MemAvailable"):
            info[k] = int(v.split()[0]) // 1024
    if info:
        return {"total_mb": info.get("MemTotal"), "free_mb": info.get("MemAvailable")}
    return {}


def battery() -> dict | None:
    if os.environ.get("TERMUX_VERSION") and shutil.which("termux-battery-status"):
        try:
            out = subprocess.run(["termux-battery-status"], capture_output=True, text=True, timeout=5).stdout
            d = json.loads(out)
            return {"percent": d.get("percentage"), "charging": d.get("status") in ("CHARGING", "FULL"), "temp_c": d.get("temperature")}
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return None
    for base in Path("/sys/class/power_supply").glob("BAT*") if Path("/sys/class/power_supply").exists() else []:
        cap = _read(str(base / "capacity")).strip()
        st = _read(str(base / "status")).strip()
        if cap:
            return {"percent": int(cap), "charging": st in ("Charging", "Full")}
    return None


def temperature() -> float | None:
    temps = []
    for z in Path("/sys/class/thermal").glob("thermal_zone*/temp") if Path("/sys/class/thermal").exists() else []:
        try:
            v = int(_read(str(z)).strip()) / 1000
            if 0 < v < 150:
                temps.append(v)
        except ValueError:
            continue
    return round(max(temps), 1) if temps else None


def snapshot(data_dir: Path | None = None) -> dict:
    d: dict = {
        "os": f"{platform.system()} {platform.release()}",
        "machine": platform.machine(),
        "python": platform.python_version(),
        "termux": bool(os.environ.get("TERMUX_VERSION")),
        "cpus": os.cpu_count(),
        "uptime_bender_s": int(time.time() - _START),
    }
    try:
        d["load"] = [round(x, 2) for x in os.getloadavg()]
    except (OSError, AttributeError):
        pass
    d["memory"] = memory()
    if data_dir:
        try:
            du = shutil.disk_usage(data_dir)
            d["disk"] = {"total_gb": round(du.total / 2**30, 1), "free_gb": round(du.free / 2**30, 1)}
        except OSError:
            pass
    bat = battery()
    if bat:
        d["battery"] = bat
    t = temperature()
    if t is not None:
        d["temp_c"] = t
    d["self"] = {"rss_mb": rss_mb(), **own_io()}
    return d


def describe(s: dict) -> str:
    n = s.get("cpus") or 0
    cores = "ядро" if n % 10 == 1 and n % 100 != 11 else "ядра" if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14) else "ядер"
    parts = [f"Машина: {s.get('os')} ({s.get('machine')}), {n} {cores}, Python {s.get('python')}" + (", Termux" if s.get("termux") else "")]
    if s.get("load"):
        parts.append(f"Нагрузка: {s['load'][0]} / {s['load'][1]} / {s['load'][2]}")
    m = s.get("memory") or {}
    if m:
        parts.append(f"Память: свободно {m.get('free_mb')} из {m.get('total_mb')} МБ")
    dk = s.get("disk")
    if dk:
        parts.append(f"Диск: свободно {dk['free_gb']} из {dk['total_gb']} ГБ")
    if s.get("battery"):
        b = s["battery"]
        parts.append(f"Батарея: {b.get('percent')}%{' (заряжается)' if b.get('charging') else ''}")
    if s.get("temp_c") is not None:
        parts.append(f"Температура: {s['temp_c']} °C")
    me = s.get("self") or {}
    parts.append(f"Я сам: {me.get('rss_mb')} МБ памяти, работаю {s.get('uptime_bender_s', 0) // 60} мин")
    return "\n".join(parts)


def machine_uptime_s() -> int | None:
    t = _read("/proc/uptime").split()
    try:
        return int(float(t[0])) if t else None
    except ValueError:
        return None


def top_processes(n: int = 5) -> list[dict]:
    """Самые прожорливые по памяти процессы (Linux/Termux, по /proc). Только по запросу — это чтение сотен файлов."""
    out = []
    proc = Path("/proc")
    if not proc.is_dir():
        return out
    page_mb = (os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096) / (1024 * 1024)
    for d in proc.iterdir():
        if not d.name.isdigit():
            continue
        try:
            stat = (d / "stat").read_text()
            name = stat[stat.index("(") + 1:stat.rindex(")")]
            rss_pages = int((d / "statm").read_text().split()[1])
        except (OSError, ValueError, IndexError):
            continue
        out.append({"pid": int(d.name), "name": name, "rss_mb": round(rss_pages * page_mb, 1)})
    out.sort(key=lambda x: x["rss_mb"], reverse=True)
    return out[:n]


def network() -> dict:
    """Есть ли сеть хотя бы по признакам ОС (без запросов наружу): маршрут по умолчанию и интерфейсы."""
    route = _read("/proc/net/route")
    if route:
        default = [ln.split()[0] for ln in route.splitlines()[1:] if len(ln.split()) > 2 and ln.split()[1] == "00000000"]
        return {"default_route": bool(default), "interface": default[0] if default else None}
    return {}
