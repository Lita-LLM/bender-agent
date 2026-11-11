"""API экрана «Память»: обзор, дневник (без выхода из папки), факты, сжатие."""

import asyncio
import json
import urllib.error
import urllib.request

from bender.memory import selfdir
from bender.net.http_server import HttpServer
from tests.helpers import owner_web as create_web, owner_headers
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv


def _call(port, path, body=None):
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode() if body is not None else None,
                                 headers=owner_headers(**({"X-Bender": "1", "Content-Type": "application/json"} if body is not None else {})))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class MemoryApiTest(TempDirCase):
    @async_test
    async def test_overview_diary_facts_compact(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            pid = app.models.roles()["main"][0]["provider"]
            app.models.set_role("fast", [{"provider": pid, "model": "script"}])
            app.settings.set("agent", {"history_tokens": 1200})
            web = HttpServer(create_web(app, allowed_hosts={"127.0.0.1"}))
            await web.start("127.0.0.1", 0)
            port = web.port
            api = lambda path, body=None: asyncio.to_thread(_call, port, path, body)  # noqa: E731
            try:
                selfdir.append_diary(app.dirs.diary, "Купили новый чайник")
                (self.tmp / "secret.md").write_text("не для пульта", encoding="utf-8")
                app.facts.add("хозяин", "город", "Казань", single=True)
                for i in range(20):
                    app.store.add_message(env.sid, {"role": "user", "content": f"вопрос номер {i}: " + "расскажи подробнее про проект " * 6})
                    app.store.add_message(env.sid, {"role": "assistant", "content": f"ответ номер {i}: " + "вот подробности о проекте " * 6})

                code, ov = await api("/api/memory/overview")
                self.assertEqual(code, 200)
                self.assertEqual(ov["core"]["memory"]["limit"], 40000)
                self.assertEqual(ov["facts"]["active"], 1)
                self.assertEqual(ov["diary_total"], 1)
                self.assertGreater(ov["compact"]["tail_tokens"], ov["compact"]["budget"])
                self.assertEqual(ov["review"]["every"], 1)  # «Быстрая» есть — после каждого хода

                code, d = await api("/api/memory/diary?path=" + ov["diary"][0]["path"])
                self.assertIn("чайник", d["text"])
                for bad in ("../../secret.md", "/etc/passwd", "../" * 6 + "etc/passwd"):
                    code, _ = await api("/api/memory/diary?path=" + urllib.request.quote(bad))
                    self.assertEqual(code, 404, bad)

                code, f = await api("/api/memory/facts")
                fid = f["facts"][0]["id"]
                code, _ = await api(f"/api/memory/facts/{fid}/close", {})
                self.assertEqual(code, 200)
                self.assertEqual((await api("/api/memory/facts"))[1]["facts"], [])
                self.assertEqual(len((await api("/api/memory/facts?all=1"))[1]["facts"]), 1)

                env.script([{"text": '{"summary": "Говорили о проекте: ПЕРЕСКАЗ", "diary": ""}'}])
                code, st = await api("/api/memory/compact", {"action": "now"})
                self.assertEqual(code, 200, st)
                self.assertIn("ПЕРЕСКАЗ", st["summary"])
                code, ov = await api("/api/memory/overview")
                self.assertLess(ov["compact"]["tail_tokens"], ov["compact"]["budget"])
                code, _ = await api("/api/memory/compact", {"action": "reset"})
                self.assertEqual((await api("/api/memory/overview"))[1]["compact"]["summary"], "")

                code, err = await api("/api/memory/vectors", {})
                self.assertEqual(code, 400)  # нет роли «Эмбеддинги» — понятная ошибка
                self.assertIn("Эмбеддинги", err["error"]["hint"])
            finally:
                await web.stop()
