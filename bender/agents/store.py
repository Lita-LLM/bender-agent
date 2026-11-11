"""Приватное пространство таблиц в том же SQLite; значения SQL не переписываются."""
from __future__ import annotations

import re
from ..core.store import BASE_SCHEMA, Store

TOKEN = re.compile(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|\b[A-Za-z_]\w*\b|[().,]")
CREATE = re.compile(r'(?is)\bCREATE\s+(?:UNIQUE\s+)?(?:VIRTUAL\s+)?(TABLE|INDEX)\s+(?:IF\s+NOT\s+EXISTS\s+)?([A-Za-z_]\w*)')


def valid_id(value):
    from ..core.errors import HumanError
    if not isinstance(value, str) or not re.fullmatch('[a-z][a-z0-9_]{0,39}', value):
        raise HumanError('agent_id', 'Некорректное имя агента', 'Латинские буквы, цифры и _, до 40 знаков; первая — буква.')
    return value


class Connection:
    def __init__(self, db, agent_id):
        self.raw = db
        self.prefix = 'agent_' + valid_id(agent_id) + '__'
        self.names = {r['name'] for r in db.execute("SELECT name FROM sqlite_master WHERE type IN ('table','index')")
                      if not r['name'].startswith(('sqlite_', 'agent_', 'flock_'))}
        self.names.update(r['name'][len(self.prefix):] for r in db.execute("SELECT name FROM sqlite_master") if r['name'].startswith(self.prefix))
        for sql in BASE_SCHEMA:
            match = CREATE.search(sql)
            if match: self.names.add(match[2])

    def sql(self, sql):
        match = CREATE.search(sql)
        if match: self.names.add(match[2])
        tokens = list(TOKEN.finditer(sql)); changes = {}
        expected, insert_target = False, None
        is_index = bool(match and match[1].upper() == 'INDEX')
        for i, token in enumerate(tokens):
            value = token[0]
            quoted = value.startswith(('"', "'"))
            name = value[1:-1].replace('""', '"') if value.startswith('"') else value
            upper = name.upper()
            prev = tokens[i-1][0].upper() if i else ''
            nxt = tokens[i+1][0].upper() if i+1 < len(tokens) else ''
            relation = expected and upper not in ('IF', 'NOT', 'EXISTS')
            if expected and upper in ('IF', 'NOT', 'EXISTS'): continue
            if relation:
                expected = False
                if name in self.names:
                    changes[token.start()] = self.prefix+name
                    if prev == 'INTO': insert_target = name
            # Квалификатор таблицы и специальные функции FTS; обычная колонка meta не меняется.
            if name in self.names and (nxt in ('.', 'MATCH') or
                (prev == '(' and i >= 2 and tokens[i-2][0].upper() in ('BM25','HIGHLIGHT','SNIPPET','TABLE_INFO')) or
                (insert_target == name and prev == '(')):
                changes[token.start()] = self.prefix+name
            if not quoted and upper in ('FROM','JOIN','INTO','UPDATE','TABLE','INDEX','REFERENCES'):
                expected = True
            if is_index and upper == 'ON': expected = True
        return ''.join(sql[(tokens[i-1].end() if i else 0):t.start()] + changes.get(t.start(), t[0]) for i,t in enumerate(tokens)) + (sql[tokens[-1].end():] if tokens else sql)

    def execute(self, sql, params=()): return self.raw.execute(self.sql(sql), params)
    def executemany(self, sql, params): return self.raw.executemany(self.sql(sql), params)
    def __getattr__(self, name): return getattr(self.raw, name)


class NamespaceStore(Store):
    """Один файл и соединение хозяина, общая блокировка; свои meta/схемы/FTS и строки."""
    def __init__(self, shared, agent_id):
        self.path, self._lock = shared.path, shared._lock
        self._settings_root = shared._settings_root
        self._migration_backup = shared._migration_backup
        self.agent_id = valid_id(agent_id)
        self.db = Connection(shared.db, agent_id)
        self.migrate('base', 1, BASE_SCHEMA)

    def _has_meta(self):
        return bool(self.db.raw.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (self.db.prefix+'meta',)).fetchone())

    def migrate(self, part, version, statements):
        # Имена заранее известны и для уже применённой миграции после перезапуска.
        for sql in statements:
            match = CREATE.search(sql)
            if match: self.db.names.add(match[2])
        super().migrate(part, version, statements)
        tables = []
        for sql in statements:
            match = CREATE.search(sql)
            if match and match[1].upper() == 'TABLE' and 'VIRTUAL' not in sql.upper(): tables.append(match[2])
        for table in tables:
            if 'agent_id' not in {r['name'] for r in self.q('PRAGMA table_info('+table+')')}:
                super().migrate('agent_tag:'+table, 1, ["ALTER TABLE "+table+" ADD COLUMN agent_id TEXT NOT NULL DEFAULT '"+self.agent_id+"'"])

    def close(self): pass  # физическое соединение закрывает хозяин после всех контекстов
