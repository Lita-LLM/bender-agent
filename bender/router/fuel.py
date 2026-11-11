"""Топливо: только числовые квоты и безопасные выдержки, без фонового опроса."""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import re
import time
from urllib.parse import urlsplit, urlunsplit

from ..core.errors import HumanError
from ..guard.policy import Action
from ..net.http_client import NetError, Timeouts


def number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) and n >= 0 else None
    except (TypeError, ValueError, OverflowError): return None


def reset(value, now):
    """Unix, ISO или длительность OpenAI (1m2.5s); не угадываем неизвестный формат."""
    if value is None: return None
    n = number(value)
    if n is not None: return n
    value = str(value)
    try: return dt.datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()
    except (ValueError, OverflowError, OSError): pass
    if re.fullmatch(r'(?:\d+(?:\.\d+)?(?:ms|[smhd]))+', value):
        return now + sum(float(n) * {'ms': .001, 's': 1, 'm': 60, 'h': 3600, 'd': 86400}[unit] for n, unit in re.findall(r'(\d+(?:\.\d+)?)(ms|[smhd])', value))
    return None


def window(name, used=None, remaining=None, limit=None, reset_at=None, minutes=None):
    if used is None and remaining is not None and limit: used = 100 * (1 - remaining / limit)
    if used is None and remaining is None: return None
    return {'name': name, 'used_percent': min(100, max(0, used)) if used is not None else None,
            'remaining': remaining, 'limit': limit, 'reset_at': reset_at, 'minutes': minutes}


def headers_windows(headers, now):
    h = {str(k).lower(): v for k, v in headers.items()}
    out = []
    # Имена и абсолютный reset-at сверены с codex-api/src/rate_limits.rs.
    prefixes = {'x-codex'} | {k.rsplit('-primary-used-percent', 1)[0] for k in h if re.fullmatch(r'x-codex-[a-z0-9-]+-primary-used-percent', k)}
    for prefix in sorted(prefixes):
        for name in ('primary', 'secondary'):
            base = f'{prefix}-{name}-'
            used = number(h.get(base + 'used-percent'))
            minutes = number(h.get(base + 'window-minutes'))
            title = ('5 ч' if minutes == 300 else 'неделя' if minutes == 10080 else f'{minutes:g} мин' if minutes else name)
            if prefix != 'x-codex': title = prefix[2:] + ' ' + title
            w = window(title, used=used, reset_at=reset(h.get(base + 'reset-at'), now), minutes=minutes)
            if w: out.append((base, w))
    for family, names in (('x-ratelimit', ('requests', 'tokens', 'input-tokens', 'output-tokens')), ('anthropic-ratelimit', ('requests', 'tokens', 'input-tokens', 'output-tokens'))):
        for name in names:
            base = family + '-'
            suffix = '-' + name if family == 'x-ratelimit' else ''
            stem = base + name + '-' if family == 'anthropic-ratelimit' else base
            remaining = number(h.get(stem + 'remaining' + suffix))
            limit = number(h.get(stem + 'limit' + suffix))
            w = window(name, remaining=remaining, limit=limit, reset_at=reset(h.get(stem + 'reset' + suffix), now))
            if w: out.append((family + '-' + name, w))
    for name in ('5h', '7d', '7d-opus', '7d-sonnet'):
        base = 'anthropic-ratelimit-unified-' + name
        usage = number(h.get(base + '-utilization'))
        if usage is not None:
            out.append((base, window(name, used=usage * 100, reset_at=reset(h.get(base + '-reset'), now))))
    return out


