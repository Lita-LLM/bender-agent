"""Долгая цель разговора: агент сам продолжает ход, пока не достигнет её или не упрётся в решение хозяина.

Цель живёт в мета-хранилище агента по разговору (goal:<sid>). Когда модель заканчивает шаг без тулов,
а цель активна, ход не завершается: агенту приходит служебное «продолжай», пока он не вызовет
goal done/blocked, не кончатся подталкивания (agent.goal_rounds) или он дважды подряд не ответит
одним текстом — тогда цель ставится на паузу, а хозяин получает обычный ответ.
"""
from __future__ import annotations

import time

STATES = ('active', 'blocked', 'paused', 'done', 'dropped')
IDLE_LIMIT = 2  # подряд ответов без тулов после подталкивания — значит, агент сам не видит хода


def key(sid):
    return 'goal:' + str(sid)


def get(ctx, sid):
    value = ctx.store.meta_get(key(sid))
    return value if isinstance(value, dict) and value.get('text') else None


def save(ctx, sid, goal):
    ctx.store.meta_set(key(sid), goal)
    try:
        ctx.bus.publish({'type': 'goal', 'session': sid, 'goal': goal})
    except Exception:  # noqa: BLE001 — показ в пульте не должен ломать цель
        pass
    return goal


def start(ctx, sid, text, done_when='', by='agent'):
    text = ' '.join(str(text or '').split())[:2000]
    done_when = ' '.join(str(done_when or '').split())[:1000]
    goal = {'text': text, 'done_when': done_when, 'state': 'active', 'by': by, 'created': time.time(),
            'updated': time.time(), 'rounds': 0, 'idle': 0, 'note': ''}
    return save(ctx, sid, goal)


def mark(ctx, sid, state, note=''):
    goal = get(ctx, sid)
    if not goal:
        return None
    goal.update(state=state, note=' '.join(str(note or '').split())[:2000], updated=time.time(), idle=0)
    return save(ctx, sid, goal)


def progress(ctx, sid):
    """Агент сделал шаг с тулами — счётчик «стою на месте» обнуляется."""
    goal = get(ctx, sid)
    if goal and goal['state'] == 'active' and goal.get('idle'):
        goal['idle'] = 0
        save(ctx, sid, goal)


def wake(ctx, sid):
    """Новое слово хозяина снимает блок/паузу: он ответил, цель снова в работе."""
    goal = get(ctx, sid)
    if goal and goal['state'] in ('blocked', 'paused'):
        goal.update(state='active', idle=0, rounds=0, updated=time.time())
        save(ctx, sid, goal)


def line(ctx, sid):
    """Строка в приставке запроса: цель видна и после сжатия истории."""
    goal = get(ctx, sid) if sid else None
    if not goal or goal['state'] not in ('active', 'blocked', 'paused'):
        return ''
    state = {'active': 'в работе', 'blocked': 'ждёт решения хозяина', 'paused': 'на паузе'}[goal['state']]
    return ' · ЦЕЛЬ (' + state + '): ' + goal['text'][:300] + (' — готово, когда: ' + goal['done_when'][:200] if goal.get('done_when') else '') + ' (тул goal)'


def nudge(ctx, sid, limit):
    """Текст «продолжай» или None, если ход пора отдать хозяину."""
    goal = get(ctx, sid)
    if not goal or goal['state'] != 'active' or not limit:
        return None
    if goal.get('idle', 0) >= IDLE_LIMIT or goal.get('rounds', 0) >= limit:
        why = 'агент дважды подряд ответил без действий' if goal.get('idle', 0) >= IDLE_LIMIT else f'исчерпано {limit} самопродолжений'
        mark(ctx, sid, 'paused', why)
        return None
    goal['rounds'] = goal.get('rounds', 0) + 1
    goal['idle'] = goal.get('idle', 0) + 1
    goal['updated'] = time.time()
    save(ctx, sid, goal)
    return ('[Цель ещё открыта — продолжай сам, хозяин не нужен для следующего шага. Цель: ' + goal['text']
            + (' Готово, когда: ' + goal['done_when'] if goal.get('done_when') else '')
            + f'. Самопродолжение {goal["rounds"]} из {limit}.\n'
            'Выбери следующий шаг и сделай его тулами. Не спрашивай «продолжать ли» — продолжай. '
            'Достигнута — goal action=done с итогом. Без решения хозяина дальше нельзя (деньги, необратимое, вкус, выбор между его целями) — '
            'goal action=blocked с вопросом. Подход не работает — смени его, а не останавливайся.]')
