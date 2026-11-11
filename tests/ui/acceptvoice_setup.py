"""Установленный тестовый пакет и потерянные ссылки у двух агентов, без загрузок."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from bender.core.app import App
app=App(sys.argv[1]);app.selfdir.finish_awakening()
p=app.models.add_provider(name='Фейк',base_url='http://127.0.0.1:'+sys.argv[2]+'/v1',extra={'is_local':True})
app.models.set_role('main',[{'provider':p['id'],'model':'echo'}])
child=app.agents.get(app.agents.create('Второй',agent_id='listener',awaken=False)['id'])
app.settings.set('voice_local',{'port':int(sys.argv[2]),'enabled':True})
folder=app.dirs.local/'voice';folder.mkdir();(folder/'installed.json').write_text('{}');(folder/'token').write_text('fixture-key')
app.models.add_provider(preset='local_voice',base_url='http://127.0.0.1:'+sys.argv[2]+'/v1',api_key='fixture-key')
for ctx in (app,child):ctx.settings.replace('roles',{**ctx.models.roles(),'ears':[{'provider':'gone','model':'old'}],'voice':[{'provider':'gone','model':'old'}]})
asyncio.run(app.close())
