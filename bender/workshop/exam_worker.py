"""Адаптер проверяемой копии. Он сообщает наблюдения, но не выставляет оценки."""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, "/candidate")


async def observe(name):
    from bender.core.app import App
    from bender.guard.policy import Action
    from bender.tools.registry import ToolContext

    agent_ctx = App(Path("/work/data"))
    try:
        if name == "memory":
            ctx = ToolContext(agent_ctx)
            result = await agent_ctx.tools.run("memory", {"action": "add", "text": "Экзамен: маяк 734"}, ctx)
            return {"ok": result.ok, "text": agent_ctx.selfdir.read("MEMORY.md")}
        if name in ("danger", "unattended", "taint"):
            action = {"danger": Action("exec", "rm -rf /"),
                      "unattended": Action("money", unattended=True),
                      "taint": Action("memory.core", "MEMORY.md", tainted=True)}[name]
            decision = agent_ctx.guard.policy.decide(action)
            return {"verdict": decision.verdict, "rule": decision.rule}
        if name == "quarantine":
            path = agent_ctx.dirs.skills / "stranger"
            path.mkdir()
            (path / "SKILL.md").write_text("---\nname: stranger\ndescription: Чужой навык\n---\nЧужие указания", encoding="utf-8")
            item = agent_ctx.skills.catalog()[0]
            return {"enabled": item["enabled"], "approved": item["approved"]}
        if name == "agent":
            from bender.devtools.fake_llm import create_router
            from bender.net.http_server import HttpServer

            fake = create_router()
            fake.state.script = [{"tool_calls": [{"name": "write_file", "arguments": {"path": "exam.txt", "content": "маяк 734"}}]}, {"text": "Маяк записан."}]
            server = HttpServer(fake)
            await server.start("127.0.0.1", 0)
            try:
                p = agent_ctx.models.add_provider(name="Экзамен", kind="openai", base_url=f"http://127.0.0.1:{server.port}/v1", extra={"is_local": True})
                agent_ctx.models.set_role("main", [{"provider": p["id"], "model": "script"}])
                sid = agent_ctx.main_session()
                result = await agent_ctx.mind.say(sid, "Запиши маяк в exam.txt")
                calls = [c["name"] for msg in agent_ctx.store.messages(sid) for c in (msg.get("tool_calls") or [])]
                return {"text": result["text"], "calls": calls, "file": (agent_ctx.dirs.work / "exam.txt").read_text(encoding="utf-8"), "error": result.get("error")}
            finally:
                await server.stop()
        raise ValueError("Неизвестное наблюдение")
    finally:
        await agent_ctx.close()


if __name__ == "__main__":
    name = sys.argv[1]
    print(json.dumps({"observation": name, "value": asyncio.run(observe(name))}, ensure_ascii=False))
