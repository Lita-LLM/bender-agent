"""Описание одобренных модулей и исходящие каналы через MCP/Предохранитель."""
import json

from .registry import ToolResult
from .web import untrusted


def register(reg):
    available = lambda agent_ctx: not agent_ctx.safe and agent_ctx.settings.get("modules", "enabled", True)

    @reg.tool("module_info", "Модули и их каналы", group="modules", available=available)
    async def module_info(ctx):
        """Показать только включённые одобренные модули и имена каналов; код пакета здесь не раскрывается."""
        rows = [{"name": item["id"], "description": item["metadata"].get("description", ""), "channels": item["metadata"].get("channels", [])}
                for item in ctx.app.modules.public()["modules"] if item.get("enabled")]
        return ToolResult(untrusted("каталог модулей", ctx.app.guard.scrub(json.dumps(rows, ensure_ascii=False))), tainted=True, summary="каталог модулей")

    @reg.tool("module_send", "Отправить в канал модуля", group="modules", available=available)
    async def module_send(ctx, name: str, channel: str, target: str, text: str):
        """Отправить текст через объявленный канал модуля; msg.send и MCP проходят Предохранитель.

        name — имя включённого модуля
        channel — имя канала из module_info
        target — адресат, принятый этим каналом
        text — текст сообщения
        """
        return await ctx.app.modules.send(name, channel, target, text, ctx)
