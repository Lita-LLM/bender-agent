"""Поддельный подключённый Telegram и script-модель для проверки Rich."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from bender.core.app import App
from bender.channels.telegram import OWNER_META,TOKEN_SECRET
app=App(sys.argv[1])
p=app.models.add_provider(name='Фейк',base_url=f'http://127.0.0.1:{sys.argv[2]}/v1',extra={'is_local':True})
app.models.set_role('main',[{'provider':p['id'],'model':'script'}])
app.secrets.set(TOKEN_SECRET,'123:FAKE')
app.store.meta_set(OWNER_META,{'user_id':42,'chat_id':42,'name':'Хозяин'})
app.settings.set('telegram',{'enabled':True,'api_base':f'http://127.0.0.1:{sys.argv[2]}/tg'})
app.selfdir.finish_awakening()
asyncio.run(app.close())
