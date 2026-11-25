"""Знак агента в сообщениях: «🦊: текст», эмодзи у каждого агента стаи свой. Эмодзи из IDENTITY.md, если его ещё не занял
другой агент стаи; иначе — прежний знак агента; иначе первый свободный из палитры. Выбор запоминается в записи стаи,
поэтому знак не прыгает от сообщения к сообщению.

Правила простые: знак агента, уже записанный в стае, другому не отдаётся — и знак агента в архиве тоже (его старые
сообщения в чате не должны выглядеть словами нового соседа), пока в палитре есть свободные. 🤖 палитра бережёт для
главного: новый агент его не получает, если главный ещё не выбрал себе другой. Временные помощники знаки не держат."""
import json

from ..core.store import dumps

PALETTE = ('🤖', '🦊', '🐺', '🦉', '🐙', '🐝', '🦄', '🐢', '🦅', '🐬', '🦁', '🐼', '🐸', '🦋', '🐧', '🐳', '🦜', '🐞', '🦔', '🐨',
           '🦌', '🐿', '🦩', '🐠', '🦚', '🐌', '🦀', '🐇', '🦭', '🐊')


def _host(ctx):
    host = getattr(ctx, 'host', None)
    return host if host is not None and hasattr(host, 'agents') else ctx


def emoji(ctx):
    host = _host(ctx)
    agents = getattr(host, 'agents', None)
    agent = getattr(ctx, 'agent_id', 'main')
    want = (ctx.selfdir.identity().get('emoji') or '').split()
    want = want[0] if want and len(want[0]) <= 8 else ''
    if agents is None: return want or PALETTE[0]
    row = host.store.one('SELECT data FROM flock_agents WHERE id=?', (agent,))
    if not row: return want or PALETTE[0]
    data = json.loads(row['data'] or '{}')
    taken, resting, main = set(), set(), None
    for other in host.store.q('SELECT id, archived, temporary, data FROM flock_agents WHERE id!=?', (agent,)):
        badge = json.loads(other['data'] or '{}').get('badge')
        if other['id'] == 'main': main = badge
        if not badge or other['temporary'] and other['archived']: continue
        (resting if other['archived'] else taken).add(badge)
    current = data.get('badge')
    reserved = {PALETTE[0]} if agent != 'main' and main in (None, PALETTE[0]) else set()
    free = [e for e in PALETTE if e not in taken and e not in resting and e not in reserved]
    if not free: resting, free = set(), [e for e in PALETTE if e not in taken and e not in reserved]  # палитра кончилась — знаки архива снова в ходу
    blocked = taken | resting
    choice = want if want and want not in blocked else current if current and current not in taken else \
        (free[0] if free else PALETTE[0])
    if choice != current:
        data['badge'] = choice
        host.store.x('UPDATE flock_agents SET data=? WHERE id=?', (dumps(data), agent))
    return choice


def name(ctx):
    found = ctx.selfdir.identity().get('name')
    if found: return found
    host = _host(ctx)
    try: return host.agents.record(getattr(ctx, 'agent_id', 'main'))['name']
    except Exception: return getattr(ctx, 'agent_id', 'main')  # noqa: BLE001


def line(ctx):
    """«🦊 Луна» — эмодзи с именем (для экранов, где нужен и знак, и имя)."""
    return emoji(ctx) + ' ' + name(ctx)

