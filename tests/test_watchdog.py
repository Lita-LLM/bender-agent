"""Сторож: перезапуск, «перезапусти меня», безопасный режим при петле падений, сдача, остановка."""

import json
import os
import signal
import subprocess
import sys
import threading
import time
import unittest
from pathlib import Path

from bender.core.watchdog import EXIT_RESTART, Watchdog, load_boot, save_boot
from bender.core.errors import HumanError
from tests.helpers import TempDirCase

# Подставное ядро: берёт следующий код выхода из плана, записывает, с какими флагами его запустили.
CHILD = r"""
import json, os, sys, time
from pathlib import Path
d = Path(sys.argv[1])
plan = json.loads((d / "plan.json").read_text())
code = plan.pop(0) if plan else 0
(d / "plan.json").write_text(json.dumps(plan))
with open(d / "runs.log", "a") as f:
    f.write(json.dumps({"safe": os.environ.get("BENDER_SAFE"), "restarted": os.environ.get("BENDER_RESTARTED"),
                        "core": os.environ.get("BENDER_CORE")}) + "\n")
if code == "clear-safe":
    p = d / "local" / "boot.txt"
    p.write_text("".join(ln for ln in p.read_text().splitlines(True) if not ln.startswith("safe.")))
    sys.exit(75)
if code == "sleep":
    time.sleep(30)
sys.exit(code)
"""


class WatchdogTest(TempDirCase):
    def setUp(self):
        super().setUp()
        (self.tmp / "child.py").write_text(CHILD)
        self.said = []

    def make(self, plan, **kw):
        (self.tmp / "plan.json").write_text(json.dumps(plan))
        return Watchdog(self.tmp, [sys.executable, str(self.tmp / "child.py"), str(self.tmp)], out=self.said.append,
                        sleep=lambda s: None, backoff=(0, 0, 0), **kw)

    def runs(self):
        return [json.loads(x) for x in (self.tmp / "runs.log").read_text().splitlines()]

    def test_clean_exit(self):
        self.assertEqual(self.make([0]).run(), 0)
        self.assertEqual(len(self.runs()), 1)
        self.assertEqual(self.runs()[0]["core"], "1")
        self.assertEqual(load_boot(self.tmp)["starts"], 1)

    def test_restart_request_is_not_a_crash(self):
        self.assertEqual(self.make([EXIT_RESTART, EXIT_RESTART, 0]).run(), 0)
        runs = self.runs()
        self.assertEqual(len(runs), 3)
        self.assertEqual([r["restarted"] for r in runs], [None, "1", "1"])
        self.assertFalse(load_boot(self.tmp).get("crashes"))

    def test_crash_loop_turns_on_safe_mode(self):
        self.assertEqual(self.make([1, 1, 1, 0]).run(), 0)
        runs = self.runs()
        self.assertEqual([r["safe"] for r in runs], [None, None, None, "1"])
        boot = load_boot(self.tmp)
        self.assertIn("упало 3 раза", boot["safe"]["reason"])
        self.assertEqual(boot["last_crash"]["code"], 1)
        self.assertTrue(any("безопасный режим" in s for s in self.said))

    def test_gives_up_when_safe_mode_crashes_too(self):
        self.assertEqual(self.make([1, 1, 1, 3, 3, 3, 0]).run(), 1)
        self.assertEqual(len(self.runs()), 6)
        self.assertIn("gave_up", load_boot(self.tmp))
        self.assertTrue(any("сдаётся" in s for s in self.said))

    def test_owner_returns_to_normal_mode(self):
        save_boot(self.tmp, {"safe": {"reason": "тест"}})
        self.assertEqual(self.make(["clear-safe", 0]).run(), 0)
        self.assertEqual([r["safe"] for r in self.runs()], ["1", None])

    def test_old_crashes_do_not_count(self):
        save_boot(self.tmp, {"crashes": [time.time() - 3600, time.time() - 3000]})
        self.assertEqual(self.make([1, 0]).run(), 0)
        self.assertFalse(load_boot(self.tmp).get("safe"))

    def test_state_file_roundtrip(self):
        data = {"starts": 3, "last_start": 1.5, "crashes": [1.25, 2.5], "safe": {"since": 7.0, "reason": "падало\tтри\nраза", "code": -9},
                "last_crash": {"at": 1.0, "code": 70, "lived": 0.4}, "gave_up": None}
        self.assertTrue(save_boot(self.tmp, data))
        back = load_boot(self.tmp)
        data.pop("gave_up")
        self.assertEqual(back, data)

    def test_quick_args(self):
        from bender.core.watchdog import quick_args
        self.assertEqual(quick_args([]), (None, False))
        self.assertEqual(quick_args(["--data", "/x", "--no-anim", "run", "--port", "9", "--no-browser", "--safe"]), ("/x", True))
        self.assertEqual(quick_args(["--data=/y", "run", "--listen"]), ("/y", False))
        for argv in (["chat"], ["--help"], ["run", "--no-watchdog"], ["run", "--data", "/x"], ["--port", "9"], ["run", "--port", "abc"], ["--version"]):
            self.assertIsNone(quick_args(argv), argv)

    def test_config_error_is_not_restarted(self):
        self.assertEqual(self.make([78, 0]).run(), 1)
        self.assertEqual(len(self.runs()), 1)

    def test_counts_crashes_even_if_state_cannot_be_saved(self):
        (self.tmp / "local").write_text("не папка")  # boot.txt не записать
        self.assertEqual(self.make([1, 1, 1, 1, 1, 1, 0]).run(), 1)
        self.assertEqual([r["safe"] for r in self.runs()], [None, None, None, "1", "1", "1"])

    def test_stop_signal_stops_without_restart(self):
        w = self.make(["sleep", 0])
        threading.Timer(0.5, lambda: w._on_signal(signal.SIGTERM, None)).start()
        t = time.time()
        self.assertEqual(w.run(), 0)
        self.assertLess(time.time() - t, 10)
        self.assertEqual(len(self.runs()), 1)


