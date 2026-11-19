"""Проверка Claude Code из пульта не запускает команду из адреса GET (16.11)."""

import asyncio
import json
import stat
import urllib.error
import urllib.request

from bender.net.http_server import HttpServer
from tests.helpers import TempDirCase, async_test, owner_headers
from tests.helpers import owner_web as create_web
from tests.test_mind import MindEnv


def _call(port, path, body=None, pult=True):
    h = owner_headers(**({"Content-Type": "application/json"} if body is not None else {}), **({"X-Bender": "1"} if pult else {}))
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode() if body is not None else None, headers=h)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class ClaudeCodeCheckTest(TempDirCase):
    @async_test
    async def test_get_ignores_command_and_post_needs_pult_header(self):
        marker = self.tmp / "ran"
        evil = self.tmp / "evil.sh"
        evil.write_text(f"#!/bin/sh\ntouch {marker}\n", encoding="utf-8")
        evil.chmod(evil.stat().st_mode | stat.S_IXUSR)
        async with MindEnv(self.tmp) as env:
            web = HttpServer(create_web(env.app, allowed_hosts={"127.0.0.1"}))
            await web.start("127.0.0.1", 0)
            api = lambda *a, **kw: asyncio.to_thread(_call, web.port, *a, **kw)  # noqa: E731
            try:
                code, out = await api(f"/api/claude-code/check?command={evil}", pult=False)
                self.assertEqual(code, 200, out)
                self.assertNotEqual(out.get("command"), [str(evil)])
                code, out = await api("/api/claude-code/check", {"command": str(evil)}, pult=False)
                self.assertEqual(code, 403)
                self.assertFalse(marker.exists())
            finally:
                await web.stop()


if __name__ == "__main__":
    import unittest
    unittest.main()
