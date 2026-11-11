"""Ошибки, понятные человеку.

Каждая ошибка отвечает на два вопроса: что случилось и что с этим делать.
Сырые подробности (ответ сервера, трассировка) лежат в detail и показываются
под спойлером.
"""

from __future__ import annotations

import traceback


class HumanError(Exception):
    def __init__(
        self,
        code: str,
        title: str,
        hint: str = "",
        *,
        detail: str | None = None,
        status: int = 400,
        data: dict | None = None,
    ):
        super().__init__(f"{title}. {hint}".strip())
        self.code = code
        self.title = title
        self.hint = hint
        self.detail = detail
        self.status = status
        self.data = data or {}

    def to_dict(self) -> dict:
        out = {"code": self.code, "title": self.title, "hint": self.hint}
        if self.detail:
            out["detail"] = self.detail
        if self.data:
            out["data"] = self.data
        return out


def unexpected(exc: BaseException, doing: str) -> HumanError:
    """Неожиданная ошибка в нашем коде: честно говорим, что это баг, и прикладываем трассировку."""
    return HumanError(
        "internal",
        f"Внутренняя ошибка: {doing}",
        "Это ошибка в программе, а не в твоих действиях. Данные не повреждены: запись идёт атомарно. "
        "Скопируй подробности ниже и пришли разработчику.",
        detail="".join(traceback.format_exception(type(exc), exc, exc.__traceback__))[-6000:],
        status=500,
    )
