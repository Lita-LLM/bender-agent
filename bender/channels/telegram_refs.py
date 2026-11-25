"""Учёт сообщений Telegram: чьё сообщение (агент и разговор) и что в нём было.

Раньше это были два JSON в meta — 'telegram:refs' у каждого агента и 'telegram:message_routes' у хозяина, —
и каждое запомненное сообщение переписывало их целиком: десятки мегабайт и сотни миллисекунд ЦП в цикле
событий, а маршруты выпадали через ~1000 обменов. Теперь — строка на сообщение:

- telegram_routes в базе хозяина: сообщение → агент и разговор (общий бот стаи, все чаты);
- telegram_refs у каждого агента (NamespaceStore сам добавляет префикс, главный — в базе хозяина):
  данные сообщения JSON-ом, незнакомые поля сохраняются.

Ключ прежний: «чат:сообщение» (str(int(chat))+':'+str(mid)); номер темы в ключ не входит, он лежит в данных.

Совместимость: прежние JSON только читаются. Один раз на базу они переносятся в таблицу; флаг в meta
помнит длину перенесённого JSON, так что дописанное прежним слотом после отката переносится при следующем
запуске (строки таблицы при этом главнее). Сами JSON не удаляются и не переписываются: прежний слот после
отката видит их застывшими на момент обновления — старые сообщения узнаёт, новые нет.

Обрезка редкая — одним DELETE раз в PRUNE_EVERY записей (и при первой записи процесса), не на каждую запись.
"""
from __future__ import annotations

import json
import time
import weakref

ROUTES = 'telegram_routes'
REFS = 'telegram_refs'
LEGACY = {ROUTES: ('telegram:message_routes', 'telegram:routes_imported'), REFS: ('telegram:refs', 'telegram:refs_imported')}
KEEP = {ROUTES: 50000, REFS: 10000}  # маршруты крошечные; данные несут текст до 4000+4096 знаков
PRUNE_EVERY = 500

SCHEMA = {
    ROUTES: ["""CREATE TABLE IF NOT EXISTS telegram_routes (key TEXT PRIMARY KEY, chat INTEGER, message INTEGER,
                agent TEXT NOT NULL, session TEXT, created REAL)""",
             "CREATE INDEX IF NOT EXISTS telegram_routes_by_created ON telegram_routes(created)"],
    REFS: ["""CREATE TABLE IF NOT EXISTS telegram_refs (key TEXT PRIMARY KEY, session TEXT, direction TEXT,
              data TEXT NOT NULL, created REAL)""",
           "CREATE INDEX IF NOT EXISTS telegram_refs_by_created ON telegram_refs(created)"],
}
PUT_ROUTE = ("INSERT INTO telegram_routes(key, chat, message, agent, session, created) VALUES(?,?,?,?,?,?) "
             "ON CONFLICT(key) DO UPDATE SET agent=excluded.agent, session=excluded.session, created=excluded.created")
PUT_REF = ("INSERT INTO telegram_refs(key, session, direction, data, created) VALUES(?,?,?,?,?) "
           "ON CONFLICT(key) DO UPDATE SET session=excluded.session, direction=excluded.direction, data=excluded.data, created=excluded.created")

_STATE = weakref.WeakKeyDictionary()  # хранилище → {таблица: готовность и счётчик записей до обрезки}


def key(chat, mid):
    """Ключ как в прежних JSON. Адрес темы — int с .thread: в ключ идёт только номер чата."""
    if chat is None or mid is None or isinstance(mid, bool):
        return None
    try:
        return str(int(chat)) + ':' + str(int(mid))
    except (TypeError, ValueError):
        return None


def _host_store(host):
    """Маршруты всегда у хозяина: годится контекст хозяина, агента или само хранилище."""
    host = getattr(host, 'host', host)
    return getattr(host, 'store', host)


def _store(ctx):
    return getattr(ctx, 'store', ctx)


