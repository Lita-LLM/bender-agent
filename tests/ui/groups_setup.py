"""Группа хозяина с непрозрачными правами из обновления Telegram."""
from rich_setup import app as prepared
from bender.core.app import App
import asyncio
app=App(prepared.dirs.root)
app.telegram.groups.record({'id':-100001,'type':'supergroup','title':'Рабочая группа'}, {'id':42,'first_name':'Хозяин'}, {'status':'member','can_send_messages':True},date=1700000000)
asyncio.run(app.close())
