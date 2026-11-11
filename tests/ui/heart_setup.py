"""Данные для tests/ui/heart.js: задача из OpenClaw (выключенная), намерение, день для сна, сценарий сновидения.

python3 tests/ui/heart_setup.py ПАПКА_ДАННЫХ ПОРТ_FAKE_LLM
"""

import asyncio
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bender.core.app import App  # noqa: E402
from bender.heart.adopt import adopt  # noqa: E402
from bender.memory import selfdir  # noqa: E402


def main(data: str, port: int) -> None:
    app = App(data)
    base = f"http://127.0.0.1:{port}"
    p = app.models.add_provider(name="Фейк", kind="openai", base_url=base + "/v1", extra={"is_local": True})
    app.models.set_role("main", [{"provider": p["id"], "model": "echo"}])
    app.models.set_role("dreamer", [{"provider": p["id"], "model": "script"}])
    app.settings.set("heart", {"quiet_hours": ""})
    sd = app.selfdir
    sd.set_identity({"name": "Тин", "emoji": "🤖"})
    sd.finish_awakening()
    app.store.meta_set("import:openclaw:cron", [{"id": "oc1", "name": "Утренняя сводка", "enabled": True, "job": {
        "schedule": {"kind": "cron", "expr": "0 9 * * *"}, "sessionTarget": "isolated", "payload": {"kind": "agentTurn", "message": "Сводка: почта, погода"}}}])
    adopt(app)
    sid = app.main_session()
    app.store.add_message(sid, {"role": "user", "content": "Я переехал в Казань, теперь работаю из дома"}, {"actor": "owner"})
    app.store.add_message(sid, {"role": "assistant", "content": "Запомню: Казань, работа из дома."})
    selfdir.append_diary(app.dirs.diary, "Хозяин переехал в Казань")
    asyncio.run(app.close())
    script = [{"text": json.dumps({"themes": ["переезд"], "about_owner": ["живёт в Казани"], "promote": [{"text": "Хозяин живёт в Казани и работает из дома", "to": "memory"}],
                                   "story": "Мне снилась Волга и бесконечный мост из задач."}, ensure_ascii=False)}]
    req = urllib.request.Request(base + "/fake/script", data=json.dumps(script).encode(), headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req).read()


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))
