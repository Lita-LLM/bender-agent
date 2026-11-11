"""Маленький MCP-сервер для проверки Мастерской, без зависимостей."""
import json
from pathlib import Path
import sys


def add(a, b):
    return a + b


def observe():
    try:
        Path("/package/write-trap").write_text("подмена")
        readonly = False
    except OSError:
        readonly = True
    return {"host": Path("/etc/passwd").exists(), "exam": Path("/workspace/BENDER/exams/catalog.json").exists(), "readonly": readonly}


def main():
    for line in sys.stdin:
        request = json.loads(line)
        if "id" not in request:
            continue
        method = request["method"]
        response = {"jsonrpc": "2.0", "id": request["id"]}
        if method == "initialize":
            response["result"] = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}, "serverInfo": {"name": "workshop-example", "version": "1"}}
        elif method == "tools/list":
            response["result"] = {"tools": [
                {"name": "sum", "description": "Сложить два числа", "inputSchema": {"type": "object", "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}}, "required": ["a", "b"]}},
                {"name": "inspect", "description": "Проверить изоляцию", "inputSchema": {"type": "object", "properties": {}}},
            ]}
        elif method == "tools/call":
            params = request["params"]
            value = observe() if params["name"] == "inspect" else add(**params["arguments"])
            response["result"] = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]}
        else:
            response["error"] = {"code": -32601, "message": "Нет такого метода"}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
