"""Отдельный stdio MCP в Chromium: NPROC считает весь UID, включая браузер."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bender.core.app import App
app = App(sys.argv[1])
app.settings.set("guard", {"process_count":1024}, by="owner")
asyncio.run(app.close())
