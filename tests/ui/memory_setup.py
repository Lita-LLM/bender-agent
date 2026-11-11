"""Данные для tests/ui/memory.js: память, факты, дневник, длинный разговор и сценарий пересказа у фальшивой модели.

python3 tests/ui/memory_setup.py ПАПКА_ДАННЫХ ПОРТ_FAKE_LLM
"""

import asyncio
import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bender.core.app import App  # noqa: E402
from bender.memory import selfdir  # noqa: E402


def main(data: str, port: int) -> None:
    app = App(data)
    base = f"http://127.0.0.1:{port}"
    p = app.models.add_provider(name="Фейк", kind="openai", base_url=base + "/v1", extra={"is_local": True})
    app.models.set_role("main", [{"provider": p["id"], "model": "echo"}])
    app.models.set_role("fast", [{"provider": p["id"], "model": "script"}])
    app.settings.set("agent", {"history_tokens": 1500})
    sd = app.selfdir
    sd.set_identity({"name": "Тин", "emoji": "🤖"})
    sd.finish_awakening()
    sd.write("MEMORY.md", sd.read("MEMORY.md").rstrip() + "\n\n- Хозяин живёт в Казани с весны\n- Проект Бендер — лёгкий агент без зависимостей\n")
    sd.write("USER.md", selfdir.add_entry(sd.read("USER.md"), "Всегда отвечай коротко", directive=True))
    app.facts.add("хозяин", "город", "Казань", single=True, provenance="owner")
    selfdir.append_diary(app.dirs.diary, "Переехал в новый дом — так сказал хозяин")
    sid = app.main_session()
    for i in range(25):
        app.store.add_message(sid, {"role": "user", "content": f"вопрос {i}: расскажи про проект Бендер подробно и обстоятельно"})
        app.store.add_message(sid, {"role": "assistant", "content": f"ответ {i}: Бендер — лёгкий агент на чистом Питоне, без зависимостей"})
    asyncio.run(app.close())
    script = [{"text": json.dumps({"summary": "- Хозяин долго расспрашивал про **Бендер**\n- Я объяснял: лёгкий агент на чистом Питоне",
                                   "diary": "Обсуждали устройство Бендера"}, ensure_ascii=False)}]
    req = urllib.request.Request(base + "/fake/script", data=json.dumps(script).encode(), headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req).read()


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))
