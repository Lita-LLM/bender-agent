"""Предложения и экзамены: результаты хранит управляющий процесс."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import signal
import shutil
import sys
import time

from ..core.errors import HumanError
from ..core.ids import new_id
from ..guard import sandbox
from ..updates.manager import snapshot, manifest, canonical

ROOT = Path(__file__).resolve().parents[2]
CATALOG = ROOT / "exams" / "catalog.json"


def exam_hash():
    files = [CATALOG, Path(__file__).with_name("exam_driver.py"), Path(__file__).with_name("exam_worker.py"), sandbox.CHILD]
    return hashlib.sha256(b"\0".join(p.read_bytes() for p in files)).hexdigest()


class Workshop:
    def __init__(self, agent_ctx):
        self.app = agent_ctx
        self.root = agent_ctx.dirs.root / "workshop"
        self.root.mkdir(exist_ok=True)
        self.running = set()
        self.tasks = set()
        self.lock = asyncio.Lock()

    def get(self, ident):
        if not isinstance(ident, str) or not ident.startswith("p-") or not ident[2:].isalnum() or len(ident) > 40:
            raise HumanError("proposal_id", "Неверное имя предложения", "Выбери предложение из списка.")
        record = self.app.store.meta_get("workshop.proposal." + ident)
        if not isinstance(record, dict):
            raise HumanError("proposal_missing", "Предложение не найдено", "Обнови список Мастерской.")
        return dict(record)

    def save(self, record):
        ident = record["id"]
        self.app.store.meta_set("workshop.proposal." + ident, record)
        ids = self.app.store.meta_get("workshop.proposals", [])
        if ident not in ids:
            self.app.store.meta_set("workshop.proposals", (ids + [ident])[-100:])
        self.app.bus.publish({"type": "workshop", "id": ident, "status": record["status"]})

    def enabled(self):
        if self.app.safe or not self.app.settings.get("workshop", "enabled", True):
            raise HumanError("workshop_off", "Мастерская выключена", "Хозяин может включить её в настройках.")

    def public(self):
        history = self.app.store.meta_get("workshop.exams", [])
        from .proxy import status
        return {"network_proxy": status(self.app), "enabled": self.app.settings.get("workshop", "enabled", True), "catalog": json.loads(CATALOG.read_text(encoding="utf-8")),
                "history": history, "last": history[-1] if history else None, "running": bool(self.running),
                "live_enabled": self.app.settings.get("workshop", "live_exams", False), "live_history": self.app.store.meta_get("workshop.live", []),
                "proposals": [self.get(ident) for ident in reversed(self.app.store.meta_get("workshop.proposals", []))]}

    async def run_exams(self, ctx, *, candidate=None):
        self.enabled()
        await ctx.check("exam.run", "экзамены", "проверить копию без сети и настоящих данных")
        info = sandbox.for_app(self.app)
        if info.get("kind") != "bwrap":
            raise HumanError("exam_isolation", "Экзаменам нужна изоляция bubblewrap", "Установи bubblewrap с поддержкой пространств имён. Без неё чужой код не запускается.")
        async with self.lock:
            self.enabled()
            ident = "exam-" + new_id()
            folder = self.root / ident
            folder.mkdir()
            source = folder / "candidate"
            record = await asyncio.to_thread(snapshot, Path(candidate or ROOT), source)
            scratch = folder / "scratch"
            scratch.mkdir()
            config = {"candidate": str(source), "bwrap": info["path"], "worker": str(Path(__file__).with_name("exam_worker.py")),
                      "catalog": str(CATALOG), "limits_helper": str(sandbox.CHILD), "scratch": str(scratch),
                      "limits": {name: min(info["limits"].get(name, ceiling), ceiling) for name, ceiling in
                                 {"RLIMIT_CPU": 120, "RLIMIT_AS": 1024 * 1024 * 1024, "RLIMIT_NPROC": 1024, "RLIMIT_FSIZE": 64 * 1024 * 1024}.items()},
                      "nonce": new_id(), "timeout": self.app.settings.get("workshop", "exam_seconds", 30)}
            proc = await asyncio.create_subprocess_exec(sys.executable, str(Path(__file__).with_name("exam_driver.py")), stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
                env={k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "SYSTEMROOT")})
            self.running.add(proc)
            started = time.time()
            try:
                out, err = await asyncio.wait_for(proc.communicate(json.dumps(config).encode()), config["timeout"] * 6 + 15)
                report = json.loads(out)
                if proc.returncode or report.pop("nonce", None) != config["nonce"] or len(report.get("results", [])) != len(self.public()["catalog"]):
                    raise ValueError("Проверяющий не подтвердил полный набор экзаменов")
            except asyncio.CancelledError:
                raise
            except (ValueError, asyncio.TimeoutError) as exc:
                report = {"ok": False, "results": [], "error": "Проверяющий не завершил прогон: " + str(exc)[:500]}
            finally:
                if proc.returncode is None:
                    os.killpg(proc.pid, signal.SIGKILL)
                    await proc.wait()
                self.running.discard(proc)
                shutil.rmtree(folder, ignore_errors=True)
            report.update({"id": ident, "at": started, "seconds": round(time.time() - started, 3), "candidate_hash": hashlib.sha256(canonical(manifest(record))).hexdigest(), "exam_hash": exam_hash()})
            history = self.app.store.meta_get("workshop.exams", [])
            self.app.store.meta_set("workshop.exams", (history + [report])[-100:])
            self.app.journal.write("exam", "Экзамены: " + ("пройдены" if report["ok"] else "провалены"), actor=ctx.actor, data={"id": ident, "ok": report["ok"]})
            return report

    def stop(self):
        for task in tuple(self.tasks):
            task.cancel()
        for proc in tuple(self.running):
            if proc.returncode is None:
                os.killpg(proc.pid, signal.SIGKILL)
