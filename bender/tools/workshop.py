"""Мастерская: агент предлагает и проверяет, хозяин применяет."""
import json
from .registry import ToolResult


def register(reg):
    available = lambda agent_ctx: not agent_ctx.safe and agent_ctx.settings.get("workshop", "enabled", True)

    @reg.tool("run_exams", "Экзамены Бендера", group="workshop", available=available)
    async def run_exams(ctx):
        """Проверить отдельную копию программы без сети и настоящих данных. Результат выставляет внешний проверяющий."""
        report = await ctx.app.workshop.run_exams(ctx)
        return ToolResult(json.dumps(report, ensure_ascii=False), ok=report["ok"], summary="экзамены пройдены" if report["ok"] else "экзамены провалены", data=report)

    @reg.tool("propose_tool", "Предложить новый тул", group="workshop", available=lambda agent_ctx: available(agent_ctx) and agent_ctx.settings.get("workshop", "tools_enabled", True))
    async def propose_tool(ctx, name: str, description: str, code: str, tests: str, capabilities: list[str], network_hosts: list[str] | None = None):
        """Предложить локальный MCP-сервер на Python и unittest. Код проверяется изолированно; применяет только хозяин.

        name — короткое имя латиницей
        description — задача тула
        code — полный код server.py, MCP через stdin/stdout, без сторонних библиотек
        tests — полный test_tool.py, импорт сервера как import server
        capabilities — exec (внутри изоляции), при необходимости fs.read и fs.write рабочей папки; net.read через посредник на одобренные домены
        network_hosts — точные домены для net.read, без пути/порта/звёздочек; HTTPS может отправлять данные домену
        """
        from ..workshop.tools import propose

        record = await propose(ctx.app.workshop, ctx, name, description, code, tests, capabilities, network_hosts)
        return ToolResult(json.dumps(record, ensure_ascii=False), ok=record["status"] == "waiting", tainted=True, summary="Тул предложен: " + name, data={"proposal": record["id"]})
