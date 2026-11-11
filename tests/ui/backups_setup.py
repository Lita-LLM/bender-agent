"""Два временных диска вне живой папки данных."""
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from bender.core.app import App
import asyncio
app=App(sys.argv[1])
app.settings.set('backups',{'path':str(Path(sys.argv[1]).parent/'disk1'),'second_path':str(Path(sys.argv[1]).parent/'disk2')})
(app.dirs.work/'hello.txt').write_text('бекап')
asyncio.run(app.close())