class Fuel:
    def __init__(self, app):
        self.app = app
        self.accounts = {}
        self.refresh_error = ''

    def observe(self, candidate, account, kind, data):
        if not self.app.settings.get('fuel', 'enabled', True): return
        p = candidate.provider
        row = self.accounts.setdefault((p['id'], account), {'provider': p['id'], 'account': account, 'windows': {}, 'models': {}})
        now = time.time()
        model = row['models'].setdefault(candidate.model, {})
        if kind == 'headers':
            for key, value in headers_windows(data, now): self._window(row, key, value, now)
        elif kind == 'claude':
            name = data.get('rateLimitType')
            if name in ('five_hour', 'seven_day', 'seven_day_opus', 'seven_day_sonnet', 'overage'):
                utilization = number(data.get('utilization'))
                used = utilization * 100 if utilization is not None else 100 if data.get('status') == 'rejected' else None
                w = window(name, used=used, reset_at=reset(data.get('resetsAt'), now))
                if w: self._window(row, 'claude-' + name, w, now)
        elif kind == 'ok': model.update(last_success=now, seconds=round(data['seconds'], 3))
        elif kind == 'error':
            from ..agents.runtime import ACTIVE
            from ..mind.passport import clean
            error = clean(self.app, {k: data.get(k) for k in ('http', 'class', 'body')})
            error.update(at=now, agent_id=getattr(ACTIVE.get(), 'agent_id', 'main'))
            model['error'] = error
            if data.get('http') in (429, 402): row['last_limit_error'] = error
        self.app.bus.publish({'type': 'fuel', 'agent_id': '*', 'fuel': self.snapshot()})

    def _window(self, row, key, value, now):
        value = dict(value, observed_at=now)
        row['windows'][key] = value
        used = value['used_percent']
        if used is None or not self.app.settings.get('fuel', 'notify', True): return
        end = value['reset_at']
        # Без срока сброса можем гарантировать только одну пару предупреждений за процесс.
        period = str(int(end)) if end else 'unknown'
        cache_key = 'fuel:notice:' + hashlib.sha256(f"{row['provider']}:{row['account']}:{key}:{period}".encode()).hexdigest()
        previous = self.app.store.meta_get(cache_key) or 0
        level = 100 if used >= 100 else 85 if used >= 85 else 0
        if level <= previous: return
        self.app.store.meta_set(cache_key, level)
        from ..mind.notices import home
        p = self.app.models.providers().get(row['provider'], {})
        text = f"⛽ {p.get('name', row['provider'])}, аккаунт {row['account'] + 1}: {value['name']} — израсходовано {used:.0f}%."
        self.app.mind._spawn(home(self.app, text, 'fuel'))

    def snapshot(self, *, agent_id=None):
        from ..mind.passport import clean
        def scoped(error):
            if error and agent_id is not None and error.get('agent_id', 'main') != agent_id:
                return {k: v for k, v in error.items() if k != 'body'}
            return error
        rows = []
        providers = self.app.models.providers()
        for (pid, account), row in self.accounts.items():
            if pid not in providers: continue
            models = []
            for name, state in row['models'].items():
                blocked, left, reason = self.app.router.health.blocked(pid, account, name)
                models.append({'id': name, **state, 'error': scoped(state.get('error')), 'health': 'cooling' if blocked else 'ok' if state.get('last_success') else 'unknown', 'cooling': round(left), 'reason': reason})
            rows.append({'provider': pid, 'name': providers[pid].get('name', pid), 'account': account + 1,
                         'windows': list(row['windows'].values()), 'models': models, 'last_limit_error': scoped(row.get('last_limit_error'))})
        return clean(self.app, {'accounts': rows, 'refresh_error': self.refresh_error, 'enabled': self.app.settings.get('fuel', 'enabled', True)})

    def brief(self):
        parts = []
        for row in self.snapshot()['accounts']:
            for w in row['windows']:
                used = w['used_percent']
                if used is None: continue
                reset_at = w['reset_at']
                if reset_at and reset_at <= time.time(): continue
                when = ''
                if reset_at:
                    try: when = ', сброс ' + dt.datetime.fromtimestamp(reset_at).strftime('%d.%m %H:%M')
                    except (ValueError, OverflowError, OSError): pass
                parts.append(f"{row['name']} #{row['account']} {w['name']} — {100-used:.0f}% осталось{when}")
        return ('Топливо (последнее измерение): ' + '; '.join(parts[:4])) if parts else ''

    async def refresh(self):
        """Только явная кнопка/Полигон; документированный self-service API OmniRoute."""
        if not self.app.settings.get('fuel', 'enabled', True): return self.snapshot()
        errors = []
        for p in self.app.models.providers().values():
            if p.get('preset') != 'omniroute' or p.get('enabled') is False: continue
            u = urlsplit(p.get('base_url', ''))
            if u.username or u.password or u.query or u.fragment:
                errors.append('Адрес OmniRoute должен быть без логина и параметров; ключ хранится в сейфе.'); continue
            path = u.path.rstrip('/')
            if path.endswith('/v1'): path = path[:-3]
            url = urlunsplit((u.scheme, u.netloc, path + '/api/usage/om-usage', 'format=json', ''))
            decision = await self.app.guard.check(Action('net.read', url, actor='owner', summary='Прочитать квоты OmniRoute без запроса модели'))
            if not decision.allowed: continue
            for account, key in self.app.router._keys(p):
                if not key:
                    errors.append('Для квот OmniRoute нужен ключ с allowUsageCommand.'); continue
                try:
                    resp = await self.app.http.request('GET', url, headers={'Authorization': 'Bearer ' + key}, timeouts=Timeouts(connect=10, read=20, total=25))
                    async with resp:
                        data = json.loads(await resp.read(limit=1024 * 1024)) if resp.ok else None
                        if not resp.ok or not isinstance(data, dict) or data.get('allowed') is not True:
                            errors.append(f'OmniRoute: HTTP {resp.status}; включи allowUsageCommand у своего ключа.'); continue
                    self._omni(p, account, data)
                except (ValueError, TypeError, OSError, HumanError, NetError):
                    errors.append('Не удалось прочитать квоты OmniRoute. Проверь соединение и разрешение ключа.')
        self.refresh_error = ' '.join(dict.fromkeys(errors))
        self.app.bus.publish({'type': 'fuel', 'fuel': self.snapshot()})
        return self.snapshot()

    def _omni(self, p, account, data):
        row = self.accounts.setdefault((p['id'], account), {'provider': p['id'], 'account': account, 'windows': {}, 'models': {}})
        now = time.time()
        snapshots = data.get('providers')
        if not isinstance(snapshots, list): snapshots = [data.get('provider')]
        for i, entry in enumerate(snapshots[:100]):
            if not isinstance(entry, dict) or not isinstance(entry.get('quotas'), dict): continue
            # connectionId не выходит из процесса; имя/адрес/токен аккаунта не храним.
            ident = hashlib.sha256(str(entry.get('connectionId', i)).encode()).hexdigest()[:16]
            provider = re.sub('[^a-zA-Z0-9_-]', '', str(entry.get('provider', '')))[:40]
            for name, q in list(entry['quotas'].items())[:50]:
                if not isinstance(q, dict): continue
                used, remaining = number(q.get('usedPercentage')), number(q.get('remainingPercentage'))
                if used is None and remaining is not None: used = 100 - remaining
                if used is None:
                    a, b = number(q.get('used')), number(q.get('total'))
                    if a is not None and b: used = 100 * a / b
                    elif a is not None and a <= 100: used = a
                    elif number(q.get('remaining')) is not None and number(q.get('remaining')) <= 100: used = 100 - number(q.get('remaining'))
                title = f'{provider} #{i+1}: {str(name)[:60]}'
                w = window(title, used=used, reset_at=reset(q.get('resetAt'), now))
                if w: self._window(row, 'omni-' + ident + '-' + str(name)[:60], w, now)
        personal = data.get('personal')
        if isinstance(personal, dict):
            for period, title in (('daily', 'день, $'), ('weekly', 'неделя, $')):
                spent, limit = number(personal.get(period + 'SpentUsd')), number(personal.get(period + 'LimitUsd'))
                if spent is not None and limit:
                    self._window(row, 'omni-personal-' + period, window(title, used=100*spent/limit, reset_at=reset(personal.get(period+'ResetAtIso'), now)), now)
