"""Сторож: маленький родительский процесс над ядром.

- запускает ядро (`python -m bender run` с BENDER_CORE=1) и ждёт его;
- ядро вышло с кодом 0 — всё, выходим; с кодом 75 — «перезапусти меня» (обновление, правка себя,
  смена режима из пульта) — перезапускаем сразу; с кодом 78 — ошибка настройки (порт занят, папка
  данных недоступна), ядро уже объяснило её хозяину — перезапуск не поможет, выходим;
- ядро упало — перезапуск с паузами 1, 2, 5, 10, 30 с; 3 падения за 10 минут — безопасный режим
  (без модулей, каналов и сердца; причина видна в пульте); падает и в безопасном — сдаёмся с объяснением;
- Ctrl+C, SIGTERM, SIGHUP — передаём ядру и выходим, не перезапуская.

Состояние — в local/boot.txt (строки «ключ<TAB>значение»): счётчик стартов, недавние падения,
безопасный режим и его причина. Ядро читает его, чтобы показать причину, и пишет, когда хозяин
просит обычный режим. Если файл не пишется, Сторож всё равно считает падения в памяти.

Лёгкость — требование (цель ≤ 10 МБ): никаких argparse, json, pathlib, re, subprocess и signal
(модуль signal тянет enum). Только os, sys, time и _signal; ядро запускается через posix_spawn.
На Windows — subprocess, загружаемый там же.
"""

from __future__ import annotations

import os
import sys
import time

try:
    import _signal as _sig
except ImportError:  # pragma: no cover
    import signal as _sig  # type: ignore[no-redef]

EXIT_RESTART = 75
EXIT_CONFIG = 78  # ядро не может стартовать из-за настройки — перезапуск не поможет
EXIT_USAGE = 2    # argparse не понял аргументы — перезапуск тем более не поможет
BACKOFF = (1, 2, 5, 10, 30)
WINDOW = 600     # окно подсчёта падений, секунд
LIMIT = 3        # столько падений в окне — безопасный режим
STABLE = 300     # прожило столько — счёт пауз начинается заново
PROGRAM_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))


# --- состояние: local/boot.txt ------------------------------------------------------------
# {"starts": 3, "last_start": 1.5, "crashes": [1.5, 2.5], "safe": {"since": …, "reason": …} | None,
#  "last_crash": {"at", "code", "lived"}, "gave_up": …} ⇄ строки «safe.reason<TAB>текст».


def boot_path(data_dir) -> str:
    return os.path.join(os.fspath(data_dir), "local", "boot.txt")


def _value(raw: str, key: str):
    if key == "crashes":
        out = []
        for x in raw.split(","):
            try:
                out.append(float(x))
            except ValueError:
                pass
        return out
    for cast in (int, float):
        try:
            return cast(raw)
        except ValueError:
            pass
    return raw.replace("\\n", "\n").replace("\\t", "\t")


def load_boot(data_dir) -> dict:
    out: dict = {}
    try:
        with open(boot_path(data_dir), encoding="utf-8") as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return out
    except OSError:
        if not os.path.isfile(boot_path(data_dir)):return out
        return {'_error':'Не читается local/boot.txt. Проверь права и восстанови целую копию; файл не перезаписан.'}
    except UnicodeDecodeError:
        return {'_error':'Не читается local/boot.txt. Проверь права и восстанови целую копию; файл не перезаписан.'}
    invalid = not bool(lines)
    for ln in lines:
        key, tab, raw = ln.partition("\t")
        if not tab or not key:
            invalid = True
            continue
        head, dot, sub = key.partition(".")
        if dot:
            if not isinstance(out.get(head), dict):
                out[head] = {}
            out[head][sub] = _value(raw, sub)
        else:
            out[key] = _value(raw, key)
    for key in ('starts','rollback_count','last_start'):
        value = out.get(key,0)
        if not isinstance(value,(int,float)) or not 0 <= value < 1e18:
            invalid = True
    for key in ('safe','last_crash','slots','rollback'):
        if key in out and not isinstance(out[key],dict):invalid = True
    if 'crashes' in out and (not isinstance(out['crashes'],list) or any(not 0 <= item < 1e18 for item in out['crashes'])):
        invalid = True
    slots=out.get('slots')
    if isinstance(slots,dict):
        for key in ('current','previous','pending','nonce'):
            if key in slots and not isinstance(slots[key],str):invalid=True
        if 'timeout' in slots and (not isinstance(slots['timeout'],(int,float)) or not 0 < slots['timeout'] <= 300):invalid=True
    safe=out.get('safe')
    if isinstance(safe,dict) and 'reason' in safe and not isinstance(safe['reason'],str):invalid=True
    if invalid:out['_error']='Повреждён local/boot.txt. Выполни bender doctor и восстанови целую копию; файл не перезаписан.'
    return out


