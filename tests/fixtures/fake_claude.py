"""Фальшивый `claude -p` для тестов CLI-движка: читает промпт из stdin, разбирает флаги, ходит в MCP Бендера
(initialize → tools/list → tools/call по сценарию) и печатает поток stream-json, как настоящий Claude Code.

Сценарий — переменная окружения FAKE_CLAUDE (JSON): {"tools": [{"name": …, "arguments": {…}}], "text": "…",
"error": "текст ошибки", "exit": 1}. Что пришло (аргументы, промпт, список тулов) — в файл FAKE_CLAUDE_LOG (JSON строками).
"""

import json
import os
import sys
import urllib.request
import uuid


def arg(name, default=None):
    a = sys.argv
    return a[a.index(name) + 1] if name in a and a.index(name) + 1 < len(a) else default


def rpc(url, token, method, params=None, mid=1):
    body = {"jsonrpc": "2.0", "method": method, "params": params or {}}
    if mid is not None:
        body["id"] = mid
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}",
                                                                              "Accept": "application/json, text/event-stream"})
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=60) as r:
        raw = r.read()
        return json.loads(raw) if raw else None


def out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main():
    plan = json.loads(os.environ.get("FAKE_CLAUDE") or "{}")
    if "--help" in sys.argv:  # как настоящий: флаги, которые знает эта «версия» (FAKE_CLAUDE_OLD — старая без новых флагов)
        flags = ["-p", "--output-format", "--verbose", "--tools", "--mcp-config", "--strict-mcp-config", "--allowedTools", "--system-prompt",
                 "--model", "--resume", "--session-id"]
        if not os.environ.get("FAKE_CLAUDE_OLD"):
            flags += ["--include-partial-messages", "--permission-prompts", "--disable-slash-commands", "--system-prompt-snapshot",
                      "--no-session-persistence", "--restricted", "--effort", "--input-format"]
        print("Usage: claude [options]\n" + "\n".join("  " + f for f in flags))
        return
    if "--version" in sys.argv:
        print("9.9.9 (Claude Code)")
        return
    if sys.argv[1:3] == ["auth", "status"]:
        auth = plan.get("auth", "claude.ai")
        print(json.dumps({"loggedIn": bool(auth), "authMethod": auth or "none", "apiProvider": "firstParty"}, indent=2))
        sys.exit(0 if auth else 1)
    if os.environ.get("FAKE_CLAUDE_OLD"):
        for f in ("--include-partial-messages", "--permission-prompts", "--disable-slash-commands", "--system-prompt-snapshot", "--no-session-persistence", "--restricted"):
            if f in sys.argv:
                sys.stderr.write(f"error: unknown option '{f}'\n")
                sys.exit(1)
    prompt = sys.stdin.read()
    images = []
    if arg("--input-format") == "stream-json":  # одно сообщение с блоками: текст — промптом, картинки — отдельно
        blocks = json.loads(prompt.splitlines()[0])["message"]["content"]
        prompt = "".join(b.get("text", "") for b in blocks if b.get("type") == "text")
        images = [b["source"]["media_type"] for b in blocks if b.get("type") == "image"]
    cfg = json.loads(arg("--mcp-config", "{}"))
    srv = (cfg.get("mcpServers") or {}).get("bender") or {}
    url, token = srv.get("url"), (srv.get("headers") or {}).get("Authorization", "")[7:]
    sid = arg("--resume") or arg("--session-id") or str(uuid.uuid4())
    log = {"argv": sys.argv[1:], "prompt": prompt, "session": sid, "resume": "--resume" in sys.argv, "images": images, "tools": [], "results": [],
           "env": {k: v for k, v in os.environ.items() if k.startswith(("CLAUDE_CODE_DISABLE", "CLAUDE_CODE_EFFORT"))}}
    out({"type": "system", "subtype": "init", "session_id": sid, "model": arg("--model", "default"), "tools": []})
    if plan.get("error"):
        out({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": plan["error"], "session_id": sid})
        sys.stderr.write(plan["error"] + "\n")
        _log(log)
        sys.exit(int(plan.get("exit", 1)))
    if url:
        init = rpc(url, token, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "fake-claude", "version": "0"}})
        log["server"] = (init or {}).get("result", {}).get("serverInfo")
        rpc(url, token, "notifications/initialized", mid=None)
        listed = rpc(url, token, "tools/list", mid=2)
        log["tools"] = [t["name"] for t in (listed or {}).get("result", {}).get("tools", [])]
        for i, call in enumerate(plan.get("tools") or []):
            out({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": f"mcp__bender__{call['name']}", "input": call.get("arguments", {})}]},
                 "session_id": sid})
            r = rpc(url, token, "tools/call", {"name": call["name"], "arguments": call.get("arguments", {})}, mid=10 + i)
            res = (r or {}).get("result") or {}
            log["results"].append({"name": call["name"], "isError": res.get("isError"), "text": "".join(c.get("text", "") for c in res.get("content", []))[:500],
                                   "images": [c.get("mimeType") for c in res.get("content", []) if c.get("type") == "image"]})
    text = plan.get("text", "Готово.")
    for part in [text[: len(text) // 2], text[len(text) // 2:]]:
        if part:
            out({"type": "stream_event", "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": part}}, "session_id": sid})
    out({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": text}]}, "session_id": sid})
    out({"type": "result", "subtype": "success", "is_error": False, "result": text, "session_id": sid, "total_cost_usd": 0.0123,
         "usage": {"input_tokens": 120, "output_tokens": 30, "cache_read_input_tokens": 100}})
    _log(log)


def _log(log):
    path = os.environ.get("FAKE_CLAUDE_LOG")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(log, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
