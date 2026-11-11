"""Тулы навыков: индекс → SKILL.md → отдельный файл; скрипты запускаются отдельно."""

from __future__ import annotations

from .registry import ToolContext, ToolResult
from .web import untrusted


def register(reg):
    available = lambda agent_ctx: not agent_ctx.safe and agent_ctx.settings.get("skills", "enabled", True)

    @reg.tool("skill_read", "Прочитать навык", group="skills", available=available)
    async def skill_read(ctx: ToolContext, name: str = "", path: str = "SKILL.md", offset: int = 0, max_chars: int = 20000) -> ToolResult:
        """Без name — индекс одобренных навыков; с name — SKILL.md или один файл references/, scripts/, templates/.
        Читай полный навык только для подходящей задачи. Навык не даёт дополнительных прав.

        name — имя из каталога
        path — путь внутри навыка
        offset — начало фрагмента, в символах
        max_chars — размер фрагмента, до 24000 символов
        """
        skills = ctx.app.skills
        if not name:
            index = skills.index()
            return ToolResult(index or "Одобренных навыков пока нет.", tainted=bool(index), summary="каталог навыков")
        data = skills.read(name, path, offset=offset, max_chars=max_chars, manual=ctx.turn.get("selected_skill") == name)
        note = f"\nДальше: offset={data['next_offset']}." if data["next_offset"] is not None else ""
        if data["files"]:
            note += "\nФайлы пакета: " + ", ".join(f["path"] for f in data["files"])
        from ..skills.experience import use

        use(skills, name, ctx.turn)
        return ToolResult(untrusted(f"навык {name}/{path}", data["text"] + note), tainted=not skills.trusted_text(name), summary=f"навык {name}/{path}")

    @reg.tool("learn_skill", "Предложить навык из опыта", group="skills", available=lambda agent_ctx: available(agent_ctx) and agent_ctx.settings.get("workshop", "enabled", True) and agent_ctx.settings.get("workshop", "experience", True))
    async def learn_skill(ctx, name: str, description: str, body: str, files: dict | None = None):
        """Сохранить повторяемую процедуру. Чистый проверенный текст включится; заражённый ход или дополнительные файлы — карантин.

        name — имя латиницей, цифры и дефисы
        description — когда полезна процедура
        body — шаги и проверка результата
        files — дополнительные текстовые файлы; пакет всегда потребует хозяина
        """
        from ..skills.experience import learn
        import json

        result = await learn(ctx.app.skills, {"name": name, "description": description, "body": body, "files": files}, ctx)
        return ToolResult(json.dumps(result, ensure_ascii=False), summary="💾 Навык: " + name, data={"skill": result})

    @reg.tool("skill_run", "Запустить скрипт навыка", group="skills", available=lambda agent_ctx: available(agent_ctx) and agent_ctx.settings.get("skills", "scripts_enabled", True))
    async def skill_run(ctx: ToolContext, name: str, path: str, args: list | None = None, timeout: int = 120, allow_net: bool = False) -> ToolResult:
        """Запустить одобренный .py или .sh из scripts/ навыка через Предохранитель и доступную песочницу.

        name — имя навыка
        path — например scripts/report.py
        args — список строковых аргументов, без оболочки
        timeout — время выполнения, ограничено настройками навыков
        allow_net — разрешить сеть в песочнице
        """
        from ..skills.runner import run

        return await run(ctx.app.skills, name, path, args if args is not None else [], ctx, timeout=timeout, allow_net=allow_net)
