"""Перенесённый Помощник для проверки предложения прав и действующего профиля."""
import asyncio
import runpy
from pathlib import Path
import sys
runpy.run_path(str(Path(__file__).with_name('agents_setup.py')))
from bender.core.app import App
app=App(sys.argv[1])
record=app.agents.create('Перенесённый',agent_id='migrated',awaken=False)
ctx=app.agents.get(record['id'])
ctx.store.meta_set('import:openclaw:last',{'source':'тестовый источник'})
ctx.models.set_role('main',app.models.roles()['main'])
ctx.selfdir.finish_awakening()
asyncio.run(app.close())
