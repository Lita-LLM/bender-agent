"""Матрица: пробуждённый агент и локальная эхо-модель, модули ещё не одобрены."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from bender.core.app import App
app=App(sys.argv[1])
app.settings.set('guard',{'process_count':1024},by='owner')
provider=app.models.add_provider(name='Фальшивая',kind='openai',base_url=f'http://127.0.0.1:{sys.argv[2]}/v1',extra={'is_local':True})
app.models.set_role('main',[{'provider':provider['id'],'model':'echo'}])
app.selfdir.set_identity({'name':'Тин'});app.selfdir.finish_awakening()
app.settings.set('agent',{'awakening':False},by='owner')
app.store.meta_set('import:openclaw:channels',[{'matrix':{'homeserver':'https://imported.example.org','rooms':{'!old:example.org':{}},'encryption':True,'accessToken':'НЕ_ПОКАЗЫВАТЬ_В_ПРЕДЛОЖЕНИИ'}}])
asyncio.run(app.close())
