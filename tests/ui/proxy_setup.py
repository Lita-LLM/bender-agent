"""Карточка доменов проверена офлайн, сетевой выключатель пока закрыт."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bender.core.app import App
from bender.tools.registry import ToolContext
from bender.workshop.tools import propose
from tests.test_workshop_tools import CODE, TESTS


async def main():
    app=App(sys.argv[1]);app.selfdir.finish_awakening()
    record=await propose(app.workshop,ToolContext(app),'net-preview','Пример одобренных доменов',CODE,TESTS,['exec','net.read'],['example.org'])
    assert record['status']=='waiting',record
    await app.close()


asyncio.run(main())
