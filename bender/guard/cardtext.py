"""Что именно одобряет хозяин в карточке вне пульта (Telegram, юзербот, терминал).

Сводка тула бывает обрезана («выполнить: …» до 200 знаков), а аргументы MCP лежат только в detail:
хозяин на телефоне одобрял не то, что видел (охота 19.11). Здесь собирается полный текст действия;
если он не влезает, карточка говорит об этом прямо, а кнопка «Да» остаётся только в пульте.
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
            parts.append('Аргументы: ' + visible(raw))
            if len(raw) >= 2000:
                parts.append('')  # аргументы уже урезаны при записи — полностью только в пульте
                card = {**card, 'target': 'x' * 2000}
    text = '\n'.join(p for p in parts if p)
    truncated = len(text) > limit or len(str(card.get('target') or '')) >= 2000
    if len(text) > limit:
        text = text[:limit] + '…'
    return text, truncated
