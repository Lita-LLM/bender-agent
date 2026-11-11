"""Обслуживание из Telegram тем же, что в пульте: бекапы, самопроверка, журнал, нагрузка. Без обращения к модели."""
from __future__ import annotations

from ..core.dates import calendar
from ..core.errors import HumanError


def backup_text(host) -> str:
    backups = host.backups
    cfg = backups.config()
    state = host.store.meta_get('backups.state', {}) or {}
    lines = ['Бекапы ' + ('включены' if cfg['enabled'] else 'выключены') + '.']
    try:
        roots = backups.roots()
    except HumanError as error:
        roots = []
        lines.append(error.title + '. ' + error.hint)
    for root in roots:
        lines.append(f'• {root}: снимков {len(backups.snapshots(root))}')
    if not roots:
        lines.append('Папка бекапов не выбрана: пульт → Бекапы.')
    last = state.get('last_backup')
    lines.append('Последний снимок: ' + (calendar(last) if last else 'ещё не было'))
    check = state.get('verification')
    if check:
        lines.append('Проверка: ' + ('всё цело' if check.get('ok') else f"повреждений: {len(check.get('bad') or [])}; нажми «Починить»") + f" (блоков {check.get('checked')})")
    fix = state.get('repair')
    if fix:
        lines.append(f"Починка {calendar(fix.get('at'))}: восстановлено блоков {fix.get('fixed', 0)}, потеряно {fix.get('lost', 0)}" + (' (сама, при сбое снимка)' if fix.get('auto') else ''))
    if backups.progress and host.store.meta_get('backups.in_progress'):
        lines.append(f"Идёт снимок: файлов {backups.progress.get('files')} из {backups.progress.get('total')}.")
    return '\n'.join(lines)


def backup_result(action: str, result: dict) -> str:
    if action == 'now':
        text = f"Снимок сохранён: файлов {result.get('files')}, новых блоков {result.get('copied')}, копий {result.get('destinations')}."
        if result.get('healed'):
            text += f"\nСам починил бекап по пути: восстановлено блоков {result['healed']['fixed']}, потеряно {result['healed']['lost']}."
        warnings = result.get('warnings') or []
        if warnings:
            text += '\n⚠️ ' + '\n⚠️ '.join(warnings[:5])
        return text
    if action == 'verify':
        return 'Всё цело (проверено блоков: %s).' % result.get('checked') if result.get('ok') else f"Найдены повреждения ({len(result.get('bad') or [])} из {result.get('checked')}). Нажми «Починить»."
    text = f"Починка: восстановлено блоков {result.get('fixed', 0)}, потеряно {result.get('lost', 0)}, описей подтянуто {result.get('manifests', 0)}."
    if result.get('damaged'):
        text += '\nЗатронутые снимки: ' + ', '.join(result['damaged'][:5]) + '. Свежий снимок сохранит то, что ещё есть на диске.'
    if result.get('snapshot'):
        text += '\n' + backup_result('now', result['snapshot'])
    if result.get('snapshot_error'):
        text += '\n⚠️ Новый снимок не вышел: ' + result['snapshot_error']
    return text


def doctor_text(host) -> str:
    from ..diagnostics.doctor import collect
    report = collect(host.dirs.root, app=host)
    marks = {'ok': '✅', 'warn': '⚠️', 'error': '❌'}
    rows = report['rows']
    bad = [row for row in rows if row['level'] != 'ok']
    lines = ['Самопроверка: ' + ('всё в порядке' if report['ok'] and not bad else 'есть что посмотреть') + f' (проверок {len(rows)}).']
    for row in bad[:12]:
        lines.append(f"{marks.get(row['level'], '•')} {row['title']}" + (f" — {row['action']}" if row.get('action') else ''))
    if len(bad) > 12:
        lines.append(f'… и ещё {len(bad) - 12}: пульт → Самопроверка.')
    return '\n'.join(lines)


def journal_text(ctx, arg: str = '') -> str:
    word = arg.strip().lower()
    count = int(word) if word.isdigit() else 12
    kind = None if word.isdigit() or not word else word
    rows = ctx.journal.recent(max(1, min(30, count)), kind=kind)
    if not rows:
        return 'В журнале пока нет записей' + (f' вида «{kind}»' if kind else '') + '.'
    return 'Бортжурнал (новое сверху):\n' + '\n'.join(f"{calendar(r['ts'])} · {r['kind']} · {str(r['summary'])[:160]}" for r in rows)


def load_text(host) -> str:
    agents = host.agents
    watch = getattr(host, 'loop_watch', None)
    lines = ['Нагрузка']
    if watch is not None:
        lag = watch.lag
        lines.append(f'Цикл отвечает за {lag:.1f} с (норма меньше секунды)' + (' — перегружен, плановые дела ждут.' if agents.overloaded() else '.'))
        if watch.recent:
            last = watch.recent[-1]
            lines.append(f"Последнее зависание: {calendar(last['started'])}, {last['seconds']} с, в {last['where']}.")
    else:
        lines.append('Сторож цикла не запущен (запуск не через bender run).')
    running = []
    for record in agents.public():
        ctx = agents.loaded.get(record['id'])
        if ctx is None:
            continue
        active = [sid for sid in list(ctx.mind.turns) if ctx.mind.running(sid)]
        if active:
            running.append(f"{record['name']}: ходов {len(active)}")
    lines.append('Идут ходы: ' + ('; '.join(running) if running else 'нет.'))
    free = getattr(agents.background_limit, '_value', None)
    if free is not None:
        lines.append(f'Безлюдные ходы: занято {agents.background_size - free} из {agents.background_size}.')
    team = getattr(host, 'team', None)
    if team is not None and getattr(team, 'running', None):
        lines.append('Задачи команды идут.')
    loaded = sum(1 for r in agents.public() if r['loaded'])
    lines.append(f'Агентов в памяти: {loaded}.')
    return '\n'.join(lines)
