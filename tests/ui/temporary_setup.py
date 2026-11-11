"""Свои контексты задач: Script у всех нужных ролей, без разбора после хода."""
import asyncio
import runpy
import sys
from pathlib import Path
runpy.run_path(str(Path(__file__).with_name('correspondence_setup.py')))
from bender.core.app import App
app=App(sys.argv[1]);app.models.set_role('coder',app.models.roles()['main']);asyncio.run(app.close())
