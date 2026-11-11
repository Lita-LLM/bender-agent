"""Запуск Бендера: ядро, пульт, фоновая жизнь. Обычно его запускает Сторож (bender run)."""

from __future__ import annotations

import asyncio
import os
import signal
import socket
import subprocess
import sys
import time
import webbrowser

from pathlib import Path

from .. import __version__
from .errors import HumanError
from .paths import default_data_dir
from .termui import Term
from .watchdog import EXIT_CONFIG, load_boot

EXIT_RESTART = 75  # «перезапусти меня» — для Сторожа (обновления, правки себя)


def lan_addresses() -> list[str]:
    out = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            out.append(s.getsockname()[0])
    except OSError:
        pass
    return [a for a in out if not a.startswith("127.")]


def open_browser(url: str) -> None:
    if os.environ.get("TERMUX_VERSION"):
        try:
            subprocess.Popen(["termux-open-url", url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return
        except OSError:
            pass
    try:
        webbrowser.open(url)
    except Exception:  # noqa: BLE001
        pass


def _token(app) -> str:
    from .web_auth import token
    return token(app.dirs.root, create=True)


async def serve(args, term: Term) -> int:
    from .run_lock import RunLock
    with RunLock(args.data or default_data_dir()):
        return await _serve(args,term)


async def _serve(args, term: Term) -> int:
    from ..net.http_server import HttpServer
    from ..web.api import create_web
    from .app import App

    data_dir = args.data or default_data_dir()
    safe = bool(getattr(args, "safe", False) or os.environ.get("BENDER_SAFE") == "1")
    with term.step(f"Открываю папку данных {data_dir}"):
        try:
            app = App(data_dir, safe=safe)
        except OSError as e:
            raise HumanError("data_dir", "Не открывается папка данных", f"Проверь путь и права: {data_dir}. Другую папку можно указать так: bender --data ПАПКА",
                             detail=f"{type(e).__name__}: {e}") from e
    app.watched = os.environ.get("BENDER_WATCHED") == "1"  # type: ignore[attr-defined]
    restarted = os.environ.get("BENDER_RESTARTED") == "1"
    loop = asyncio.get_running_loop()
    app.bus.bind_loop(loop)
    web = app.settings.group("web")
    listen = bool(getattr(args, "listen", False) or web.get("listen"))
    host = "0.0.0.0" if listen else "127.0.0.1"
    port = int(getattr(args, "port", None) or web.get("port") or 8770)
    allowed = {"127.0.0.1", "localhost", "[::1]", "::1"}
    lan = lan_addresses() if listen else []
    allowed.update(lan)
    token = _token(app)
    handler = create_web(app, allowed_hosts=allowed, token=token)
    server = HttpServer(handler)
    chosen = None
    with term.step("Поднимаю пульт"):
        for candidate in range(port, port + 20):
            try:
                await server.start(host, candidate)
                chosen = candidate
                break
            except OSError:
                continue
        if chosen is None:
            raise HumanError("port_busy", f"Порты {port}–{port + 19} заняты", "Закрой программу, которая их заняла, или запусти с --port.")
    app.store.meta_set("web:active_port", chosen)
    app.web_port = chosen
    url = f"http://127.0.0.1:{chosen}/?token={token}"
    extra = [f"Данные: {app.dirs.root}"]
    if chosen != port:
        extra.append(f"Порт {port} был занят, взял {chosen}.")
    for ip in lan:
        extra.append(f"Из сети: http://{ip}:{chosen}/?token={token}")
    if app.safe:
        why = (load_boot(app.dirs.root).get("safe") or {}).get("reason")
        extra.append("БЕЗОПАСНЫЙ РЕЖИМ: без модулей, каналов и сердца" + (f" ({why})." if why else "."))
    if not app.selfdir.born():
        extra.append("Агент ещё не проснулся — открой пульт и познакомься.")
    if restarted:
        term.line(f"  Перезапущен: {url}" + (" · безопасный режим" if app.safe else ""))
    else:
        term.ready(url, extra)
    app.journal.write("system", f"Бендер {__version__} {'перезапущен' if restarted else 'запущен'}", data={"port": chosen, "listen": listen, "safe": app.safe})
    if web.get("open_browser", True) and not getattr(args, "no_browser", False) and not restarted:
        open_browser(url)

    stop = asyncio.Event()
    restart = {"flag": False}
    app.request_restart = lambda: (restart.__setitem__("flag", True), stop.set())
    app.request_shutdown = stop.set

    async def housekeeping():
        while True:
            try:
                await asyncio.to_thread(app.housekeeping)
                await asyncio.to_thread(app.index.sync, None)  # догнать индекс целиком (поиск догоняет по 2 с)
                await app.vectors.fill(max_items=2000)  # векторы для того, что появилось без них (новая модель, перенос)
            except Exception:  # noqa: BLE001 — уборка не должна ронять ядро
                pass
            await asyncio.sleep(86400)

    keeper = asyncio.ensure_future(housekeeping())
    from .stall import LoopWatch

    def stalled(record):
        app.journal.write("system", f"Бендер не отвечал {record['seconds']} с — цикл стоял в {record['where']}. Стек: local/stalls.txt",
                          data={"seconds": record["seconds"], "where": record["where"], "stack": record["first"][-12:]})

    app.loop_watch = LoopWatch(loop, stalled, app.dirs.local / "stalls.txt")
    hang = app.loop_watch.previous_hang()
    if hang:
        from .stall import hang_place
        where = hang_place(hang)
        app.journal.write("alarm", f"Бендер завис и перезапустился сам: цикл стоял в {where}. Стеки: local/hang-last.txt", data={"where": where, "stack": hang[-3000:]})
        # каналы ещё не подняты: сообщить хозяину чуть позже
        loop.call_later(20, app.bus.publish, {"type": "notify", "kind": "alarm", "title": "Бендер завис и перезапустился", "text": f"Цикл стоял в {where}. Стеки всех потоков — в local/hang-last.txt; покажи их разработчику."})
    app.loop_watch.start()
    if not app.safe:
        app.heart.start()  # задачи по времени; в безопасном режиме сердце молчит
        app.telegram.start()  # каналы — тоже только в обычном режиме
        app.mail.start()
        if app.settings.get('matrix','enabled',False):app.matrix.start()
        app._speakers_ready=True
        if app.settings.get('speakers','enabled',False):app.speakers.start()
        app.mind.recover()
        app.agents.start()
        app.agents.start_userbots()
        app.hooks.start()     # события вебхуков: прерванные — interrupted, принятые — снова в очередь
        app.team.recover()    # задачи субагентов, прерванные перезапуском
    if os.environ.get("BENDER_TRIAL"):
        from ..updates.manager import report_healthy

        await report_healthy(app, server, chosen)
    if not app.safe:
        from ..updates.news import announce

        app.update_news = asyncio.ensure_future(announce(app))  # после самопроверки: откатившийся слот новостью не станет
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    try:
        await stop.wait()
    finally:
        keeper.cancel()
        app.loop_watch.stop()
        app.journal.write("system", "Бендер остановлен")
        await server.stop()
        await app.close()
    return EXIT_RESTART if restart["flag"] else 0


EXIT_CRASH = 70


def write_crash(data_dir, exc: BaseException) -> Path | None:
    """Последнее падение ядра — текстом, который можно целиком вставить в ИИ-чат."""
    import platform
    import traceback

    try:
        p = Path(data_dir).expanduser() / "local" / "last_crash.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            f"Бендер {__version__} упал {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Python {sys.version.split()[0]} · {platform.platform()}\n\n"
            + "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
            encoding="utf-8",
        )
        return p
    except OSError:
        return None


def run(args) -> int:
    restarted = os.environ.get("BENDER_RESTARTED") == "1"
    term = Term(animations=not getattr(args, "no_anim", False) and not restarted)
    if not restarted:
        term.banner(__version__)
    try:
        code = asyncio.run(serve(args, term))
    except HumanError as e:
        term.error(e)
        return EXIT_CONFIG  # Сторож не будет перезапускать: ошибку настройки перезапуск не лечит
    except KeyboardInterrupt:
        code = 0
    except Exception as e:  # noqa: BLE001 — падение ядра: записать и отдать Сторожу
        p = write_crash(args.data or default_data_dir(), e)
        term.error(HumanError("crash", "Ядро упало", f"Подробности — в {p}" if p else "", detail=f"{type(e).__name__}: {e}"))
        return EXIT_CRASH
    if code != EXIT_RESTART:
        term.line(term.dim("  Бендер остановлен. Всё сохранено."))
    return code

