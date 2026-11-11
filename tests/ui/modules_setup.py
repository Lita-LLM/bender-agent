"""Пульт модулей: пробуждённый агент с локальной моделью, без установленных модулей."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bender.core.app import App

app = App(sys.argv[1])
# RLIMIT_NPROC считает весь UID: Chromium запускает десятки собственных процессов.
app.settings.set("guard", {"process_count": 1024}, by="owner")
provider = app.models.add_provider(name="Фейк", base_url=f"http://127.0.0.1:{sys.argv[2]}/v1", extra={"is_local": True})
app.models.set_role("main", [{"provider": provider["id"], "model": "script"}])
app.selfdir.set_identity({"name": "Тин"})
app.selfdir.finish_awakening()
asyncio.run(app.close())
