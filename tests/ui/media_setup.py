"""Фальшивая модель со зрением и пробуждённый агент для вложений."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from bender.core.app import App
app=App(sys.argv[1])
p=app.models.add_provider(name='Фейк',base_url=f'http://127.0.0.1:{sys.argv[2]}/v1',extra={'is_local':True,'models':[{'id':'script','vision':True}]})
app.models.set_role('main',[{'provider':p['id'],'model':'script'}])
app.selfdir.finish_awakening()
asyncio.run(app.close())
