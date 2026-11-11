"""Знак агента в сообщениях: «🦊: текст», эмодзи у каждого агента стаи свой. Эмодзи из IDENTITY.md, если его ещё не занял
другой агент стаи; иначе — первый свободный из палитры. Выбор запоминается в записи стаи,
поэтому знак не прыгает от сообщения к сообщению."""
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
    taken = set()
    for other in host.store.q('SELECT data FROM flock_agents WHERE id!=? AND archived=0', (agent,)):
        taken.add(json.loads(other['data'] or '{}').get('badge'))
    current = data.get('badge')
    choice = want if want and want not in taken else current if current and current not in taken else \
        next((e for e in PALETTE if e not in taken), PALETTE[0])
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

