"""Код тула проходит тесты, экзамены, просмотр пакета и карантин MCP."""
from __future__ import annotations

import asyncio
import json
import os
import signal
import tempfile
import time

from ..core.errors import HumanError
from ..core.fsutil import atomic_write_text
from ..core.ids import new_id
from ..skills.manager import ID
from .isolation import Confined
from .manager import exam_hash


def allowed(workshop, ctx, key):
    workshop.enabled()
    if not workshop.app.settings.get("workshop", key, True):
        raise HumanError("proposal_off", "Такие предложения выключены", "Хозяин может включить их в настройках Мастерской.")
    if ctx.tainted or ctx.unattended:
        raise HumanError("proposal_context", "Из заражённого или безлюдного хода код не предлагается", "Начни отдельный чистый разговор с хозяином.")


async def test_package(workshop, path, *, arguments=None, seconds=60):
    """Самотесты — диагностический лог. Допуск отдельно дают доверенные экзамены."""
    prepared = Confined(workshop.app, path, arguments or ["-m", "unittest", "discover", "-s", "/package", "-p", "test_*.py", "-v"])
    with tempfile.TemporaryFile() as output:
        try:
            proc = await asyncio.create_subprocess_exec(*prepared.argv, stdout=output, stderr=output, env=prepared.env, start_new_session=True)
        except OSError as exc:
            prepared.close()
            raise HumanError("workshop_start", "Не удалось запустить проверку копии", "Проверь bubblewrap, интерпретатор и свободные ресурсы.", detail=str(exc)) from None
        workshop.running.add(proc)
        started = time.time()
        try:
            await asyncio.wait_for(proc.wait(), seconds)
            ok = proc.returncode == 0
        except asyncio.TimeoutError:
            ok = False
        finally:
            if proc.returncode is None:
                os.killpg(proc.pid, signal.SIGKILL)
                await proc.wait()
            workshop.running.discard(proc)
            prepared.close()
        output.seek(0)
        head = output.read(8000)
        size = output.seek(0, 2)
        if size > 16000:
            output.seek(-8000, 2)
            head += "\n… середина журнала опущена …\n".encode() + output.read(8000)
        elif size > 8000:
            output.seek(8000)
            head += output.read(8000)
        log = head.decode("utf-8", "replace")
    return {"ok": ok, "seconds": round(time.time() - started, 3), "log": log, "exit_code": proc.returncode}


async def propose(workshop, ctx, name, description, code, tests, capabilities, network_hosts=None):
    task = asyncio.current_task()
    workshop.tasks.add(task)
    try:
        return await _propose(workshop, ctx, name, description, code, tests, capabilities, network_hosts)
    finally:
        workshop.tasks.discard(task)


