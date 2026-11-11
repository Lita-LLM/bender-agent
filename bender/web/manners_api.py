"""Пульт → Я → Манеры: библиотека хозяина и выбор манеры текущего агента."""

from __future__ import annotations


def routes(r, app, J, body) -> None:
    from ..core.errors import HumanError
    from ..mind import manners
    from ..tools.registry import ToolContext

    def owner():
        return ToolContext(app, actor="owner", session_id="manners-panel")

    @r.route("GET", "/api/manners")
    @J
    async def listing(req):
        return manners.public(app)

    @r.route("POST", "/api/manners")
    @J
    async def create(req):
        b = body(req)
        await owner().check("settings", "manners", "добавить манеру речи")
        mid = manners.save(app.settings, None, b.get("name", ""), b.get("description", ""), b.get("text", ""))
        app.journal.write("settings", f"Хозяин добавил манеру «{b.get('name', '')}»", actor="owner", data={"manner": mid})
        return {"id": mid, **manners.public(app)}

    @r.route("PUT", "/api/manners/{mid}")
    @J
    async def update(req):
        b, mid = body(req), req.params["mid"]
        if not manners.get(app.settings, mid):
            raise HumanError("not_found", "Такой манеры нет", "Обнови страницу.", status=404)
        await owner().check("settings", "manners", "поправить манеру речи")
        manners.save(app.settings, mid, b.get("name", ""), b.get("description", ""), b.get("text", ""))
        app.journal.write("settings", f"Хозяин поправил манеру «{b.get('name', '')}»", actor="owner", data={"manner": mid})
        return manners.public(app)

    @r.route("DELETE", "/api/manners/{mid}")
    @J
    async def remove(req):
        mid = req.params["mid"]
        await owner().check("settings", "manners", "удалить манеру речи")
        manners.delete(app.settings, mid)
        app.journal.write("settings", f"Хозяин убрал манеру {mid}", actor="owner", data={"manner": mid})
        return manners.public(app)

    @r.route("POST", "/api/manners/{mid}/restore")
    @J
    async def restore(req):
        mid = req.params["mid"]
        await owner().check("settings", "manners", "вернуть заготовку манеры")
        manners.restore(app.settings, mid)
        return manners.public(app)

    @r.route("POST", "/api/manner")
    @J
    async def choose(req):
        """Выбор манеры текущего агента: {"active": id | ""}."""
        mid = str(body(req).get("active") or "")
        await owner().check("manner", mid or "off", "выбрать манеру речи агента")
        manners.choose(app, mid, actor="owner")
        return manners.public(app)

    @r.route("GET", "/api/manners/{mid}/preview")
    @J
    async def preview(req):
        """Как раздел выглядит в системном промпте — ровно то, что получит модель."""
        item = manners.get(app.settings, req.params["mid"])
        if not item:
            raise HumanError("not_found", "Такой манеры нет", "Обнови страницу.", status=404)
        from ..mind.context import estimate_tokens
        text = manners.section(item)
        return {"text": text, "tokens": estimate_tokens(text)}
