"""Данные для tests/ui/load.js: агент стаи для выбора заведующим, частый замер и отчёт о необычной нагрузке на диске.

python3 tests/ui/load_setup.py ПАПКА_ДАННЫХ ПОРТ_FAKE_LLM
"""

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bender.core.app import App  # noqa: E402
from bender.core.loadwatch import TITLES  # noqa: E402


async def main(data: str, port: int) -> None:
    app = App(data)
    p = app.models.add_provider(name="Фейк", kind="openai", base_url=f"http://127.0.0.1:{port}/v1", extra={"is_local": True})
    app.models.set_role("main", [{"provider": p["id"], "model": "echo"}])
    app.selfdir.set_identity({"name": "Тин", "emoji": "🤖"})
    app.selfdir.finish_awakening()
    app.agents.create("Завхоз", agent_id="zavhoz", awaken=False)
    app.settings.set("load", {"sample_seconds": 5})
    watch = app.load_watch
    now = time.time()
    anomaly = {"id": time.strftime("%Y%m%d-%H%M%S", time.localtime(now - 3600)) + "-node", "subject": "node", "title": TITLES["node"], "started": now - 3600,
               "reason": "узел занят на 99% дольше 5 мин", "critical": False, "numbers": {"t": now - 3600, "own": 4.0, "sys": 99.0, "load1": 8.2, "mem": 41.0}}
    snap = {"seconds": 2.0, "own": 4.0, "threads": [], "tasks": [], "system": {"busy": 99.0, "iowait": 0.0, "steal": 0.0},
            "top_cpu": [{"pid": 4242, "comm": "stress", "cpu_pct": 396.0, "rss_mb": 12.0, "bender": "", "command": "stress --cpu 4"}],
            "top_rss": [], "processes": 120}
    watch.save(watch.report(anomaly, snap, {}))
    await app.close()


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1], int(sys.argv[2])))