def _raw(v) -> str:
    if isinstance(v, (list, tuple)):
        return ",".join(repr(float(x)) for x in v)
    return str(v).replace("\t", "\\t").replace("\n", "\\n")


def save_boot(data_dir, data: dict) -> bool:
    """Записать состояние. Не вышло (папка данных сломана) — Сторож всё равно работает дальше."""
    lines = []
    for k, v in data.items():
        if v is None:
            continue
        if isinstance(v, dict):
            lines += [f"{k}.{sk}\t{_raw(sv)}" for sk, sv in v.items() if sv is not None]
        else:
            lines.append(f"{k}\t{_raw(v)}")
    p = boot_path(data_dir)
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p + ".tmp", "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        os.replace(p + ".tmp", p)
        return True
    except OSError:
        return False


# --- процесс ядра --------------------------------------------------------------------------


class _Child:
    """Дочерний процесс без subprocess: posix_spawn + waitpid. На Windows — subprocess."""

    def __init__(self, cmd: list[str], env: dict):
        self.proc = None
        self.code: int | None = None
        if os.name == "nt" or not hasattr(os, "posix_spawn"):
            import subprocess

            self.proc = subprocess.Popen(cmd, env=env)
            self.pid = self.proc.pid
        else:
            self.pid = os.posix_spawn(cmd[0], cmd, env)

    def wait(self) -> int:
        if self.proc is not None:
            self.code = self.proc.wait()
            return self.code
        try:
            _, status = os.waitpid(self.pid, 0)
        except ChildProcessError:
            status = 0
        self.code = os.waitstatus_to_exitcode(status) if hasattr(os, "waitstatus_to_exitcode") else (status >> 8)
        return self.code

    def signal(self, signum: int) -> None:
        if self.code is not None:
            return
        try:
            if self.proc is not None:
                self.proc.send_signal(signum)
            else:
                os.kill(self.pid, signum)
        except (OSError, ValueError):  # на Windows Popen.send_signal понимает не все сигналы; Ctrl+C ядро получит само
            pass


