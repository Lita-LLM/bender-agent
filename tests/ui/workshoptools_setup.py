"""Тул-код уже проверен, хозяину предстоят просмотр и одобрение."""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bender.core.app import App
from bender.tools.registry import ToolContext
from bender.workshop.tools import propose
from tests.test_workshop_tools import CODE, TESTS


async def main():
    app = App(sys.argv[1])
    app.selfdir.finish_awakening()
    app.settings.set("guard", {"process_count": 1024})
    record = await propose(app.workshop, ToolContext(app), "sum", "Сумма чисел", CODE, TESTS, ["exec"])
    assert record["status"] == "waiting", record
    await app.close()


asyncio.run(main())
