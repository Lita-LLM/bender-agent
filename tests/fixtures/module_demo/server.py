"""Модуль-пример: echo через stdio MCP, без доступа к объектам Бендера."""
import json
import sys

for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message.get("method")
    if method == "server/discover":
        result = {"supportedVersions": ["2026-07-28"], "capabilities": {"tools": {}}, "serverInfo": {"name": "demo"}}
    elif method == "initialize":
        result = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}, "serverInfo": {"name": "demo"}}
    elif method == "tools/list":
        result = {"tools": [{"name": "echo", "description": "Повторить текст из примера модуля", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "Модуль: " + str(message.get("params", {}).get("arguments", {}).get("text", ""))}]}
    else:
        result = {}
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}, ensure_ascii=False), flush=True)
