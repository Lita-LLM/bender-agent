"""Слоты в браузере: собственная неподписанная разработка и короткий срок проверки."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from bender.core.app import App
app=App(sys.argv[1])
app.settings.set("updates",{"allow_development":True,"health_seconds":3})
asyncio.run(app.close())
