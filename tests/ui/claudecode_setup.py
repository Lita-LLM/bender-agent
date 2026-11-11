"""Данные для tests/ui/claudecode.js: пробуждённый агент без моделей и фальшивый `claude` в bin/ рядом с папкой данных
(run.sh добавляет её в PATH). Фальшивый claude вызывает тул Бендера через петлевой MCP и отвечает текстом.

python3 tests/ui/claudecode_setup.py ПАПКА_ДАННЫХ ПОРТ_FAKE_LLM
"""

import asyncio
import json
import os
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from bender.core.app import App  # noqa: E402


def main(data: str, port: int) -> None:
    app = App(data)  # моделей нет: первый запуск, Claude Code подключается из пульта
    app.selfdir.set_identity({"name": "Тин", "emoji": "🤖"})
    app.selfdir.finish_awakening()
    asyncio.run(app.close())
    plan = {"tools": [{"name": "write_file", "arguments": {"path": "из-claude.txt", "content": "Привет из Claude Code"}}],
            "text": "Записал файл через руки Бендера."}
    bindir = Path(data).resolve().parent / "bin"
    bindir.mkdir(parents=True, exist_ok=True)
    wrapper = bindir / "claude"
    wrapper.write_text("#!/bin/sh\nFAKE_CLAUDE=" + shlex.quote(json.dumps(plan, ensure_ascii=False)) + " exec " + shlex.quote(sys.executable) + " "
                       + shlex.quote(str(ROOT / "tests" / "fixtures" / "fake_claude.py")) + ' "$@"\n', encoding="utf-8")
    os.chmod(wrapper, 0o755)


if __name__ == "__main__":
    main(sys.argv[1], int(sys.argv[2]))
