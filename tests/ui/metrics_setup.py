"""Три дня наблюдаемых исходов, расходы, навыки и экзамены для графиков."""
import asyncio
import sys
import time
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bender.core.app import App
from bender.skills.experience import update
app = App(sys.argv[1])
app.selfdir.finish_awakening()
for index, ok in enumerate((False, True, True)):
    stamp = time.time() - (2-index)*86400
    with patch("bender.guard.journal.time.time", return_value=stamp):
        app.journal.write("turn", "Исход фикстуры", actor="owner", data={"ok": ok, "cost": .03 / (index + 1)})
assert app.journal.verify()["ok"]
app.store.add_message(app.main_session(), {"role": "user", "content": "Не так, исправь расчёт"}, {"actor": "owner"})
app.store.add_call(session_id=None, role="main", provider="Фикстура", model="пример", usage={}, cost=.055, seconds=0, notice="Пример расходов", request={})
update(app.skills, "report", uses=7, helped=6, failed=1)
app.store.meta_set("workshop.exams", [{"id": f"exam-example-{i}", "at": time.time()-(2-i)*86400, "ok": ok, "seconds": 2, "results": [], "candidate_hash": "пример"} for i,ok in enumerate((False,True,True))])
asyncio.run(app.close())
