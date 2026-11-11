"""Подготовка UI поиска: только локальные поставщики, без настоящих ключей."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bender.core.app import App

app = App(sys.argv[1])
app.selfdir.set_identity({"name": "Тин"})
app.selfdir.finish_awakening()
app.settings.set("search", {p + "_url": f"http://127.0.0.1:{sys.argv[2]}/fake/search/{p}" for p in ("duckduckgo", "searxng", "brave", "tavily")})
app.guard.add_rule("net.read", "*", "allow")
asyncio.run(app.close())
