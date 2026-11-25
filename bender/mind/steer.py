"""Новое сообщение хозяина во время хода: вставить в ход, прервать ход или оставить до конца хода.

Настройка agent.new_message (по умолчанию insert) — у каждого агента своя; разово — параметр delivery у
Mind.say и кнопки у ждущего сообщения (пульт, статус хода в Telegram, /delivery).

Вставка кооперативная: событие хода (turn.state['_steer']) поднимается, когда слово хозяина ждёт в очереди
хода. Ждущие тулы (команда, карточка Предохранителя, вопрос хозяину, помощники, письмо агенту) видят его и
возвращаются раньше, ничего не убивая молча: команда уходит в фон с номером процесса, карточка и вопрос
закрываются с понятной причиной. Ещё не начатые вызовы того же пакета не выполняются — модель видит,
какие, и повторит нужные. CLI-движок, у которого весь цикл внутри одного вызова, получает слово хозяина
в результате ближайшего тула Бендера (mcp/server.py). Длинный ответ модели не обрывается: слово ляжет
после него.
"""
from __future__ import annotations

import asyncio

MODES = ('insert', 'interrupt', 'queue')
TITLES = {'insert': 'Вставлять в ход', 'interrupt': 'Прерывать сразу', 'queue': 'После хода'}
ICONS = {'insert': '↪', 'interrupt': '⚡', 'queue': '⏳'}
# Каналы, где хозяин пишет сам и видит ход; API отвечает на каждый запрос отдельно и режим не использует.
CHANNELS = ('pult', 'terminal', 'telegram', 'matrix', 'userbot', 'speaker')
WORDS = {'insert': 'insert', 'вставлять': 'insert', 'вставить': 'insert', 'вставка': 'insert', 'i': 'insert',
         'interrupt': 'interrupt', 'прерывать': 'interrupt', 'прервать': 'interrupt', 'сразу': 'interrupt', 'x': 'interrupt',
         'queue': 'queue', 'после': 'queue', 'после хода': 'queue', 'очередь': 'queue', 'q': 'queue'}
# Метка для модели у реплики, которую она увидела посреди своего хода (одна и та же для API и CLI).
MARK = ('[Хозяин написал это посреди твоего хода. Прочитай сейчас и реши: сменить план, ответить '
        '(если ответ нужен до конца хода — коротко через send_message) или продолжить начатое.]')
# Причина для закрытой карточки, вопроса и не начатых вызовов.
READ_FIRST = 'пришло новое сообщение хозяина — прочитай его на следующем шаге; если это всё ещё нужно, повтори'
STEERED = '\0steered'  # ask_owner: хозяин не ответил на вопрос, а написал новое сообщение


def normalize(value) -> str | None:
    if value is None:
        return None
    return WORDS.get(str(value).strip().lower())


def default(app) -> str:
    value = app.settings.get('agent', 'new_message', 'insert')
    return value if value in MODES else 'insert'


def title(mode: str) -> str:
    return ICONS.get(mode, '') + ' ' + TITLES.get(mode, mode)


def applies(channel: str, actor: str) -> bool:
    """Режим касается только реплик хозяина из его каналов; письма агентов, итоги помощников, почта — как раньше."""
    home = {'терминал': 'terminal', 'пульт': 'pult'}.get(channel, channel)
    return actor == 'owner' and home in CHANNELS


def event(state) -> asyncio.Event | None:
    ev = state.get('_steer') if isinstance(state, dict) else None
    return ev if isinstance(ev, asyncio.Event) else None


def pending(state) -> bool:
    ev = event(state)
    return bool(ev is not None and ev.is_set())


def of_session(app, session_id) -> asyncio.Event | None:
    """Событие хода этого разговора — для Предохранителя и вопросов, у которых есть только id разговора."""
    mind = getattr(app, 'mind', None)
    turn = mind.turns.get(session_id) if mind is not None and session_id else None
    return event(turn.state) if turn is not None and turn.task is not None and not turn.task.done() else None


async def wait(ev: asyncio.Event | None, awaitable, timeout: float | None = None) -> str:
    """Ждать awaitable, срока или слова хозяина: 'done' | 'timeout' | 'steer'.

    Сам awaitable не отменяется — что с ним делать после 'steer' или 'timeout', решает тул.
    Отмена внешней задачи (Стоп) доходит до него, как и раньше с wait_for."""
    fut = asyncio.ensure_future(awaitable)
    if ev is not None and ev.is_set() and not fut.done():
        return 'steer'
    waiter = asyncio.ensure_future(ev.wait()) if ev is not None else None
    try:
        done, _ = await asyncio.wait({fut} | ({waiter} if waiter else set()), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    except asyncio.CancelledError:
        fut.cancel()
        raise
    finally:
        if waiter is not None:
            waiter.cancel()
    if fut in done:
        return 'done'
    return 'steer' if ev is not None and ev.is_set() else 'timeout'


def engine_note(app) -> str:
    """Честная оговорка для движка, который ведёт свой цикл внутри одного вызова (Claude Code)."""
    try:
        chain = app.models.roles().get('main') or []
        provider = app.models.providers().get((chain[0] if chain else {}).get('provider') or '') or {}
    except Exception:  # noqa: BLE001 — подсказка не должна ломать состояние
        return ''
    if provider.get('kind') == 'claude_code':
        return 'Claude Code видит вставку на ближайшем вызове тула Бендера; пока он только пишет ответ без тулов — после ответа.'
    return ''
