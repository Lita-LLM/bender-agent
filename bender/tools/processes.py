"""Фоновые команды: тот же запуск и защита, без опроса и постоянного журнала stdout."""
from __future__ import annotations

import asyncio

from ..core.errors import HumanError
from ..core.ids import new_id
from .registry import ToolResult
from .shell import MAX_CAPTURE, _kill, start_command
from .web import untrusted


class Processes:
    def __init__(self, agent_ctx):
        self.app, self.items = agent_ctx, {}
        self.starting = 0

    async def start(self, ctx, command, cwd, allow_net, timeout):
        if self.app.safe or not self.app.settings.get("processes", "enabled", True):
            raise HumanError("process_off", "Фоновые команды выключены", "Включи их в настройках.")
        running = sum(not x["task"].done() for x in self.items.values()) + self.starting
        if running >= self.app.settings.get("processes", "max_running", 4):
            raise HumanError("process_limit", "Достигнут предел фоновых команд", "Останови или дождись одной из запущенных.")
        self.starting += 1
        try:
            proc, prepared, _, workdir, info = await start_command(ctx, command, cwd, allow_net)
        finally:
            self.starting -= 1
        identifier = new_id()
        item = {"process": proc, "output": bytearray(), "session": ctx.session_id, "timeout": False}
        limit = max(1, min(int(timeout), self.app.settings.get("processes", "timeout_seconds", 1800)))

        async def pump():
            while True:
                chunk = await proc.stdout.read(65536)
                if not chunk:
                    return
                item["output"].extend(chunk)
                if len(item["output"]) > MAX_CAPTURE:
                    del item["output"][:-MAX_CAPTURE]

        async def monitor():
            completion = asyncio.gather(pump(), proc.wait())
            try:
                await asyncio.wait_for(completion, limit)
            except asyncio.TimeoutError:
                item["timeout"] = True
            finally:
                if proc.returncode is None:
                    _kill(proc)
                    await proc.wait()
                if not completion.done():
                    completion.cancel()
                await asyncio.gather(completion, return_exceptions=True)
                prepared.close()
                self.app.journal.write("exec", f"Фоновая команда {identifier} завершилась: {proc.returncode}", actor=ctx.actor)

        item["task"] = asyncio.create_task(monitor())
        self.items[identifier] = item
        for old in list(self.items):
            if len(self.items) <= 32:
                break
            if self.items[old]["task"].done():
                del self.items[old]
        self.app.journal.write("exec", f"Фоновая команда {identifier}: {command[:200]}", actor=ctx.actor,
                               data={"pid": proc.pid, "cwd": str(workdir), "sandbox": info["kind"]})
        return identifier

    def get(self, identifier, ctx):
        item = self.items.get(identifier)
        if item is None or (ctx.actor != "owner" and item["session"] != ctx.session_id):
            raise HumanError("process_missing", "Нет такого процесса в этой задаче", "Нужен id, полученный от process_start.")
        return item

    def stop(self):
        for item in self.items.values():
            if not item["task"].done():
                _kill(item["process"])

    async def close(self):
        self.stop()
        await asyncio.gather(*(x["task"] for x in self.items.values()), return_exceptions=True)


def register(reg):
    available = lambda agent_ctx: not agent_ctx.safe and agent_ctx.settings.get("processes", "enabled", True)

    @reg.tool("process_start", "Запустить фоновую команду", group="shell", available=available)
    async def process_start(ctx, command: str, cwd: str = "", allow_net: bool = False, timeout: int = 1800):
        """Долгая фоновая команда: вернуть id сразу; короткая с готовым выводом — run. Защита и лимиты те же.
        Проверяй вывод через process_status, останавливай process_stop. Дольше заданного времени процесс не живёт.
        """
        identifier = await ctx.app.processes.start(ctx, command, cwd, allow_net, timeout)
        return ToolResult(f"Запущен процесс {identifier}", data={"id": identifier}, summary="фоновая команда запущена")

    @reg.tool("process_status", "Прочитать вывод процесса", group="shell", available=available)
    async def process_status(ctx, id: str):
        """Прочитать накопленный вывод своего процесса и код выхода без ожидания и опроса в фоне."""
        item = ctx.app.processes.get(id, ctx)
        text = ctx.app.guard.scrub(item["output"].decode("utf-8", "replace"))
        state = {"id": id, "running": not item["task"].done(), "code": item["process"].returncode, "timed_out": item["timeout"]}
        return ToolResult(untrusted("вывод фоновой команды", text), tainted=True, data=state, summary="работает" if state["running"] else "завершён")

    @reg.tool("process_stop", "Остановить свой процесс", group="shell", available=available)
    async def process_stop(ctx, id: str):
        """Остановить запущенную ранее команду по её id, освободить временные файлы."""
        item = ctx.app.processes.get(id, ctx)
        await ctx.check("exec", f"process_stop {item['process'].pid}", "остановить свой фоновый процесс")
        _kill(item["process"])
        await item["task"]
        return ToolResult("Процесс остановлен", summary="процесс остановлен")
