"""Полигон анализа нагрузки: `bender try load [секунд] [--keep]`.

На временной папке данных запускает две синтетические нагрузки — горячий поток Python и задачу asyncio,
которая крутится без паузы (await asyncio.sleep(0)), — и снимает «кто грузит» тем же кодом, что LoadWatch
при необычной нагрузке: ЦП потоков и их места, готовые задачи цикла, процессы узла. Печатает виновника и
отчёт. Без Telegram, без модели, настоящие данные не трогает.
"""
from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
import time
from pathlib import Path

from ..core.errors import HumanError


def main(rest: list[str], term) -> int:
    keep = '--keep' in rest
    words = [w for w in rest if w != '--keep']
    try:
        seconds = float(words[0]) if words else 5.0
    except ValueError:
        term.error(HumanError('try_load', 'Нужно число секунд', 'Например: bender try load 5'))
        return 2
    seconds = max(2.0, min(seconds, 60.0))
    try:
        return asyncio.run(run(seconds, keep, term))
    except KeyboardInterrupt:
        return 130


def _spin(stop: threading.Event) -> None:
    value = 0
    while not stop.is_set():
        value = (value * 31 + 7) % 1000003  # чистый Python: держит GIL, как зацикленный разбор


async def _busy_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        await asyncio.sleep(0)  # «переподключение без паузы»: цикл отвечает, а процессор горит


async def run(seconds: float, keep: bool, term) -> int:
    from ..core.app import App
    from ..core.loadwatch import numbers_line, report_text

    tmp = Path(tempfile.mkdtemp(prefix='bender-try-load-'))
    app = App(tmp / 'data')
    stop = threading.Event()
    thread = task = None
    try:
        loop = asyncio.get_running_loop()
        app.bus.bind_loop(loop)
        app.settings.set('load', {'enabled': True, 'notify': False, 'manager': ''})
        watch = app.load_watch
        watch.start(loop)
        term.line(term.bold('  Нагрузка: синтетический горячий поток и задача без паузы, ' + f'{seconds:g} с'))
        term.line(term.dim(f'  Временные данные: {tmp} · Telegram и модель не участвуют'))
        thread = threading.Thread(target=_spin, args=(stop,), name='полигон-горячий-поток', daemon=True)
        thread.start()
        task = asyncio.ensure_future(_busy_loop(stop))
        task.set_name('полигон-без-паузы')
        watch.sample()
        await asyncio.sleep(max(1.0, seconds - 3))
        numbers = watch.sample() or {}
        started = time.time()
        snap = await asyncio.to_thread(watch.capture, min(3.0, seconds))
        context = await watch.context()
        anomaly = {'id': time.strftime('%Y%m%d-%H%M%S') + '-bender', 'subject': 'bender', 'title': 'процессор Бендера', 'started': started,
                   'reason': 'полигон: синтетическая нагрузка', 'critical': False, 'numbers': numbers}
        report = watch.report(anomaly, snap, context)
        path = watch.save(report)
        stop.set()
        await task
        thread.join(2)
        term.line('')
        term.line(f"  Числа: {numbers_line(numbers, watch.cores()) or 'нет (первый замер)'}")
        term.line(term.bold(f"  Виновник: {report['culprit']}"))
        for item in snap.get('threads', [])[:4]:
            place = item['places'][0][0] if item.get('places') else ''
            term.line(f"    поток {item['name']}{' (цикл)' if item['main'] else ''}: {item['pct']}% ядра" + (f" — {place}" if place else ''))
        for item in snap.get('tasks', [])[:3]:
            term.line(f"    задача {item['task']}: готова в {round(item['share'] * 100)}% проб")
        if snap.get('top_cpu'):
            term.line('  Процессы узла по ЦП:')
            for row in snap['top_cpu'][:5]:
                term.line(f"    {row['pid']} {row['comm']} — {row['cpu_pct']}% ядра" + (f" · {row['bender']}" if row.get('bender') else ''))
        elif snap.get('node_note'):
            term.line('  Процессы узла: ' + snap['node_note'])
        full = app.dirs.root / path
        term.line('')
        term.line(f"  Отчёт: {full}" + ('' if keep else ' (временный; --keep — оставить папку)'))
        term.line(term.dim('  — начало отчёта —'))
        for line in report_text(report).splitlines()[:30]:
            term.line('  ' + line)
        found = ('полигон-горячий-поток' in report['culprit'] or 'полигон-без-паузы' in report['culprit']
                 or any('полигон-без-паузы' in item['task'] for item in snap.get('tasks', [])))
        term.line('')
        term.line(term.green('  ✓ Синтетическая нагрузка найдена') if found else term.red('  ✗ Синтетическую нагрузку назвать не удалось (машина занята сильнее?)'))
        return 0 if found else 1
    finally:
        stop.set()
        if task is not None and not task.done():
            task.cancel()
        await app.close()
        if not keep:
            shutil.rmtree(tmp, ignore_errors=True)
