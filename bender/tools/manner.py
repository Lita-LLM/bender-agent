"""Тул manner: агент сам видит и переключает свою манеру речи (через Предохранитель)."""

from __future__ import annotations

from .registry import ToolContext, ToolResult


def register(reg) -> None:
    @reg.tool("manner", "Манера речи", group="self")
    async def manner(ctx: ToolContext, action: str = "list", name: str = "", reason: str = "") -> ToolResult:
        """Посмотреть или сменить свою манеру речи — стиль из библиотеки хозяина (ты остаёшься собой, меняются слова). Тексты манер пишет только хозяин.

        action — list: библиотека и текущая; show: полный текст манеры name; use: включить манеру name; off: говорить своим голосом из SOUL.md
        name — id или название манеры (для show и use)
        reason — почему меняешь, одной фразой (хозяин увидит в карточке)
        """
        from ..mind import manners

        agent_ctx = ctx.app
        if action not in ("list", "show", "use", "off"):
            return ToolResult("Ошибка: action — list, show, use или off.", ok=False, summary="манера: неверное действие")
        if action == "list":
            cur = manners.active(agent_ctx)
            rows = [("→ " if cur and m["id"] == cur["id"] else "• ") + f"{m['name']} ({m['id']})" + (f" — {m['description']}" if m["description"] else "")
                    for m in manners.library(agent_ctx.settings)]
            head = "Сейчас: " + (f"«{cur['name']}»" if cur else "свой голос (SOUL.md)")
            if not agent_ctx.settings.get("manner", "enabled", True):
                head += ". Манеры выключены хозяином — переключение не подействует, пока он их не включит."
            return ToolResult(head + "\n" + ("\n".join(rows) or "Библиотека пуста."), summary="манеры речи")
        if action == "show":
            item = manners.resolve(agent_ctx.settings, name)
            return ToolResult(f"«{item['name']}» ({item['id']})\n{item['description']}\n\n{item['text']}", summary=f"манера «{item['name']}»")
        if not agent_ctx.settings.get("manner", "enabled", True):
            return ToolResult("Манеры выключены хозяином (Настройки → Манера речи). Попроси его включить.", ok=False, summary="манеры выключены")
        item = manners.resolve(agent_ctx.settings, name) if action == "use" else None
        cur = manners.active(agent_ctx)
        if (cur["id"] if cur else "") == (item["id"] if item else ""):
            return ToolResult("Уже так: " + (f"«{item['name']}»" if item else "свой голос") + ".", summary="манера без изменений")
        target = item["id"] if item else "off"
        why = (" — " + reason.strip().replace("\n", " ")[:200]) if reason.strip() else ""
        await ctx.check("manner", target, "сменить манеру речи: " + (f"«{cur['name']}»" if cur else "свой голос") + " → " + (f"«{item['name']}»" if item else "свой голос") + why)
        manners.choose(agent_ctx, item["id"] if item else "", actor=ctx.actor)
        text = (f"Манера «{item['name']}» включена: с этого шага говори так." if item else "Манера выключена: говори своим голосом из SOUL.md.")
        return ToolResult(text, summary="🎭 " + (f"манера «{item['name']}»" if item else "свой голос"))
