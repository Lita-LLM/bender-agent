"""Модели разных форматов отвечают на локальном стенде, ключей нет."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from bender.core.app import App

app=App(sys.argv[1]);app.selfdir.set_identity({'name':'Тин'});app.selfdir.finish_awakening()
base=f'http://127.0.0.1:{sys.argv[2]}'
claude=app.models.add_provider(name='Claude на стенде',kind='anthropic',base_url=base+'/v1')
app.models.set_role('main',[{'provider':claude['id'],'model':'claude-opus-4-7','params':{'future_field':7}}])
app.settings.set('agent',{'awakening':False})
app.settings.set('memory',{'review':False})
app.agents.create('Второй',agent_id='second',awaken=False)
asyncio.run(app.close())
