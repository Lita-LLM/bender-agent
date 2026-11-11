"""Офлайн-экзамены в настоящем Chromium."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bender.core.app import App
app = App(sys.argv[1])
app.selfdir.finish_awakening()
app.settings.set("guard", {"process_count": 1024})
asyncio.run(app.close())
