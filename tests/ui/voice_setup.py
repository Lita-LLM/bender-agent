"""Аудио и обычный разговор на фальшивом провайдере без оплаты."""
import asyncio,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from bender.core.app import App
app=App(sys.argv[1]);app.selfdir.finish_awakening()
p=app.models.add_provider(name='Фейк',base_url='http://127.0.0.1:'+sys.argv[2]+'/v1',extra={'is_local':True})
for role,model in (('ears','ears'),('voice','voice'),('main','echo')):app.models.set_role(role,[{'provider':p['id'],'model':model}])
app.settings.set('voice',{'enabled':True,'format':'wav','auto_speak':True})
asyncio.run(app.close())
