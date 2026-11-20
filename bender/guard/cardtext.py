"""Что именно одобряет хозяин в карточке вне пульта (Telegram, юзербот, терминал).

Сводка тула бывает обрезана («выполнить: …» до 200 знаков), а аргументы MCP лежат только в detail:
хозяин на телефоне одобрял не то, что видел (охота 19.11). Здесь собирается полный текст действия;
если он не влезает в сообщение, Предохранитель кладёт полный текст в card['full'], а канал отправляет его
файлом рядом с карточкой — одобрить можно и с телефона (охота 20.11: «иллюзия выбора» с одной кнопкой «Нет»).
"""
from __future__ import annotations

import json
import re
import unicodedata

LIMIT = 3000
_HIDDEN = re.compile('[​-‏‪-‮⁠-⁩﻿]')


def visible(text) -> str:
    """Убрать управляющие и bidi-символы, которыми можно спрятать часть команды."""
    text = _HIDDEN.sub('', str(text or ''))
    return ''.join(ch if ch in '\n\t' or unicodedata.category(ch)[0] != 'C' else '·' for ch in text)


def action_text(card: dict, limit: int = LIMIT) -> tuple[str, bool]:
    """(текст, обрезано ли). Сводка, затем полная цель, если сводка её не содержит, затем аргументы внешнего тула."""
    summary = visible(card.get('summary') or card.get('what') or '')
    target = visible(card.get('target') or '')
    parts = [summary]
    if target and target not in ('*',) and target not in summary:
        parts.append('Целиком: ' + target)
    detail = card.get('detail') or {}
    cap = str(card.get('capability') or '')
    if cap.startswith('mcp.') or cap == 'skill.run':
        args = detail.get('arguments', detail.get('args'))
        if args is not None:
            raw = args if isinstance(args, str) else json.dumps(args, ensure_ascii=False)
            parts.append('Аргументы: ' + visible(raw) + (' …[обрезано при записи]' if detail.get('arguments_cut') else ''))
    if isinstance(detail.get('text'), str) and detail['text']:
        parts.append('Текст: ' + visible(detail['text']) + (' (и отправить)' if detail.get('submit') else ''))  # browser_type (охота 19.11)
    text = '\n'.join(p for p in parts if p)
    truncated = len(text) > limit or bool(card.get('full'))
    if len(text) > limit:
        text = text[:limit] + '…'
    return text, truncated


def full_text(card: dict, target: str) -> str:
    """Полный текст действия для файла рядом с карточкой; '' — если он и так влезает в сообщение."""
    text, cut = action_text({**card, 'target': target, 'full': None}, limit=10**9)
    return text if len(text) > LIMIT else ''
