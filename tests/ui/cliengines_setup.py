"""Проверка выключенного движка Codex без ключей и модели."""
import asyncio
import json
import os
import shlex
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from bender.core.app import App

app = App(sys.argv[1])
app.selfdir.set_identity({"name": "Тин"})
app.selfdir.finish_awakening()
app.guard.add_rule("exec", "*", "allow")
asyncio.run(app.close())
folder = Path(sys.argv[1]).resolve().parent
bindir = folder / "bin"
bindir.mkdir()
for name in ("codex",):
    prefix = ""
    program = bindir / name
    program.write_text("#!/bin/sh\n" + prefix + "exec " + shlex.quote(sys.executable) + " " + shlex.quote(str(ROOT / "tests/fixtures" / ("fake_" + name + ".py"))) + ' "$@"\n')
    os.chmod(program, 0o755)
