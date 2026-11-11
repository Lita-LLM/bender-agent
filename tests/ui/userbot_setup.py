"""Данные для tests/ui/userbot.js: модель echo; сам аккаунт подключает сценарий через пульт.

python3 tests/ui/userbot_setup.py ПАПКА_ДАННЫХ ПОРТ_FAKE_LLM
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bender.core.app import App  # noqa: E402


def main(data: str, port: int) -> None:
    app = App(data)
    p = app.models.add_provider(name="Фейк", kind="openai", base_url=f"http://127.0.0.1:{port}/v1", extra={"is_local": True})
    app.models.set_role("main", [{"provider": p["id"], "model": "echo"}])
    app.selfdir.set_identity({"name": "Тин", "emoji": "🤖"})
    app.selfdir.finish_awakening()
    asyncio.run(app.close())


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))
