"""Два длинных собственных разговора, настоящий импорт OpenClaw и фальшивый Telegram."""
import asyncio
import shutil
import sys
from pathlib import Path
from rich_setup import app as prepared
from bender.core.app import App
from bender.importers import openclaw

app=App(sys.argv[1]);pid=app.models.roles()['main'][0]['provider']
app.models.set_role('main',[{'provider':pid,'model':'echo'}]);app.models.set_role('fast',[{'provider':pid,'model':'script'}])
app.settings.set('agent',{'history_tokens':1200});app.settings.set('memory',{'review':False})
app.settings.set('telegram',{'command_languages':['ru','en']})
def fill(ctx):
    sid=ctx.main_session()
    for i in range(25):
        ctx.store.add_message(sid,{'role':'user','content':'Архивный вопрос '+str(i)+': '+'Расскажи про настройку Бендера. '*12})
        ctx.store.add_message(sid,{'role':'assistant','content':'Архивный ответ '+str(i)+': '+'Храним историю целиком и ищем по словам. '*12})
fill(app)
child=app.agents.get(app.agents.create('Архивист',agent_id='archive2',awaken=False)['id']);fill(child)
fixture=Path(__file__).resolve().parents[1]/'fixtures'/'openclaw-2026.10.5'/'.openclaw'
source=Path(sys.argv[1]).parent/'openclaw';shutil.copytree(fixture,source)
plan=openclaw.make_plan(app,str(source));openclaw.apply(plan,app,only=[item.id for item in plan.items if item.kind=='session' and item.selected])
asyncio.run(app.close())