def _raw(store):
    return getattr(store.db, 'raw', store.db)


def _load(value):
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def prepare(store, table):
    """Таблица создана и прежний JSON перенесён — один раз на объект хранилища за процесс."""
    state = _STATE.setdefault(store, {}).setdefault(table, {'ready': False, 'added': 0, 'pruned': False})
    if not state['ready']:
        store.migrate(table, 1, SCHEMA[table])
        _import(store, table)
        state['ready'] = True
    return state


def migrate_host(store):
    """Схемы базы хозяина — на старте, рядом с остальными миграциями: снимок перед миграцией не ложится на первое сообщение."""
    for table in (ROUTES, REFS):
        store.migrate(table, 1, SCHEMA[table])


def _import(store, table):
    from ..core.store import dumps
    blob, flag = LEGACY[table]
    row = store.one('SELECT length(value) AS n FROM meta WHERE key = ?', (blob,))
    size = int(row['n'] or 0) if row else 0
    if not size:
        return
    done = store.meta_get(flag)
    if isinstance(done, dict) and done.get('length') == size:
        return  # уже перенесено, прежний слот JSON с тех пор не трогал — не разбираем
    try:
        old = store.meta_get(blob)
    except ValueError:
        old = None  # испорченный прежний JSON не мешает новой таблице; флаг ниже не даст разбирать его снова
    items = [(k, v) for k, v in old.items() if isinstance(k, str) and isinstance(v, dict)] if isinstance(old, dict) else []
    start = time.time() - len(items) * 0.001  # порядок прежнего JSON = порядок времени: обрезка уберёт старейшие
    rows = []
    for n, (k, v) in enumerate(items):
        created = start + n * 0.001
        if table == ROUTES:
            if not isinstance(v.get('agent'), str):
                continue
            chat, _, mid = k.rpartition(':')
            try:
                chat, mid = int(chat), int(mid)
            except ValueError:
                chat = mid = None
            rows.append((k, chat, mid, v['agent'], v.get('session'), created))
        else:
            rows.append((k, v.get('session'), v.get('direction'), dumps(v), created))
    sql = ('INSERT OR IGNORE INTO telegram_routes(key, chat, message, agent, session, created) VALUES(?,?,?,?,?,?)' if table == ROUTES
           else 'INSERT OR IGNORE INTO telegram_refs(key, session, direction, data, created) VALUES(?,?,?,?,?)')
    with store.tx() as db:
        if rows:
            db.executemany(sql, rows)
        store.meta_set(flag, {'length': size, 'count': len(rows), 'at': time.time()})


def _prune(store, table, state, added):
    state['added'] += added
    if state['pruned'] and state['added'] < PRUNE_EVERY:
        return
    state['added'], state['pruned'] = 0, True
    store.x('DELETE FROM ' + table + ' WHERE key IN (SELECT key FROM ' + table + ' ORDER BY created DESC LIMIT -1 OFFSET ?)', (KEEP[table],))


def _keys(chat, mids):
    """[(ключ, номер)] для номера или списка номеров; не-числа пропускаются, как прежде."""
    out = {}
    for mid in mids if isinstance(mids, (list, tuple)) else [mids]:
        if isinstance(mid, int) and not isinstance(mid, bool):
            k = key(chat, mid)
            if k:
                out[k] = int(mid)
    return list(out.items())


def route(host, chat, mid):
    """{'agent', 'session'} сообщения или None."""
    k = key(chat, mid)
    if k is None:
        return None
    store = _host_store(host)
    prepare(store, ROUTES)
    row = store.one('SELECT agent, session FROM telegram_routes WHERE key = ?', (k,))
    return {'agent': row['agent'], 'session': row['session']} if row else None