class Watchdog:
    def __init__(self, data_dir, cmd: list[str], *, env: dict | None = None, out=None, sleep=time.sleep,
                 backoff=BACKOFF, window: float = WINDOW, limit: int = LIMIT, slots=False):
        self.data_dir = os.fspath(data_dir)
        self.cmd = cmd
        self.env = dict(os.environ if env is None else env)
        self.out = out or (lambda s: (sys.stderr.write(s + "\n"), sys.stderr.flush()))
        self.sleep = sleep
        self.backoff = backoff
        self.window = window
        self.limit = limit
        self.child: _Child | None = None
        self.stopping = False
        self.state: dict = {}
        self.slots = slots
        self.trial_failed = False
        self.trial_nonce = None

    def _on_signal(self, signum, frame) -> None:
        self.stopping = True
        if self.child is not None:
            hup = getattr(_sig, "SIGHUP", None)
            self.child.signal(_sig.SIGTERM if signum == hup else signum)

    def install_signals(self) -> None:
        for name in ("SIGINT", "SIGTERM", "SIGHUP"):
            sig = getattr(_sig, name, None)
            if sig is not None:
                try:
                    _sig.signal(sig, self._on_signal)
                except (ValueError, OSError):
                    pass

    def _nap(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while not self.stopping and time.monotonic() < end:
            self.sleep(min(0.2, max(0.0, end - time.monotonic())))

    def _sync(self) -> dict:
        """Состояние: из файла, если он читается (ядро могло его поменять), иначе — то, что в памяти."""
        if os.path.isfile(boot_path(self.data_dir)):
            self.state = load_boot(self.data_dir)
        return self.state

    def _slot_path(self, name):
        if not isinstance(name, str) or not name.startswith("v-") or not all(c.isalnum() or c in "-_" for c in name):
            return None
        path = os.path.join(self.data_dir, "versions", name)
        if os.path.islink(os.path.dirname(path)) or os.path.islink(path) or not os.path.isdir(path):
            return None
        return os.path.realpath(path)

    def _rollback(self, reason):
        boot = self._sync()
        slots = boot.get("slots") or {}
        previous = slots.get("previous")
        if not self._slot_path(previous):
            return False
        boot["rollback"] = {"at": time.time(), "from": slots.get("current"), "to": previous, "reason": reason}
        boot["rollback_count"] = int(boot.get("rollback_count") or 0) + 1
        boot["slots"] = {"current": previous, "previous": slots.get("current")}
        boot["crashes"] = []
        self.state = boot
        save_boot(self.data_dir, boot)
        self.out("  ! Новая версия не прошла самопроверку — откатываю: " + reason)
        return True

    def _health_deadline(self, signum=None, frame=None):
        """Один таймер на испытуемый старт; в обычном простое его нет."""
        path = os.path.join(self.data_dir, "local", "health-" + self.trial_nonce + ".txt")
        try:
            with open(path, encoding="utf-8") as f:
                healthy = f.read(200) == self.trial_nonce
        except OSError:
            healthy = False
        if healthy:
            boot = self._sync()
            slots = boot.get("slots") or {}
            if slots.get("nonce") == self.trial_nonce:
                slots.pop("pending", None)
                boot["health"] = {"at": time.time(), "slot": slots.get("current")}
                save_boot(self.data_dir, boot)
            try:
                os.unlink(path)
            except OSError:
                pass
            # Следующее обновление может поставить новый pending до выхода этого ядра.
            # Здоровый прежний процесс больше не находится на испытании.
            self.trial_nonce = None
        else:
            self.trial_failed = True
            if self.child:
                self.child.signal(getattr(_sig, "SIGKILL", _sig.SIGTERM))

    def _wait_trial(self, seconds):
        if hasattr(_sig, "SIGALRM"):
            previous = _sig.signal(_sig.SIGALRM, self._health_deadline)
            _sig.alarm(seconds)
            try:
                return self.child.wait()
            finally:
                _sig.alarm(0)
                _sig.signal(_sig.SIGALRM, previous)
        # На Windows WaitForSingleObject блокируется до события/срока, без опроса.
        if self.child.proc is not None:
            try:
                return self.child.proc.wait(timeout=seconds)
            except TimeoutError:
                self._health_deadline()
            except Exception as exc:
                if type(exc).__name__ != "TimeoutExpired":
                    raise
                self._health_deadline()
        return self.child.wait()

    def run(self) -> int:
        self.state = load_boot(self.data_dir)
        restarts = 0
        streak = 0  # падений подряд — для паузы
        while True:
            boot = self._sync()
            if boot.get('_error'):
                self.out('  ✗ '+boot['_error'])
                return EXIT_CONFIG
            safe = bool(boot.get("safe"))
            env = dict(self.env, BENDER_CORE="1", BENDER_WATCHED="1", BENDER_TRUST_ROOT=PROGRAM_DIR)
            env.pop("BENDER_SAFE", None)
            env.pop("BENDER_TRIAL", None)
            if safe:
                env["BENDER_SAFE"] = "1"
            if restarts:
                env["BENDER_RESTARTED"] = "1"
            command = self.cmd
            slots = boot.get("slots") or {} if self.slots else {}
            self.trial_failed = False
            self.trial_nonce = slots.get("nonce") if slots.get("pending") else None
            if self.slots and slots.get("current"):
                path = self._slot_path(slots["current"])
                if path is None:
                    if self._rollback("слот потерян или недопустим"):
                        continue
                    self.out("  ✗ Слот потерян, прежней версии нет.")
                    return 1
                script = "import os,sys,runpy; p=sys.argv[1]; os.chdir(p); sys.path.insert(0,p); sys.argv=['bender']+sys.argv[2:]; runpy.run_module('bender',run_name='__main__')"
                command = [sys.executable, "-c", script, path, *self.cmd[3:]]
                env["PYTHONPATH"] = path
                if self.trial_nonce:
                    env["BENDER_TRIAL"] = self.trial_nonce
            started = time.time()
            boot["starts"] = int(boot.get("starts") or 0) + 1
            boot["last_start"] = started
            save_boot(self.data_dir, boot)
            try:
                self.child = _Child(command, env)
            except OSError as e:
                self.out(f"  ✗ Сторож не смог запустить ядро: {e}")
                return 1
            code = self._wait_trial(max(1, min(50, int(slots.get("timeout") or 30)))) if self.trial_nonce else self.child.wait()
            self.child = None
            if self.stopping:
                return 0
            if self.trial_nonce and (self.trial_failed or (self._sync().get("slots") or {}).get("pending")):
                reason = "не получен ответ о здоровье" if self.trial_failed else f"испытательный запуск завершился, код {code}"
                if self._rollback(reason):
                    restarts += 1
                    streak = 0
                    continue
            if code == 0:
                return 0
            if code in (EXIT_CONFIG, EXIT_USAGE):
                return 1
            if code == EXIT_RESTART:
                restarts += 1
                streak = 0
                continue
            # падение
            now = time.time()
            lived = now - started
            streak = 1 if lived >= STABLE else streak + 1
            boot = self._sync()
            crashes = [t for t in boot.get("crashes") or [] if isinstance(t, (int, float)) and now - t < self.window] + [now]
            boot["crashes"] = crashes
            boot["last_crash"] = {"at": now, "code": code, "lived": round(lived, 1)}
            if len(crashes) >= self.limit:
                if safe:
                    boot["gave_up"] = now
                    save_boot(self.data_dir, boot)
                    self.out(f"  ✗ Ядро падает и в безопасном режиме ({len(crashes)} раза за {int(self.window // 60)} мин). Сторож сдаётся.")
                    self.out(f"    Что случилось — в {os.path.join(self.data_dir, 'local', 'last_crash.txt')}. Это можно целиком вставить в любой ИИ-чат.")
                    return 1
                boot["safe"] = {"since": now, "reason": f"ядро упало {len(crashes)} раза за {int(self.window // 60)} мин", "code": code}
                boot["crashes"] = []
                streak = 0
                self.out("  ! Ядро падает раз за разом — включаю безопасный режим: без модулей, каналов и сердца.")
            save_boot(self.data_dir, boot)
            delay = self.backoff[min(max(streak, 1), len(self.backoff)) - 1]
            self.out(f"  ! Ядро упало (код {code}, прожило {lived:.0f} с). Перезапуск через {delay} с…")
            self._nap(delay)
            if self.stopping:
                return 0
            restarts += 1


def default_data_dir() -> str:
    env = os.environ.get("BENDER_HOME")
    return os.path.expanduser(env) if env else os.path.join(PROGRAM_DIR, "data")


def main(data: str | None, safe: bool, argv: list[str]) -> int:
    data_dir = os.path.realpath(os.path.expanduser(data or default_data_dir()))
    env = dict(os.environ)
    env["PYTHONPATH"] = PROGRAM_DIR + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    env.setdefault("PYTHONUNBUFFERED", "1")
    boot = load_boot(data_dir)
    if safe:
        boot["safe"] = {"since": time.time(), "reason": "запущен с --safe"}
        save_boot(data_dir, boot)
    elif (boot.get("safe") or {}).get("reason") == "запущен с --safe":
        boot["safe"] = None
        save_boot(data_dir, boot)
    # безопасным режимом управляет Сторож (через boot.txt и BENDER_SAFE), иначе из него не выйти из пульта
    core_args = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--data":
            i += 2
            continue
        if arg != "--safe" and not arg.startswith("--data="):
            core_args.append(arg)
        i += 1
    w = Watchdog(data_dir, [sys.executable, "-m", "bender", "--data", data_dir, *core_args], env=env, slots=True)
    w.install_signals()
    return w.run()


def quick_args(argv: list[str], *, full=False) -> tuple[str | None, bool] | dict | None:
    """Разбор `bender [--data П] [--no-anim] [run [--port N] [--listen] [--no-browser] [--safe]]` без argparse —
    для лёгкого Сторожа. None — это не обычный запуск (другая команда, справка, --no-watchdog, опечатка):
    пусть разбирает argparse и, если надо, объясняет ошибку. full возвращает все флаги
    для ядра: ему тоже не нужно загружать argparse ради уже проверенного запуска."""
    data, safe, seen_run = None, False, False
    options = {"command":None,"no_anim":False} if full else None
    i = 0
    while i < len(argv):
        a = argv[i]
        if not seen_run:
            if a == "--data" and i + 1 < len(argv):
                if full and argv[i+1].startswith("-"):return None
                data = argv[i + 1]
                i += 2
                continue
            if a.startswith("--data="):
                data = a.split("=", 1)[1]
            elif a == "--no-anim":
                if full:options["no_anim"]=True
            elif a == "run":
                seen_run = True
                if full:options.update(command="run",port=None,listen=False,no_browser=False,no_watchdog=False)
            else:
                return None
        else:
            if a == "--port" and i + 1 < len(argv) and argv[i + 1].isdigit():
                if full:options["port"]=argv[i+1]
                i += 2
                continue
            if a.startswith("--port=") and a[7:].isdigit():
                if full:options["port"]=a[7:]
            elif a == "--safe":
                safe = True
            elif a in ("--listen", "--no-browser"):
                if full:options[a[2:].replace("-","_")]=True
            elif a == "--no-anim":
                if full:options["no_anim"]=True
            else:
                return None
        i += 1
    if full:
        options["data"]=data
        if seen_run:
            options["safe"]=safe
            try:options["port"]=int(options["port"]) if options["port"] is not None else None
            except ValueError:return None
        return options
    return data, safe
