"""Бендер как MCP-сервер: его тулы — для других агентов, прежде всего для CLI-движков (Claude Code).

Петлевой сервер (`McpLoop`) поднимается сам при первом ходе через CLI-движок: 127.0.0.1, случайный порт,
одноразовый токен на ход. Токен привязан к ходу Бендера: тулы зовутся от его имени, через Предохранитель
(одобрения — как обычно, в пульт и Telegram), заражение хода видно и обновляется, набор тулов — тот же,
что у хода. В разговоре вызовы видны карточками тулов.

Протокол — JSON-RPC 2.0 по Streamable HTTP (каждое сообщение — POST, ответ — application/json), обе
версии: 2025-11-25 (`initialize` → `notifications/initialized`) и 2026-07-28 без состояния
(`server/discover`, `resultType`). Нужное CLI-движку: tools/list, tools/call, ping.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time

from .. import __version__
from ..net.http_server import HttpServer, JSONResponse, Response, Router

VERSIONS = ("2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
NEW = "2026-07-28"


def _rpc_error(mid, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": mid, "error": {"code": code, "message": message}}


class Binding:
    """К какому ходу привязан токен."""

    def __init__(self, turn, tool_names: tuple | None):
        self.turn = turn
        self.tool_names = tool_names
        self.calls = 0


class McpServer:
    def __init__(self, agent_ctx):
        self.app = agent_ctx

    def tools(self, b: Binding) -> list[dict]:
        out = []
        for t in self.app.tools.specs():
            if b.tool_names is not None and t.name not in b.tool_names:
                continue
            out.append({"name": t.name, "description": t.description, "inputSchema": t.parameters or {"type": "object", "properties": {}}})
        return out

    async def handle(self, msg: dict, b: Binding) -> dict | None:
        mid = msg.get("id")
        method = msg.get("method") or ""
        params = msg.get("params") or {}
        if mid is None:  # уведомление
            return None
        new = isinstance(params.get("_meta"), dict) and str(params["_meta"].get("io.modelcontextprotocol/protocolVersion") or "") == NEW

        def ok(result: dict) -> dict:
            if new:
                result = {"resultType": "complete", **result}
            return {"jsonrpc": "2.0", "id": mid, "result": result}

        info = {"name": "bender", "title": "Бендер", "version": __version__}
        if method == "initialize":
            want = str(params.get("protocolVersion") or "")
            ver = want if want in VERSIONS else "2025-11-25"
            return ok({"protocolVersion": ver, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": info,
                       "instructions": "Тулы Бендера: каждое действие проходит его Предохранитель."})
        if method == "server/discover":
            return ok({"supportedVersions": list(VERSIONS), "capabilities": {"tools": {}}, "serverInfo": info})
        if method == "ping":
            return ok({})
        if method == "tools/list":
            return ok({"tools": self.tools(b)})
        if method == "tools/call":
            return ok(await self.call(str(params.get("name") or ""), params.get("arguments") or {}, b))
        if method in ("resources/list", "prompts/list"):
            return ok({method.split("/")[0]: []})
        return _rpc_error(mid, -32601, f"Нет метода {method}")

    async def call(self, name: str, args: dict, b: Binding) -> dict:
        from ..tools.registry import ToolContext

        agent_ctx = self.app
        turn = b.turn
        sid = turn.session_id
        if b.tool_names is not None and name not in b.tool_names:
            return {"content": [{"type": "text", "text": f"Тул «{name}» этому ходу не выдан."}], "isError": True}
        if agent_ctx.guard.halted:
            return {"content": [{"type": "text", "text": "Стоп-кран: хозяин остановил все действия."}], "isError": True}
        b.calls += 1
        cid = f"mcp_{int(time.time() * 1000) % 10_000_000}_{b.calls}"
        agent_ctx.mind.publish(sid, {"type": "tool_start", "id": cid, "name": name, "arguments": args})
        ctx = ToolContext(app=agent_ctx, session_id=sid, actor="agent", tainted=turn.tainted, unattended=turn.unattended, turn=turn.state,
                          progress=lambda s, _id=cid: agent_ctx.mind.publish(sid, {"type": "tool_progress", "id": _id, "text": s}))
        r = await agent_ctx.tools.run(name, args if isinstance(args, dict) else {}, ctx)
        if r.tainted and not turn.tainted:
            turn.tainted = True
        if r.ok and name in ("read_file", "look") and hasattr(turn, "cost"):
            turn.cost += (r.data or {}).get("aux_cost", 0)  # Уши/Зрение внутри тула — тоже траты хода
        agent_ctx.mind.publish(sid, {"type": "tool_end", "id": cid, "name": name, "ok": r.ok, "summary": r.summary, "tainted": r.tainted, "content": (r.content or "")[:4000]})
        # в разговоре — строкой: история для API-моделей должна оставаться парной (вызов → результат), поэтому не как tool
        msg = {"role": "event", "kind": "tool", "content": f"⚙ {name}: {r.summary or ('готово' if r.ok else 'ошибка')}"}
        agent_ctx.store.add_message(sid, msg, {"via": "mcp", "ok": r.ok, "tainted": r.tainted, "args": json.dumps(args, ensure_ascii=False)[:2000]})
        agent_ctx.bus.publish({"session": sid, "type": "message", "message": msg})
        content = [{"type": "text", "text": r.content or ""}]
        # картинка из read_file или чужого MCP — изображением, как API-моделям (звук MCP не передаёт)
        content += [{"type": "image", "data": part["data"], "mimeType": part.get("media_type", "image/png")}
                    for part in getattr(r, "model_parts", None) or [] if part.get("type") == "image" and part.get("data")]
        return {"content": content, "isError": not r.ok}


class McpLoop:
    """Петлевой MCP-сервер для CLI-движков: поднимается по требованию, токен — на один ход."""

    def __init__(self, agent_ctx):
        self.app = agent_ctx
        self.server = McpServer(agent_ctx)
        self.http: HttpServer | None = None
        self.port: int | None = None
        self.bindings: dict[str, Binding] = {}
        self._lock = asyncio.Lock()

    async def ensure(self) -> int:
        async with self._lock:
            if self.http is None:
                r = Router()

                @r.route("POST", "/mcp")
                async def mcp(req):
                    auth = req.headers.get("authorization") or ""
                    b = self.bindings.get(auth[7:].strip()) if auth.lower().startswith("bearer ") else None
                    if b is None:
                        return JSONResponse(_rpc_error(None, -32001, "Нужен токен хода Бендера"), 401)
                    try:
                        msg = req.json()
                    except ValueError:
                        return JSONResponse(_rpc_error(None, -32700, "Не JSON"), 400)
                    if isinstance(msg, list):  # пачка (старые версии)
                        out = [x for x in [await self.server.handle(m, b) for m in msg if isinstance(m, dict)] if x is not None]
                        return JSONResponse(out) if out else Response(b"", 202)
                    res = await self.server.handle(msg if isinstance(msg, dict) else {}, b)
                    return JSONResponse(res) if res is not None else Response(b"", 202)

                @r.route("GET", "/mcp")
                async def mcp_get(req):
                    return Response(b"", 405, {"Allow": "POST"})

                self.http = HttpServer(r)
                await self.http.start("127.0.0.1", 0)
                self.port = self.http.port
            return self.port

    async def open(self, turn, tool_names: tuple | None) -> tuple[str, str]:
        port = await self.ensure()
        token = secrets.token_urlsafe(24)
        self.bindings[token] = Binding(turn, tool_names)
        return f"http://127.0.0.1:{port}/mcp", token

    def close(self, token: str) -> None:
        self.bindings.pop(token, None)

    def calls(self, token: str) -> int:
        binding = self.bindings.get(token)
        return binding.calls if binding else 0

    async def stop(self) -> None:
        self.bindings.clear()
        if self.http is not None:
            await self.http.stop()
            self.http = None