async def _propose(workshop, ctx, name, description, code, tests, capabilities, network_hosts=None):
    allowed(workshop, ctx, "tools_enabled")
    if not isinstance(name, str) or not ID.fullmatch(name) or len(name) > 55:
        raise HumanError("tool_name", "Нужно короткое имя тула латиницей", "До 55 строчных букв, цифр и дефисов.")
    if not isinstance(description, str) or not 1 <= len(description) <= 1000 or any(not isinstance(text, str) or not 20 <= len(text) <= 65536 for text in (code, tests)):
        raise HumanError("tool_code", "Нужны описание, код MCP-сервера и тесты", "Код и тесты — от 20 до 65536 знаков каждый.")
    if not isinstance(capabilities, list) or any(not isinstance(c, str) for c in capabilities) or len(capabilities) != len(set(capabilities)) or "exec" not in capabilities or any(c not in ("exec", "fs.read", "fs.write", "net.read") for c in capabilities) or ("fs.write" in capabilities and "fs.read" not in capabilities):
        raise HumanError("workshop_caps", "Проверь список способностей тула", "Нужен exec внутри изоляции. Доступны fs.read и fs.write рабочей папки (запись вместе с чтением). net.read — только точные домены через посредник. Сообщения недоступны.")
    if "net.read" in capabilities:
        from .proxy import hosts
        network_hosts = hosts(network_hosts)
    elif network_hosts:
        raise HumanError('workshop_hosts', 'Домены указаны без net.read', 'Добавь способность net.read или убери домены.')
    await ctx.check("workshop.propose", name, "предложить код MCP-тула с тестами, без применения")
    allowed(workshop, ctx, "tools_enabled")
    ident = "p-" + new_id().lower()
    folder = workshop.root / ident
    package = folder / "package"
    package.mkdir(parents=True)
    metadata = {"name": "ws-" + name, "version": ident, "description": description, "capabilities": capabilities,
                "workshop": True, **({"network_hosts": network_hosts} if network_hosts else {}), "mcp": {"entry": "server.py", "runtime": "python"}}
    for rel, text in {"bender-module.json": json.dumps(metadata, ensure_ascii=False), "server.py": code, "test_tool.py": tests}.items():
        atomic_write_text(package / rel, text)
    item = workshop.app.modules.package_at(package)
    record = {"id": ident, "kind": "tool", "name": name, "description": description, "at": time.time(), "status": "testing", "hash": item["hash"], "capabilities": capabilities, "network_hosts": network_hosts or []}
    workshop.save(record)
    try:
        record["tests"] = await test_package(workshop, package)
        from ..mcp.client import Client, Stdio

        prepared = Confined(workshop.app, package, ["/package/server.py"])
        client = Client(Stdio(prepared.argv, cwd=workshop.app.dirs.work, env=prepared.env, cleanup=prepared.close), timeout=10)
        try:
            await client.connect()
            record["definitions"] = await client.tools()
            if not record["definitions"]:
                raise HumanError("tool_definitions", "MCP-сервер не объявил ни одного тула", "Добавь tools/list и повтори предложение.")
        finally:
            await client.close()
        record["exams"] = await workshop.run_exams(ctx)
        record["status"] = "waiting" if record["tests"]["ok"] and record["exams"]["ok"] else "failed"
    except HumanError as exc:
        record.update(status="failed", error=exc.to_dict())
    except asyncio.CancelledError:
        record.update(status="failed", error={"title": "Проверка отменена"})
        workshop.save(record)
        raise
    workshop.save(record)
    return record


async def approve(workshop, ident, expected, ctx):
    allowed(workshop, ctx, "tools_enabled")
    record = workshop.get(ident)
    if record["kind"] != "tool" or record["status"] not in ("waiting", "archived") or record["hash"] != expected or not record["exams"].get("ok") or record["exams"].get("exam_hash") != exam_hash():
        raise HumanError("proposal_changed", "Предложение не прошло проверку или изменилось", "Повтори проверку и просмотр.")
    folder = workshop.root / ident / "package"
    package = workshop.app.modules.package_at(folder, full=True)
    if package["hash"] != expected:
        raise HumanError("proposal_changed", "Код тула изменился после проверки", "Создай новое предложение.")
    await ctx.check("self.code", ident, "одобрить просмотренный код тула и его способности", detail={"hash": expected, "capabilities": record["capabilities"], "network_hosts": package["metadata"].get("network_hosts", []), "tests": record["tests"], "exams": record["exams"]["id"]})
    allowed(workshop, ctx, "tools_enabled")
    if record["exams"].get("exam_hash") != exam_hash():
        raise HumanError("proposal_changed", "Экзамены изменились во время одобрения", "Повтори предложение и проверку.")
    # Повторная сверка после ожидания карточки.
    if workshop.app.modules.package_at(folder)["hash"] != expected:
        raise HumanError("proposal_changed", "Пакет изменился во время одобрения", "Создай новое предложение.")
    modules = workshop.app.modules
    name = package["id"]
    candidate = modules.path(name).exists()
    target = modules.path(name, candidate=candidate)
    if target.exists():
        raise HumanError("proposal_busy", "Другое предложение этого тула уже подготовлено", "Сначала заверши его в разделе «Модули».")
    modules.copy(package, target)
    await modules.approve(name, expected, candidate=candidate)
    for other in workshop.public()["proposals"]:
        if other["id"] != ident and other.get("module") == name and other["status"] in ("approved", "applied", "disabled"):
            other["status"] = "archived"
            workshop.save(other)
    record.update(status="approved", module=name, server=modules.trust(name).get("server"))
    workshop.save(record)
    return record


async def definitions(workshop, ident, ctx, expected=None):
    workshop.enabled()
    record = workshop.get(ident)
    if record.get("kind") != "tool" or record.get("status") not in ("approved", "applied"):
        raise HumanError("proposal_status", "Сначала одобри код тула", "Просмотри код, результаты и способности.")
    if expected is None:
        await workshop.app.mcp.discover(record["server"], ctx)
        return workshop.app.mcp.public()
    workshop.app.mcp.approve(record["server"], expected)
    record["status"] = "applied"
    workshop.save(record)
    return record
