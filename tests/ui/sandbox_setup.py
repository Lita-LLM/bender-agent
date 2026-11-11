"""Подготовка проверки защиты ОС: команды разрешены явным правилом тестового хозяина."""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from bender.core.app import App

app = App(sys.argv[1])
# RLIMIT_NPROC считает весь UID, включая процессы Chromium.
app.settings.set("guard", {"process_count": 1024}, by="owner")
app.selfdir.set_identity({"name": "Тин"})
app.selfdir.finish_awakening()
app.guard.add_rule("exec", "*", "allow")
asyncio.run(app.close())
