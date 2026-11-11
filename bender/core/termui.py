"""Терминал: цвета, анимация пробуждения, спиннеры и понятный вывод ошибок.

Анимации выключаются сами, если вывод идёт не в
живой терминал (файл, лог, пайп), если задан NO_COLOR или TERM=dumb, и если их
выключили в настройках (BENDER_NO_ANIM).
"""

from __future__ import annotations

import itertools
import os
import sys
import threading
import time
from contextlib import contextmanager

from .errors import HumanError

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
HIDE_CURSOR = "\x1b[?25l"
SHOW_CURSOR = "\x1b[?25h"

AMBER = (255, 184, 64)  # свечение визора
STEEL = (176, 188, 204)  # корпус
RED = (232, 90, 101)
GREEN = (110, 201, 140)


def _enable_windows_vt() -> bool:
    if sys.platform != "win32":
        return True
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:  # noqa: BLE001
        return False


class Term:
    def __init__(self, stream=None, animations: bool = True):
        self.out = stream or sys.stdout
        self.tty = bool(getattr(self.out, "isatty", lambda: False)())
        no_color = "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb"
        self.color = self.tty and not no_color and _enable_windows_vt()
        self.animate = self.color and animations and os.environ.get("BENDER_NO_ANIM") is None
        self._lock = threading.Lock()
        for s in (self.out, sys.stderr):
            try:
                if getattr(s, "isatty", lambda: False)():
                    s.reconfigure(errors="replace")
                else:
                    # вывод в файл или лог: UTF-8, иначе на Windows рамки превратятся в «?»
                    s.reconfigure(encoding="utf-8", errors="replace")
            except (AttributeError, ValueError):
                pass

    # цвет

    def rgb(self, text: str, rgb: tuple[int, int, int], bold: bool = False) -> str:
        if not self.color:
            return text
        r, g, b = rgb
        return f"{BOLD if bold else ''}\x1b[38;2;{r};{g};{b}m{text}{RESET}"

    def amber(self, text: str, bold: bool = False) -> str:
        return self.rgb(text, AMBER, bold)

    def steel(self, text: str, bold: bool = False) -> str:
        return self.rgb(text, STEEL, bold)

    def red(self, text: str, bold: bool = False) -> str:
        return self.rgb(text, RED, bold)

    def green(self, text: str, bold: bool = False) -> str:
        return self.rgb(text, GREEN, bold)

    def dim(self, text: str) -> str:
        return f"{DIM}{text}{RESET}" if self.color else text

    def bold(self, text: str) -> str:
        return f"{BOLD}{text}{RESET}" if self.color else text

    def write(self, text: str = "") -> None:
        with self._lock:
            try:
                self.out.write(text)
                self.out.flush()
            except BrokenPipeError:
                # вывод обрезали (`bender try … | head`) — молчим дальше, а не падаем
                try:
                    os.dup2(os.open(os.devnull, os.O_WRONLY), self.out.fileno())
                except (OSError, ValueError, AttributeError):
                    pass

    def line(self, text: str = "") -> None:
        self.write(text + "\n")

    # анимация пробуждения

    def banner(self, version: str) -> None:
        """Визор просыпается: глаза мигают, по визору бежит свет, проявляется имя."""
        title = "Б Е Н Д Е Р"
        if not self.animate:
            self.line(self.amber("◉ ◉", bold=True) + "  " + self.steel(title, bold=True) + self.dim(f"  v{version}"))
            return
        self.write(HIDE_CURSOR)
        try:
            # глаза открываются
            for eyes in ("─ ─", "▁ ▁", "▄ ▄", "◉ ◉", "─ ─", "◉ ◉"):
                self.write("\r\x1b[2K  " + self.amber(eyes, bold=True))
                time.sleep(0.07)
            # свет бежит по визору и проявляет имя
            width = len(title)
            for k in range(width + 4):
                shown = ""
                for idx, ch in enumerate(title):
                    dist = abs(idx - k)
                    if idx <= k:
                        glow = max(0, 3 - dist)
                        color = (min(255, STEEL[0] + glow * 25), min(255, STEEL[1] + glow * 20), max(64, STEEL[2] - glow * 40))
                        shown += self.rgb(ch, color, bold=True)
                    else:
                        shown += self.dim("·" if ch != " " else " ")
                self.write("\r\x1b[2K  " + self.amber("◉ ◉", bold=True) + "  " + shown)
                time.sleep(0.03)
            self.write("\r\x1b[2K  " + self.amber("◉ ◉", bold=True) + "  " + self.steel(title, bold=True) + self.dim(f"   v{version}") + "\n")
        finally:
            self.write(SHOW_CURSOR)

    @contextmanager
    def step(self, text: str):
        """Шаг со спиннером: ✓ при успехе, ✗ при ошибке."""
        if not self.animate:
            self.write(f"  • {text}… ")
            try:
                yield
            except BaseException:
                self.line("ошибка")
                raise
            self.line("готово")
            return
        stop = threading.Event()
        frames = itertools.cycle(["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"])

        def spin():
            while not stop.is_set():
                self.write(f"\r\x1b[2K  {self.amber(next(frames))} {text}")
                time.sleep(0.08)

        th = threading.Thread(target=spin, daemon=True)
        self.write(HIDE_CURSOR)
        th.start()
        try:
            yield
        except BaseException:
            stop.set()
            th.join()
            self.write(f"\r\x1b[2K  {self.red('✗')} {text}\n" + SHOW_CURSOR)
            raise
        stop.set()
        th.join()
        self.write(f"\r\x1b[2K  {self.green('✓')} {text}\n" + SHOW_CURSOR)

    def error(self, err: HumanError) -> None:
        self.line()
        self.line("  " + self.red("✗ " + err.title, bold=True))
        if err.hint:
            self.line("    " + err.hint)
        if err.detail:
            self.line(self.dim("    Подробности:"))
            for ln in err.detail.strip().splitlines()[-25:]:
                self.line(self.dim("      " + ln))
        self.line()

    def ready(self, url: str, extra: list[str], title: str = "Пульт", stop_hint: bool = True) -> None:
        box = [f"{title}:  {url}", *extra] + (["Остановить: Ctrl+C"] if stop_hint else [])
        width = max(len(x) for x in box) + 4
        self.line()
        self.line("  " + self.steel("╭" + "─" * width + "╮"))
        for i, text in enumerate(box):
            colored = text.replace(url, self.amber(url, bold=True)) if i == 0 else self.dim(text) if text.startswith("Остановить") else text
            self.line("  " + self.steel("│") + "  " + colored + " " * (width - len(text) - 2) + self.steel("│"))
        self.line("  " + self.steel("╰" + "─" * width + "╯"))
        self.line()
