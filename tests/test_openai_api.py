"""Бендер как модель: OpenAI-совместимый /v1/chat/completions (ключ, ответ, поток, отдельная сессия)."""

import asyncio
import json
import urllib.error
import urllib.request

from bender.channels.openai_api import KEY_SECRET
from bender.net.http_server import HttpServer
from tests.helpers import owner_web as create_web, owner_headers
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv


def _call(port, path, body=None, key=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=json.dumps(body).encode() if body is not None else None, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=15) as r:
            raw = r.read().decode("utf-8")
            return r.status, (raw if r.headers.get("content-type", "").startswith("text/event-stream") else json.loads(raw))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


class OpenAIApiTest(TempDirCase):
    @async_test
    async def test_api(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            web = HttpServer(create_web(app, allowed_hosts={"127.0.0.1"}))
            await web.start("127.0.0.1", 0)
            api = lambda path, body=None, key=None: asyncio.to_thread(_call, web.port, path, body, key)  # noqa: E731
            try:
                code, out = await api("/v1/models")
                self.assertEqual((code, out["error"]["code"]), (403, "api_disabled"))
                app.settings.set("api", {"enabled": True})
                code, out = await api("/v1/models")
                self.assertEqual((code, out["error"]["code"]), (401, "invalid_api_key"))
                app.secrets.set(KEY_SECRET, "bender-test-key")
                code, out = await api("/v1/models", key="wrong-key")
                self.assertEqual(code, 401)
                code, out = await api("/v1/models", key="bender-test-key")
                self.assertEqual([m["id"] for m in out["data"]], ["bender", "bender-isolated", "bender/main"])

                # API разговаривает отдельно от главного чата; начальная история — данные клиента
                env.script([{"text": "Привет из Бендера"}])
                code, out = await api("/v1/chat/completions", {"model": "bender", "messages": [{"role": "user", "content": "старое"}, {"role": "user", "content": "привет"}]},
                                      key="bender-test-key")
                self.assertEqual(code, 200, out)
                self.assertEqual(out["choices"][0]["message"], {"role": "assistant", "content": "Привет из Бендера"})
                main = app.store.messages(out["bender"]["session"])
                self.assertNotEqual(out["bender"]["session"],app.main_session())
                self.assertEqual([m["content"] for m in main if m["role"] == "user"], ["старое", "привет"])
                self.assertEqual(main[0]["meta"]["channel"], "api")

                # поток
                env.script([{"text": "раз два три"}])
                code, raw = await api("/v1/chat/completions", {"model": "bender", "stream": True, "messages": [{"role": "user", "content": "считай"}]}, key="bender-test-key")
                self.assertEqual(code, 200)
                datas = [ln[6:] for ln in raw.splitlines() if ln.startswith("data: ")]
                self.assertEqual(datas[-1], "[DONE]")
                chunks = [json.loads(d) for d in datas[:-1]]
                self.assertEqual("".join(c["choices"][0]["delta"].get("content", "") for c in chunks), "раз два три")
                self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")

                # отдельная сессия: история и указания программы — из запроса
                env.script([{"text": "Ответ в роли"}])
                code, out = await api("/v1/chat/completions", {"model": "bender-isolated", "messages": [
                    {"role": "system", "content": "Ты играешь пирата."}, {"role": "user", "content": "Кто ты?"},
                    {"role": "assistant", "content": "Пират!"}, {"role": "user", "content": "Где сокровища?"}]}, key="bender-test-key")
                self.assertEqual(out["choices"][0]["message"]["content"], "Ответ в роли")
                sid = out["bender"]["session"]
                self.assertEqual(app.store.session(sid)["kind"], "api")
                msgs = app.store.messages(sid)
                self.assertEqual([m["role"] for m in msgs[:3]], ["user", "assistant", "user"])
                self.assertIn("Ты играешь пирата", msgs[2]["content"])
                self.assertIn("Где сокровища?", msgs[2]["content"])
                sent = [r for r in env.fake.state.requests if r.get("api") == "openai"][-1]["body"]["messages"]
                self.assertIn("Пират!", json.dumps(sent, ensure_ascii=False))

                code, out = await api("/v1/chat/completions", {"model": "gpt-5", "messages": [{"role": "user", "content": "x"}]}, key="bender-test-key")
                self.assertEqual(code, 404)
            finally:
                await web.stop()
