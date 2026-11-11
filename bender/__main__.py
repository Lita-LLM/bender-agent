"""Точка входа: `python -m bender …` или `bender …`.

Подкоманды:
  (без)           запустить Бендера (пульт + сердце + каналы)
  chat            разговор в терминале
  try …           Полигон: потрогать отдельную часть (модель, тул, Предохранитель…)
  import openclaw переселить агента из OpenClaw
  fake-llm        фальшивый сервер моделей для проверок без токенов
"""

from __future__ import annotations

import os
import sys

from . import MIN_PYTHON, __version__


def _watch(watchdog,data,safe,raw):
    # Сам Сторож не загружает pathlib/json/механизм блокировки и не растёт в памяти.
    # Прежняя копия отвергается до записи boot.txt; окончательная блокировка — в serve.
    os.environ.setdefault('BENDER_PROGRAM_DIR',watchdog.PROGRAM_DIR)
    root=data or os.environ.get('BENDER_HOME') or os.path.join(watchdog.PROGRAM_DIR,'data')
    if os.path.exists(os.path.join(os.path.expanduser(root),'local','run.lock')):
        code='import sys; sys.path.insert(0,sys.argv[1]); from bender.core.run_lock import preflight; sys.exit(preflight(sys.argv[2]))'
        args=[sys.executable,'-c',code,watchdog.PROGRAM_DIR,root]
        if os.name=='nt':
            # os.spawnv в Windows не берёт аргументы в кавычки: код с пробелами распался бы на слова.
            import subprocess
            status=subprocess.call(args)
        else:status=os.spawnv(os.P_WAIT,sys.executable,args)
        if status:return status
    return watchdog.main(data,safe,raw)


def build_parser():
    import argparse

    ap = argparse.ArgumentParser(prog="bender", description="Бендер — постоянно живущий ИИ-агент")
    ap.add_argument("--data", help="папка данных (по умолчанию data/ рядом с программой или BENDER_HOME)")
    ap.add_argument("--version", action="version", version=f"Бендер {__version__}")
    ap.add_argument("--no-anim", action="store_true", help="без анимаций в терминале")
    sub = ap.add_subparsers(dest="command")

    run = sub.add_parser("run", help="запустить Бендера (по умолчанию)")
    run.add_argument("--port", type=int, help="порт пульта (по умолчанию 8770)")
    run.add_argument("--listen", action="store_true", help="пускать в пульт из локальной сети (по ссылке с токеном)")
    run.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    run.add_argument("--safe", action="store_true", help="безопасный режим: без модулей, каналов и сердца")
    run.add_argument("--no-watchdog", action="store_true", help="без Сторожа: не перезапускать ядро при падении")
    # Старые службы писали --no-anim после run; SUPPRESS не затирает общий флаг, заданный перед run.
    run.add_argument("--no-anim", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS)

    doctor=sub.add_parser('doctor',help='самопроверка без запуска фоновой жизни')
    doctor.add_argument('--json',action='store_true',help='результат JSON')
    doctor.add_argument('--models',action='store_true',help='маленькие платные/бесплатные запросы настроенных ролей, без памяти; выключенный голос не включается')
    report=sub.add_parser('report',help='предпросмотр и технический отчёт без личных текстов')
    report.add_argument('--output',help='куда сохранить ZIP')
    report.add_argument('--limit',type=int,default=100,help='число последних записей, 1–500')
    report.add_argument('--yes',action='store_true',help='сохранить после вывода состава без вопроса')
    usage=sub.add_parser('usage',help='расход моделей по часам: числа без текстов и названий')
    usage.add_argument('--hours',type=float,default=24,help='за сколько последних часов, по умолчанию 24')
    usage.add_argument('--model',default='',help='только модели, в имени которых есть эта строка (например gpt)')
    chat = sub.add_parser("chat", help="разговор в терминале")
    chat.add_argument("--agent", default="main", help="id или имя агента стаи")
    sub.add_parser('link', help='показать ссылку с токеном для входа в пульт')
    service=sub.add_parser('service',help='автозапуск: install, remove или status (systemd / Termux:Boot)')
    service.add_argument('action',choices=['install','remove','status'])
    service.add_argument('--voice',action='store_true',help='отдельная служба установленного локального голоса')
    service.add_argument('--json',action='store_true',help='результат JSON')
    update = sub.add_parser('update', help='проверить и обновить программу своей ветки git')
    update.add_argument('--check', action='store_true', help='только проверить новые коммиты')
    update.add_argument('--yes', action='store_true', help='установить показанные коммиты без вопроса')
    update.add_argument('--realign', action='store_true', help='после пересборки истории на сервере: привести клон к серверной ветке (прежний коммит сохранится в отдельной ветке)')
    update.add_argument('--clean', action='store_true', help='чистая переустановка программы: правки и лишние файлы в папке программы стираются, данные (ключи, агенты, память) остаются; лечит «локальные правки» из-за перевода строк на Windows')
    update.add_argument('--no-backup', action='store_true', help='один раз обновиться без бекапа (самопроверка и откат остаются)')

    tr = sub.add_parser("try", help="Полигон: потрогать отдельную часть")
    tr.add_argument("what", nargs="?", help="что проверить (без аргумента — список)")
    tr.add_argument("args", nargs=argparse.REMAINDER, help="аргументы")

    imp = sub.add_parser("import", help="переселить агента из другой системы")
    imp.add_argument("source", choices=["openclaw"], help="откуда")
    imp.add_argument("--from", dest="from_path", help="папка установки (по умолчанию ищется сама)")
    imp.add_argument("--agent", help="какого агента переселять, если их несколько (ID в OpenClaw)")
    imp.add_argument("--dry-run", action="store_true", help="только показать план")
    imp.add_argument("--yes", action="store_true", help="не спрашивать подтверждения")
    imp.add_argument("--no-secrets", action="store_true", help="не переносить ключи и токены")
    imp.add_argument("--replace-current", action="store_true", help="явно заменить текущего агента вместо создания новых")
    imp.add_argument('--profile',choices=['full','helper','observer','reader'],help='шаблон новым агентам; по умолчанию главному OpenClaw Полный, остальным Помощник')

    login = sub.add_parser("login", help="войти в подписку (пока — ChatGPT)")
    login.add_argument("service", choices=["chatgpt"], help="куда входить")
    login.add_argument("--browser", action="store_true", help="через браузер на этой машине, а не по коду")

    backup = sub.add_parser("backup", help="снимки данных без шифрования")
    backup.add_argument("action", choices=["now", "list", "verify", "repair", "restore"])
    backup.add_argument("snapshot", nargs="?")
    backup.add_argument("--target", help="новая папка восстановленного слота")
    backup.add_argument("--apply", action="store_true", help="восстановить после предпросмотра")
    backup.add_argument("--full", action="store_true", help="перечитать все блоки")

    fake = sub.add_parser("fake-llm", help="фальшивый сервер моделей для проверок без токенов")
    voice=sub.add_parser('voice',help='необязательный локальный голос отдельным процессом')
    voice.add_argument('action',choices=['install','connect','serve','bench','status','stop'])
    voice.add_argument('target',nargs='?',help='для connect: id/имя агента или all')
    voice.add_argument('--agent',help='агент для установки; без флага — все постоянные без своих аудиоролей')
    voice.add_argument('--voice',choices=['terra','ruslan','igm','sova','pvo','fr','mari','sushkov','irina'],help='русский голос Piper; с --agent — личный голос этого агента, остальные голоса остаются')
    voice.add_argument('--no-service',action='store_true',help='без запуска systemd; затем voice serve')
    voice.add_argument('--plan',action='store_true',help='показать пакет без загрузки и установки')
    fake.add_argument("--port", type=int, default=5199)
    style = sub.add_parser("style", help="манера речи агента: list, show, use ИМЯ, off")
    style.add_argument("action", nargs="?", default="list", choices=["list", "show", "use", "off"])
    style.add_argument("name", nargs="?", help="id или название манеры")
    style.add_argument("--agent", help="id или имя агента; без флага — главный")
    return ap


