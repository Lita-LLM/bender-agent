"""API стаи: два агента с локальной эхо-моделью и обычным глобальным Предохранителем."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from bender.core.app import App
app=App(sys.argv[1]);app.selfdir.set_identity({'name':'Тин'});app.selfdir.finish_awakening()
provider=app.models.add_provider(name='Фальшивая',kind='openai',base_url=f'http://127.0.0.1:{sys.argv[2]}/v1',extra={'is_local':True})
app.models.set_role('main',[{'provider':provider['id'],'model':'echo'}])
app.settings.set('agent',{'awakening':False},by='owner');app.agents.create('Второй',agent_id='second',awaken=False)
asyncio.run(app.close())
