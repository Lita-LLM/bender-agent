"""Тестовая модель для UI навыков; пользовательских ключей и внешней сети нет."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bender.core.app import App

app = App(sys.argv[1])
# RLIMIT_NPROC считает весь UID, включая процессы Chromium.
app.settings.set("guard", {"process_count": 1024}, by="owner")
provider = app.models.add_provider(name="Фейк", kind="openai", base_url=f"http://127.0.0.1:{sys.argv[2]}/v1", extra={"is_local": True})
app.models.set_role("main", [{"provider": provider["id"], "model": "script"}])
app.selfdir.set_identity({"name": "Тест", "emoji": "🤖"})
app.selfdir.finish_awakening()
asyncio.run(app.close())
