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


def cpu_lines(host, top: list | None = None, private: bool = True) -> list[str]:
    """Числа анализа нагрузки: ЦП Бендера и узла, память, кто берёт процессор, последнее необычное.
    private=False (группа) — без виновника: в нём имена процессов машины хозяина."""
    from ..core.loadwatch import numbers_line
    watch = host.load_watch
    now = watch.brief()
    if not now['enabled']:
        return ['Анализ нагрузки выключен (Настройки → Нагрузка).']
    lines = []
    if now.get('own') is None:
        lines.append('Замеров ЦП ещё нет: они идут раз в ' + str(watch.cfg.get('sample_seconds') or 10) + ' с после запуска через bender run.')
    else:
        own = f"ЦП Бендера: {now['own']:.0f}% ядра"
        if now.get('own_1m') is not None:
            own += f" (за минуту {now['own_1m']:.0f}%" + (f", за 5 мин {now['own_5m']:.0f}%" if now.get('own_5m') is not None else '') + ')'
        lines.append(own + '.')
        rest = numbers_line({k: now.get(k) for k in ('sys', 'iowait', 'load1', 'mem', 'swap', 'temp')}, now.get('cores'))
        if rest:
            lines.append('Узел: ' + rest + '.')
        elif now.get('node'):
            from ..body.load import UNAVAILABLE, platform_note
            lines.append('Узел: ' + UNAVAILABLE + (' — ' + platform_note() if platform_note() else '') + '.')
        if now.get('throttled'):
            lines.append('Raspberry Pi: ' + ', '.join(now['throttled']) + '.')
    if top:
        lines.append('Процессор берут: ' + '; '.join(f"{row['comm']} ({row['pid']}, {row['cpu_pct']:.0f}%" + (f", {row['bender']}" if row.get('bender') else '') + ')' for row in top[:3]) + '.')
    last = now.get('last')
    if last:
        state = f"спала через {max(1, round((last['ended'] - last['at']) / 60))} мин" if last.get('ended') else 'идёт сейчас'
        lines.append(f"Последнее необычное: {calendar(last['at'])} — {last['title']}: {last['reason']}" + (f". Виновник: {last['culprit']}" if last.get('culprit') and private else '')
                     + f" ({state})." + (' Отчёт — /load report.' if last.get('report') else ''))
    else:
        lines.append('Необычной нагрузки с запуска не было.')
    manager = watch.cfg.get('manager') or ''
    if manager:
        name = next((a['name'] for a in host.agents.public() if a['id'] == manager), manager)
        when = {'off': 'не зовётся', 'bender': 'зовётся при нагрузке Бендера', 'node': 'зовётся при нагрузке узла'}.get(watch.cfg.get('manager_on'), 'зовётся при любой необычной')
        lines.append(f'Заведующий узлом: {name}, {when} · /load manager.')
    else:
        lines.append('Заведующий узлом не выбран · /load manager.')
    return lines


def load_text(host, top: list | None = None, private: bool = True) -> str:
    agents = host.agents
    watch = getattr(host, 'loop_watch', None)
    lines = ['Нагрузка']
    try:
        lines += cpu_lines(host, top, private)
    except Exception:  # noqa: BLE001 — числа нагрузки не должны ломать остальной ответ
        lines.append('Числа нагрузки сейчас недоступны.')
    if watch is not None:
        lag = watch.lag
        lines.append(f'Цикл отвечает за {lag:.1f} с (норма меньше секунды)' + (' — перегружен, плановые дела ждут.' if agents.overloaded() else '.'))
        if watch.recent:
            last = watch.recent[-1]
            from ..core.stall import told
            lines.append(f"Последнее зависание: {calendar(last['started'])}, {last['seconds']} с — {told(last)}.")
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
    try:
        size = host.store.size_bytes() / 2**20
        lines.append(f'База: {size:.0f} МБ' + (', копия перед миграцией снимается в фоне' if host.store.copying() else '') + '.')
    except Exception:  # noqa: BLE001 — размер базы не должен ломать остальной ответ
        pass
    lines += timing_lines(getattr(host, 'telegram', None))
    return '\n'.join(lines)


def timing_lines(tg) -> list[str]:
    """Отклик Telegram за последние ответы: где ждали — до хода, модель до первого слова, весь ответ (медианы)."""
    items = list(getattr(tg, 'timing', None) or [])
    if not items:
        return []
    def mid(key):
        values = sorted(i[key] for i in items)
        return f'{values[len(values) // 2]:.1f}'.replace('.', ',')
    slow = max(items, key=lambda i: i['total'])
    return [f"Отклик Telegram (последних ответов: {len(items)}, медиана): до хода {mid('queue')} с, модель до первого слова {mid('model')} с, "
            f"весь ответ {mid('total')} с; самый долгий — {slow['total']:.0f} с, из них модель {slow['model']:.0f} с."]
