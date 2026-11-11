"""Расход моделей по часам: только числа и виды ходов, без текстов, названий чатов и ключей.

`bender usage` читает bender.db только на чтение. По каждому часу и агенту видно, что звало модель
(разговор, пульс, расписание, переписка стаи, разбор, сжатие, сон), сколько было вызовов и токенов
(вход, из них из кэша, выход, из него рассуждения) и сколько вызовов упало. Отдельно — самые
прожорливые сессии (короткий id и вид) и письма стаи по часам (кто кому, сколько).
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from ..core.errors import HumanError

KINDS = {'main': 'главный чат', 'chat': 'чат', 'heartbeat': 'пульс: сам', 'cron': 'расписание', 'correspondence': 'переписка стаи',
         'subagent': 'субагент', 'api': 'API', 'imported': 'перенесённый', 'task': 'задача'}
# служебные вызовы: notice — фиксированная строка из кода (side.ask, сон, пульс); неизвестное не печатаем
PURPOSES = (('разбор после хода', 'разбор после хода'), ('сжатие разговора', 'сжатие'), ('пульс', 'пульс: суждение'), ('сон', 'сон'),
            ('навык', 'навык'), ('самопроверка', 'проверка'), ('Проверка', 'проверка'), ('живой экзамен', 'проверка'), ('MCP sampling', 'MCP'))


def kind_of(notice, session_kind) -> str:
    text = str(notice or '')
    purpose = next((title for key, title in PURPOSES if text.startswith(key)), '')
    if purpose:
        return purpose
    return KINDS.get(session_kind, 'другое') if session_kind else 'служебное'


def tables(db) -> list[tuple[str, str, str]]:
    """(агент, таблица вызовов, таблица сессий) для главного и для каждого агента стаи."""
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    out = [('main', 'calls', 'sessions')] if 'calls' in names else []
    for name in sorted(names):
        m = re.fullmatch(r'agent_([A-Za-z0-9_-]+)__calls', name)
        if m:
            out.append((m[1], name, f'agent_{m[1]}__sessions' if f'agent_{m[1]}__sessions' in names else ''))
    return out


def rows(db, since: float) -> list[dict]:
    out = []
    for agent, calls, sessions in tables(db):
        kinds = {r[0]: r[1] for r in db.execute(f'SELECT id, kind FROM "{sessions}"')} if sessions else {}
        for r in db.execute(f'SELECT session_id, created_at, role, model, usage, notice, error FROM "{calls}" WHERE created_at >= ? ORDER BY created_at', (since,)):
            try:
                u = json.loads(r[4] or '{}')
            except ValueError:
                u = {}
            num = lambda k: int(u.get(k) or 0) if str(u.get(k) or 0).isdigit() else 0  # noqa: E731
            skind = kinds.get(r[0]) if r[0] else None
            out.append({'agent': agent, 'ts': float(r[1]), 'session': r[0] or '', 'kind': kind_of(r[5], skind),
                        'session_kind': KINDS.get(skind, 'другое') if skind else 'служебное',
                        'role': str(r[2] or ''), 'model': str(r[3] or '')[:40], 'error': bool(r[6]),
                        'input': num('input'), 'cached': num('cache_read'), 'output': num('output'), 'reasoning': num('reasoning')})
    return out


def letters(db, since: float) -> list[tuple]:
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='flock_letters'").fetchone():
        return []
    return db.execute("SELECT strftime('%m-%d %H:00', created, 'unixepoch', 'localtime') h, sender, recipient, COUNT(*) FROM flock_letters "
                      "WHERE created >= ? GROUP BY h, sender, recipient ORDER BY h", (since,)).fetchall()


def k(n: int) -> str:
    return f'{n / 1_000_000:.1f}M' if n >= 1_000_000 else f'{n / 1000:.0f}k' if n >= 1000 else str(n)


def span(first: float, last: float) -> str:
    a, b = time.localtime(first), time.localtime(last)
    if (a.tm_year, a.tm_yday) == (b.tm_year, b.tm_yday):
        return time.strftime('%m-%d %H:%M', a) + '–' + time.strftime('%H:%M', b)
    return time.strftime('%m-%d %H:%M', a) + ' – ' + time.strftime('%m-%d %H:%M', b)


def report(data: Path, hours: float = 24, model: str = '') -> str:
    path = Path(data).expanduser() / 'bender.db'
    if not path.exists():
        raise HumanError('usage_no_db', 'Не нашёл bender.db', f'Укажи папку данных: bender --data ПАПКА usage (искал {path.parent}).')
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    try:
        since = time.time() - hours * 3600
        data_rows = [r for r in rows(db, since) if not model or model.lower() in r['model'].lower()]
        mail = letters(db, since)
    finally:
        db.close()
    lines = [f'Расход моделей за {hours:g} ч' + (f', модели «{model}»' if model else '') + ' (местное время). Тексты не показываются.',
             'вход — все токены запроса, кэш — из них из кэша провайдера, выход — ответ, из него рассуждения.', '']
    if not data_rows:
        return '\n'.join(lines + ['Вызовов нет.'])
    groups: dict[tuple, dict] = {}
    for r in data_rows:
        key = (time.strftime('%m-%d %H:00', time.localtime(r['ts'])), r['agent'], r['kind'], r['model'])
        g = groups.setdefault(key, {'calls': 0, 'errors': 0, 'input': 0, 'cached': 0, 'output': 0, 'reasoning': 0, 'max': 0})
        g['calls'] += 1
        g['errors'] += r['error']
        for f in ('input', 'cached', 'output', 'reasoning'):
            g[f] += r[f]
        g['max'] = max(g['max'], r['input'])
    head = f"{'час':<12}{'агент':<12}{'что':<20}{'модель':<22}{'вызовов':>8}{'сбоев':>6}{'вход':>8}{'кэш':>8}{'выход':>8}{'рассужд':>8}{'макс.вход':>10}"
    lines += [head, '-' * len(head)]
    for (hour, agent, kind, mdl), g in sorted(groups.items()):
        lines.append(f"{hour:<12}{agent[:11]:<12}{kind[:19]:<20}{mdl[:21]:<22}{g['calls']:>8}{g['errors']:>6}{k(g['input']):>8}{k(g['cached']):>8}"
                     f"{k(g['output']):>8}{k(g['reasoning']):>8}{k(g['max']):>10}")
    total = {f: sum(r[f] for r in data_rows) for f in ('input', 'cached', 'output', 'reasoning')}
    lines += ['', f"Итого: вызовов {len(data_rows)}, сбоев {sum(r['error'] for r in data_rows)}, вход {k(total['input'])} (из кэша {k(total['cached'])}), "
                  f"выход {k(total['output'])} (рассуждения {k(total['reasoning'])})."]
    by_kind: dict[str, int] = {}
    for r in data_rows:
        by_kind[r['kind']] = by_kind.get(r['kind'], 0) + r['input'] + r['output']
    lines.append('По видам (вход+выход): ' + ', '.join(f'{n} {k(v)}' for n, v in sorted(by_kind.items(), key=lambda x: -x[1])))
    sessions: dict[tuple, dict] = {}
    for r in data_rows:
        if r['session']:
            # вид сессии, а не первого вызова: иначе разговор, начатый со сжатия, подписывался «сжатие»
            s = sessions.setdefault((r['agent'], r['session']), {'kind': r['session_kind'], 'calls': 0, 'tokens': 0, 'first': r['ts'], 'last': r['ts']})
            s['calls'] += 1
            s['tokens'] += r['input'] + r['output']
            s['last'] = r['ts']
    if sessions:
        lines += ['', 'Самые прожорливые сессии (id — последние 8 знаков):']
        for (agent, sid), s in sorted(sessions.items(), key=lambda x: -x[1]['tokens'])[:8]:
            lines.append(f"  {agent[:11]:<12}{sid[-8:]:<10}{s['kind']:<18} вызовов {s['calls']:<5} токенов {k(s['tokens']):<7} {span(s['first'], s['last'])}")
    if mail:
        lines += ['', 'Письма стаи по часам:'] + [f'  {h}  {a} → {b}: {n}' for h, a, b, n in mail]
    return '\n'.join(lines)


def main(args) -> int:
    from ..core.paths import default_data_dir
    try:
        print(report(Path(args.data or default_data_dir()), hours=float(args.hours), model=args.model or ''))
        return 0
    except HumanError as exc:
        print(exc.title + '. ' + exc.hint)
        return 1
