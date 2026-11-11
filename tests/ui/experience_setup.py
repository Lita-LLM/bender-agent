"""Чистый навык из опыта, подготовленный тем же путём, что и после разбора."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bender.core.app import App
from bender.skills.experience import learn
from bender.tools.registry import ToolContext


async def main():
    app = App(sys.argv[1])
    app.selfdir.finish_awakening()
    provider = app.models.add_provider(name="Фейк", base_url=f"http://127.0.0.1:{sys.argv[2]}/v1", extra={"is_local": True})
    app.models.set_role("main", [{"provider": provider["id"], "model": "script"}])
    result = await learn(app.skills, {"name": "report", "description": "Проверка отчёта", "body": "1. Собери данные.\n2. Проверь суммы.\n3. Сверь итог с исходниками."}, ToolContext(app))
    app.mind._save_event(app.main_session(), "skill", "💾 Навык: report", result)
    await app.close()


asyncio.run(main())
