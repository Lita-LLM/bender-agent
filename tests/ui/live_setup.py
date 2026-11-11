"""Свои роли в пульте: локальная фальшивая модель, агент уже проснулся."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bender.core.app import App
app = App(sys.argv[1])
provider = app.models.add_provider(name="Фейк",base_url=f"http://127.0.0.1:{sys.argv[2]}/v1",extra={"is_local":True})
for role in ("main", "fast"):
    app.models.set_role(role,[{"provider":provider["id"],"model":"script"}])
app.selfdir.finish_awakening()
asyncio.run(app.close())
