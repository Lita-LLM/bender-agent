"""Выражение лица (превью): агент сам выбирает, как выглядеть на пару секунд. Системные режимы (стоп, одобрение, сон, ошибка) главнее."""
from __future__ import annotations

from .registry import ToolContext, ToolResult

MOODS = {'joy': 'радость', 'doubt': 'сомнение', 'smirk': 'ехидство', 'surprise': 'удивление', 'sad': 'грусть'}


def register(reg) -> None:
    @reg.tool('express', 'Выражение лица', group='self')
    async def express(ctx: ToolContext, mood: str, seconds: int = 8) -> ToolResult:
        """Показать выражение на визоре в пульте: радость, сомнение, ехидство, удивление, грусть. Косметика: ничего не делает кроме картинки.

        mood — joy, doubt, smirk, surprise или sad
        seconds — на сколько секунд (1–30)
        """
        if mood not in MOODS:
            return ToolResult('Выражения: ' + ', '.join(f'{k} ({v})' for k, v in MOODS.items()) + '.', ok=False, summary='нет такого выражения')
        if not ctx.app.settings.get('appearance', 'expressions', True):
            return ToolResult('Выражения лица выключены хозяином (Настройки → Лицо и тема).', ok=False, summary='выключено')
        ctx.app.bus.publish({'type': 'expression', 'mood': mood, 'seconds': max(1, min(30, int(seconds or 8)))})
        return ToolResult('Показал: ' + MOODS[mood] + '.', summary=MOODS[mood])
