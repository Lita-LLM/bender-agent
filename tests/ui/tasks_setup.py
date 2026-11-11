"""Данные для tests/ui/tasks.js: задачи субагентов — готовая со схемой, вложенная, с ошибкой; модель для живой задачи.

python3 tests/ui/tasks_setup.py ПАПКА_ДАННЫХ ПОРТ_FAKE_LLM
"""

import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bender.core.app import App  # noqa: E402
from bender.core.ids import new_id  # noqa: E402


def main(data: str, port: int) -> None:
    app = App(data)
    base = f"http://127.0.0.1:{port}"
    p = app.models.add_provider(name="Фейк", kind="openai", base_url=base + "/v1", extra={"is_local": True})
    app.models.set_role("main", [{"provider": p["id"], "model": "echo"}])
    app.selfdir.set_identity({"name": "Тин", "emoji": "🤖"})
    app.selfdir.finish_awakening()
    sid = app.main_session()
    now = time.time()
    top, kid, bad = new_id(), new_id(), new_id()
    rows = [
        (top, None, "researcher", "Сравни три статьи про лёгких агентов", "Статьи: …", None, "fast", "done", "Вывод: самый лёгкий — Бендер.", None, 1, 0.0123, 7),
        (kid, top, "reader", "Спам ли письмо?", "Купите слона", json.dumps({"type": "object", "properties": {"spam": {"type": "boolean"}}}), "fast", "done", '{"spam": true}',
         json.dumps({"spam": True}), 2, 0.0004, 1),
        (bad, None, "coder", "Почини сборку", "", None, "coder", "error", "", None, 1, 0.0, 2),
    ]
    for tid, ptask, role, goal, ctx, schema, model, status, result, data_, depth, cost, steps in rows:
        app.store.x("""INSERT INTO tasks(id, parent_session, parent_task, role, goal, context, schema, model_role, depth, status, result, data, schema_valid, cost, steps,
                       created, started, finished, error) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (tid, sid, ptask, role, goal, ctx, schema, model, depth, status, result, data_, 1 if schema else None, cost, steps, now - 300, now - 290, now - 200,
                     "Модель не ответила вовремя" if status == "error" else None))
    asyncio.run(app.close())


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))
