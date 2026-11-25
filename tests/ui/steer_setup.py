"""Сообщение во время хода: фальшивая модель по сценарию, безвредная команда sleep разрешена правилом хозяина."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bender.core.app import App
app = App(sys.argv[1])
provider = app.models.add_provider(name="Фейк", base_url=f"http://127.0.0.1:{sys.argv[2]}/v1", extra={"is_local": True})
for role in ("main", "fast"):
    app.models.set_role(role, [{"provider": provider["id"], "model": "script"}])
app.selfdir.set_identity({"name": "Тин"})
app.selfdir.finish_awakening()
app.settings.set("agent", {"awakening": False})
app.settings.set("memory", {"review": False, "compact": False})
app.guard.add_rule("exec", "sleep *", "allow", note="стенд сценария steer")
asyncio.run(app.close())