def put_routes(host, chat, mids, agent, session):
    """Маршрут одного сообщения или нескольких (куски ответа, альбом) — одной транзакцией."""
    store = _host_store(host)
    state = prepare(store, ROUTES)
    keys = _keys(chat, mids)
    if not keys:
        return 0
    now = time.time()
    with store.tx() as db:
        db.executemany(PUT_ROUTE, [(k, int(chat), mid, agent, session, now) for k, mid in keys])
    _prune(store, ROUTES, state, len(keys))
    return len(keys)


def drop_route(host, chat, mid):
    k = key(chat, mid)
    if k is None:
        return 0
    store = _host_store(host)
    prepare(store, ROUTES)
    return store.x('DELETE FROM telegram_routes WHERE key = ?', (k,))


def ref(ctx, chat, mid):
    """Данные запомненного сообщения агента (копия) или None."""
    k = key(chat, mid)
    if k is None:
        return None
    store = _store(ctx)
    prepare(store, REFS)
    row = store.one('SELECT data FROM telegram_refs WHERE key = ?', (k,))
    return _load(row['data']) if row else None


def put_ref(ctx, chat, mids, data):
    """Запомнить сообщение (или несколько с одними данными) целиком, как прежний refs[key] = data."""
    from ..core.store import dumps
    store = _store(ctx)
    state = prepare(store, REFS)
    keys = _keys(chat, mids)
    if not keys:
        return 0
    now, value = time.time(), dumps(data)
    with store.tx() as db:
        db.executemany(PUT_REF, [(k, data.get('session'), data.get('direction'), value, now) for k, _ in keys])
    _prune(store, REFS, state, len(keys))
    return len(keys)


def update_ref(ctx, chat, mid, **changes):
    """Дополнить данные известного сообщения; незнакомые поля остаются. None — сообщение неизвестно."""
    from ..core.store import dumps
    k = key(chat, mid)
    if k is None:
        return None
    store = _store(ctx)
    prepare(store, REFS)
    with store.tx() as db:
        row = db.execute('SELECT data FROM telegram_refs WHERE key = ?', (k,)).fetchone()
        if not row:
            return None
        data = {**(_load(row['data']) or {}), **changes}
        db.execute('UPDATE telegram_refs SET data = ?, session = ?, direction = ? WHERE key = ?',
                   (dumps(data), data.get('session'), data.get('direction'), k))
    return data


def remember(host, ctx, chat, mids, data, per=None):
    """Данные у агента и маршрут у хозяина — одной транзакцией на все номера (общее соединение базы).
    per — {номер: поля поверх общих данных}: у каждого сообщения альбома или пачки своя подпись и вложение."""
    from ..core.store import dumps
    hstore, rstore = _host_store(host), _store(ctx)
    routes, refs = prepare(hstore, ROUTES), prepare(rstore, REFS)
    keys = _keys(chat, mids)
    if not keys:
        return 0
    now, value, session = time.time(), dumps(data), data.get('session')
    per = per if isinstance(per, dict) else {}
    route_rows = [(k, int(chat), mid, ctx.agent_id, session, now) for k, mid in keys]
    ref_rows = [(k, session, data.get('direction'), dumps({**data, **per[mid]}) if isinstance(per.get(mid), dict) else value, now) for k, mid in keys]
    shared = _raw(hstore) is _raw(rstore)
    with hstore.tx() as db:
        db.executemany(PUT_ROUTE, route_rows)
        if shared:
            rstore.db.executemany(PUT_REF, ref_rows)  # тот же замок и та же транзакция: NamespaceStore лишь меняет имена
    if not shared:
        with rstore.tx() as db:
            db.executemany(PUT_REF, ref_rows)
    _prune(hstore, ROUTES, routes, len(keys))
    _prune(rstore, REFS, refs, len(keys))
    return len(keys)


def count(ctx):
    """Сколько сообщений агент помнит (для полигона)."""
    store = _store(ctx)
    prepare(store, REFS)
    row = store.one('SELECT COUNT(*) AS n FROM telegram_refs')
    return int(row['n']) if row else 0