def _free_port() -> int:
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


_API_DATA = {}


def _api(port, path, body=None, timeout=1.0):
    import urllib.request

    from bender.core.web_auth import headers
    try:auth=headers(_API_DATA[port]) if port in _API_DATA else {}
    except HumanError:raise OSError('пульт ещё не создал токен') from None
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode() if body is not None else None,
                                 headers={**auth, **({"X-Bender": "1", "Content-Type": "application/json"} if body is not None else {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=timeout) as r:
        return json.loads(r.read())


def _wait_state(port, cond=lambda st: True, tries=100):
    for _ in range(tries):
        try:
            st = _api(port, "/api/state")
            if cond(st):
                return st
        except (OSError, HumanError):
            pass
        time.sleep(0.1)
    return None


@unittest.skipUnless(sys.platform.startswith("linux"), "сигналы и /proc — Linux")
class RealRunTest(TempDirCase):
    def start(self, *extra):
        env = dict(os.environ, BENDER_NO_ANIM="1")
        for k in ("BENDER_CORE", "BENDER_WATCHED", "BENDER_SAFE", "BENDER_RESTARTED"):
            env.pop(k, None)
        self.port = _free_port()
        _API_DATA[self.port]=self.tmp / "data"
        root = Path(__file__).resolve().parents[1]
        return subprocess.Popen([sys.executable, "-m", "bender", "--data", str(self.tmp / "data"), "--no-anim", "run", "--port", str(self.port), "--no-browser", *extra],
                                cwd=root, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)

    def stop(self, p):
        p.send_signal(signal.SIGTERM)
        out, _ = p.communicate(timeout=15)
        self.assertEqual(p.returncode, 0, out.decode("utf-8", "replace")[-2000:])

    def test_bender_run_under_watchdog(self):
        """Настоящий `bender run`: Сторож поднимает ядро, пульт отвечает, SIGTERM гасит обоих."""
        p = self.start()
        try:
            state = _wait_state(self.port)
            self.assertIsNotNone(state, "пульт не ответил")
            self.assertTrue(state["boot"]["watched"])
            self.assertEqual(state["boot"]["starts"], 1)
            self.assertFalse(state["safe"])
        finally:
            self.stop(p)

    def test_owner_leaves_safe_mode_from_pult(self):
        p = self.start("--safe")
        try:
            st = _wait_state(self.port)
            self.assertTrue(st["safe"])
            self.assertEqual(st["boot"]["safe"]["reason"], "запущен с --safe")
            self.assertTrue(_api(self.port, "/api/restart", {"mode": "normal"})["ok"])
            st = _wait_state(self.port, lambda s: s["boot"]["starts"] == 2)
            self.assertIsNotNone(st, "ядро не перезапустилось")
            self.assertFalse(st["safe"])
            self.assertIsNone(st["boot"]["safe"])
        finally:
            self.stop(p)


if __name__ == "__main__":
    unittest.main()
