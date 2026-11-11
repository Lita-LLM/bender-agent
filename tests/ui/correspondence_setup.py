"""Фальшивая модель без фонового разбора, чтобы сценарий переписки был точным."""
import asyncio
import runpy
import sys
from pathlib import Path
runpy.run_path(str(Path(__file__).with_name('agents_setup.py')))
from bender.core.app import App
app=App(sys.argv[1]);app.settings.set('memory',{'review':False,'compact':False});asyncio.run(app.close())
