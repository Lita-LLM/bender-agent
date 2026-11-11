"""Неизвестные возможности и тестовый Piper; реальных загрузок нет."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from bender.core.app import App
app=App(sys.argv[1]);app.selfdir.finish_awakening()
p=app.models.add_provider(name='Проверка медиа',base_url='http://127.0.0.1:'+sys.argv[2]+'/v1',extra={'is_local':True,'models':[{'id':'echo'}]})
app.models.set_role('main',[{'provider':p['id'],'model':'echo'}])
app.models.set_capability(p['id'],'echo','vision',False,source='rejected',reason='Image input is not supported (тестовая запись).')
app.settings.set('voice_local',{'enabled':True,'port':int(sys.argv[2])})
folder=app.dirs.local/'voice';folder.mkdir();(folder/'installed.json').write_text('{}');(folder/'token').write_text('fixture-key')
asyncio.run(app.close())