def main(argv: list[str] | None = None) -> int:
    if sys.version_info < MIN_PYTHON:
        print(f"Бендеру нужен Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} или новее, а запущен {sys.version.split()[0]}.")
        return 2
    raw = list(sys.argv[1:] if argv is None else argv)
    if os.environ.get("BENDER_CORE") != "1":
        # обычный запуск — Сторож над ядром; разбор без argparse, чтобы Сторож оставался крошечным
        from .core import watchdog

        quick = watchdog.quick_args(raw)
        if quick is not None:
            return _watch(watchdog,quick[0],quick[1],raw)
    options=None
    if os.environ.get("BENDER_CORE")=="1":
        from .core import watchdog
        options=watchdog.quick_args(raw,full=True)
    if options is None:args=build_parser().parse_args(raw)
    else:
        from types import SimpleNamespace
        args=SimpleNamespace(**options)
    cmd = args.command or "run"
    if cmd=='service':
        from .core.app import App
        from .core.service import main as service_main
        return service_main(args,app_factory=App)
    if cmd == 'link':
        from .core.paths import default_data_dir
        from .core.web_auth import token
        import json
        import sqlite3
        from pathlib import Path
        data=Path(args.data or default_data_dir()).expanduser()
        try:
            value=token(data)
            db=sqlite3.connect((data/'bender.db').resolve().as_uri()+'?mode=ro',uri=True)
            try:
                row=db.execute("SELECT value FROM meta WHERE key='web:active_port'").fetchone()
                port=json.loads(row[0]) if row else 8770
            finally:db.close()
            print(f'http://127.0.0.1:{port}/?token={value}')
            return 0
        except Exception as exc:
            from .core.errors import HumanError
            if not isinstance(exc,HumanError):raise
            print(exc.title+'. '+exc.hint)
            return 1
    if cmd == "style":
        from .core.app import App
        from .mind.manners_cli import main as style_main
        return style_main(args, app_factory=App)
    if cmd=='voice':
        from .core.app import App
        from .voice.cli import main as voice_main
        return voice_main(args,app_factory=App)
    if cmd == 'update':
        from .updates.cli import main as update_main
        return update_main(args)
    if cmd == 'usage':
        from .diagnostics.usage import main as usage_main
        return usage_main(args)
    if cmd in ("doctor","report"):
        from .diagnostics.cli import main as diagnostics_main
        return diagnostics_main(args)
    if cmd == "fake-llm":
        from .devtools import fake_llm

        return fake_llm.main(args.port)
    if cmd == "backup":
        from .backups.cli import main

        return main(args)
    if cmd == "try":
        from .devtools import polygon

        return polygon.main(args)
    if cmd == "chat":
        from .channels import terminal

        return terminal.main(args)
    if cmd == "login":
        from .channels import terminal

        return terminal.login_main(args)
    if cmd == "import":
        from .importers import cli as import_cli

        return import_cli.main(args)
    if os.environ.get("BENDER_CORE") != "1" and not getattr(args, "no_watchdog", False):
        # обычный запуск: Сторож над ядром (ядро — тот же `bender run` в дочернем процессе)
        from .core import watchdog

        return _watch(watchdog,args.data,bool(getattr(args, "safe", False)),raw)
    from .core import app

    return app.main(args)


if __name__ == "__main__":
    sys.exit(main())
