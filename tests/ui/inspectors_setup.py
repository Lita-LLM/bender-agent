"""Память с провенансом и настоящим событием recall, пример истории удалённой функции."""
import asyncio,sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from bender.core.app import App
from bender.mind.loop import Turn
from bender.tools.registry import ToolContext
async def main():
 app=App(sys.argv[1]);app.selfdir.finish_awakening();sid=app.main_session()
 app.store.add_message(sid,{'role':'user','content':'Расписание проверок'},{'actor':'owner','channel':'pult'})
 first=app.store.messages(sid)[0]['id']
 app.store.add_call(session_id=sid,role='main',provider='Фикстура',model='пример',usage={},cost=0,seconds=0,notice='Пример',request={'messages':[{'role':'user','content':'Расписание проверок'}]})
 (app.dirs.diary/'2026-10-22.md').write_text('# Проверки\n\n- Расписание: проверять мосты по пятницам.\n')
 app.index.sync(None);app.mind.turns[sid]=Turn(sid,'pult','owner',False,first_id=first)
 await app.tools.run('recall',{'query':'расписание'},ToolContext(app,session_id=sid))
 app.journal.write('decision','Проверка фильтра',actor='agent',data={'capability':'fs.write','verdict':'deny','channel':'pult','session':sid,'first':first})
 app.workshop.save({'id':'p-history','kind':'core','status':'rejected','name':'Прежняя правка','diff':' context\n-'+('очень длинная старая строка '*30)+'\n+новое\n','description':'История до решения хозяина'})
 await app.close()
asyncio.run(main())
