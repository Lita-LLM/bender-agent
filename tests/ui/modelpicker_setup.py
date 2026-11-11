"""Большой каталог и провайдер без каталога для телефонного выбора моделей."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bender.core.app import App
app=App(sys.argv[1])
p=app.models.add_provider(name='Каталог',base_url=f'http://127.0.0.1:{sys.argv[2]}/v1',extra={'is_local':True,'models':[{'id':f'model-{i:02d}','context':32000,'price':{'in':0,'out':0}} for i in range(20)]})
app.models.add_provider(name='Без каталога',base_url=f'http://127.0.0.1:{sys.argv[2]}/v1')
app.models.set_role('main',[{'provider':p['id'],'model':'model-00'}])
app.selfdir.finish_awakening()
asyncio.run(app.close())
