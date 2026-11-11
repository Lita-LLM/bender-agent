"""Подготовка UI поиска по пульту: агент уже проснулся, есть запись в памяти."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bender.core.app import App

app = App(sys.argv[1])
app.selfdir.set_identity({"name": "Тин"})
app.selfdir.finish_awakening()
app.facts.add("хозяин", "город", "Казань", single=True, provenance="owner")
asyncio.run(app.close())
