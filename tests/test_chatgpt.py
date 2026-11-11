"""Вход через ChatGPT и модели по подписке — на фальшивом OpenAI (bender/devtools/fake_llm.py).

Первый шаг входа по коду проверен и на настоящем auth.openai.com: формат тот же.
"""

import asyncio
import json
import socket
import time

from bender.core.app import App
from bender.devtools.fake_llm import create_router
from bender.net.http_client import HttpClient
from bender.net.http_server import HttpServer
from bender.router import chatgpt
from bender.router.errors import ProviderError
from bender.router.types import ToolSpec
from tests.helpers import TempDirCase, async_test
from tests.test_mind import call as script_call

READ = ToolSpec("read_file", "Прочитать файл", {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})
MSGS = [{"role": "system", "content": "Ты Бендер."}, {"role": "user", "content": "привет"}]


async def nosleep(_):
    await asyncio.sleep(0)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Env:
    def __init__(self, tmp):
        self.tmp = tmp

    async def __aenter__(self):
        self.fake = create_router()
        self.server = HttpServer(self.fake)
        await self.server.start("127.0.0.1", 0)
        self.base = f"http://127.0.0.1:{self.server.port}"
        self.http = HttpClient()
        self.app = App(self.tmp / "data")
        self.app.bus.bind_loop(asyncio.get_running_loop())
        return self

    async def login(self, **override) -> dict:
        device = await chatgpt.device_start(self.http, self.base)
        tokens = await chatgpt.device_wait(self.http, device, self.base, sleep=nosleep)
        tokens.update(override)
        self.provider = self.app.models.connect_chatgpt(tokens, auth_url=self.base, base_url=self.base + "/backend-api/codex")
        return tokens

    def use(self, model: str) -> None:
        self.app.models.set_role("main", [{"provider": self.provider["id"], "model": model}])

    def tokens(self) -> dict:
        return chatgpt.unpack(self.app.secrets.get(self.provider["keys"][0]))

    async def __aexit__(self, *exc):
        await self.app.close()
        await self.http.close()
        await self.server.stop()


class ChatGPTTest(TempDirCase):
    @async_test
    async def test_device_login_connects_subscription(self):
        async with Env(self.tmp) as env:
            device = await chatgpt.device_start(env.http, env.base)
            self.assertEqual(device["user_code"], "FAKE-CODE1")
            self.assertEqual(device["verification_url"], env.base + "/codex/device")
            self.assertIsNone(await chatgpt.device_poll(env.http, device, env.base))  # хозяин ещё не ввёл код
            tokens = await chatgpt.device_wait(env.http, device, env.base, sleep=nosleep)
            self.assertEqual((tokens["account_id"], tokens["email"]), ("acc_fake", "owner@example.com"))
            self.assertGreater(tokens["expires"], time.time() + 3000)
            oauth = [r for r in env.fake.state.requests if r["api"] == "oauth"][-1]
            self.assertEqual(oauth["body"]["redirect_uri"], env.base + "/deviceauth/callback")
            self.assertEqual(oauth["headers"]["originator"], "bender")
            p = env.app.models.connect_chatgpt(tokens, auth_url=env.base, base_url=env.base + "/backend-api/codex")
            self.assertEqual(p["kind"], "chatgpt")
            self.assertTrue(p["subscription"])
            self.assertEqual(env.app.models.roles()["main"][0]["model"], chatgpt.DEFAULT_MODEL)
            pub = [x for x in env.app.models.public_providers() if x["id"] == p["id"]][0]
            self.assertEqual(pub["keys"][0]["masked"], "вход owner@example.com")
            self.assertNotIn(tokens["access"], json.dumps(pub))
            # повторный вход того же аккаунта — тот же провайдер, новый токен
            again = dict(tokens, access=tokens["access"] + "x")
            p2 = env.app.models.connect_chatgpt(again)
            self.assertEqual(p2["id"], p["id"])
            self.assertEqual(len(env.app.models.providers()), 1)

    @async_test
    async def test_call_goes_to_responses_api(self):
        async with Env(self.tmp) as env:
            tokens = await env.login()
            env.use("echo")
            res = await env.app.router.call(MSGS)
            self.assertEqual(res.text, "Эхо: привет")
            self.assertEqual(res.cost, 0.0)  # подписка
            req = [r for r in env.fake.state.requests if r["api"] == "chatgpt"][-1]
            h, b = req["headers"], req["body"]
            self.assertEqual(h["authorization"], f"Bearer {tokens['access']}")
            self.assertEqual((h["chatgpt-account-id"], h["originator"], h["openai-beta"]), ("acc_fake", "bender", "responses=experimental"))
            self.assertTrue(h["user-agent"].startswith("bender/"))
            self.assertEqual((b["store"], b["stream"], b["instructions"]), (False, True, "Ты Бендер."))
            self.assertEqual(b["input"], [{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "привет"}]}])
            self.assertEqual(len(b["prompt_cache_key"]), 32)

    def test_compaction_note_does_not_change_instructions(self):
        # пересказ после сжатия меняется — в instructions он менял бы ключ кэша и весь промпт
        msgs = [{"role": "system", "content": "Ты Бендер."}, {"role": "system", "content": "[Пересказ начала разговора: …]"},
                {"role": "user", "content": "привет"}]
        instructions, items = chatgpt.to_input(msgs, "p")
        self.assertEqual(instructions, "Ты Бендер.")
        self.assertEqual(items[0], {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "[Пересказ начала разговора: …]"}]})
        self.assertEqual(items[1]["role"], "user")

    @async_test
    async def test_session_header_is_stable_per_conversation(self):
        # случайный session_id на каждый запрос сервер считал новой сессией — кэш начала запроса почти не срабатывал
        from types import SimpleNamespace
        from bender.router.cli import TURN
        async with Env(self.tmp) as env:
            await env.login()
            env.use("echo")

            async def sent(sid=None):
                token = TURN.set(SimpleNamespace(session_id=sid) if sid else None)
                try:
                    await env.app.router.call(MSGS)
                finally:
                    TURN.reset(token)
                return [r for r in env.fake.state.requests if r["api"] == "chatgpt"][-1]["headers"]["session_id"]

            first, again = await sent("s_один"), await sent("s_один")
            self.assertEqual(first, again)
            self.assertNotEqual(first, await sent("s_другой"))
            self.assertNotIn("s_один", first)  # наружу уходит хеш, не id сессии
            self.assertEqual(await sent(), await sent())  # вне хода — по системному промпту, тоже постоянный

    @async_test
    async def test_tools_and_reasoning_round_trip(self):
        async with Env(self.tmp) as env:
            await env.login()
            env.use("script")
            env.fake.state.script = [{"reasoning": "надо прочитать", "tool_calls": [{"name": "read_file", "arguments": {"path": "a.txt"}}]}, {"text": "В файле — привет."}]
            res = await env.app.router.call(MSGS, tools=[READ])
            self.assertEqual([(c.name, c.arguments) for c in res.tool_calls], [("read_file", {"path": "a.txt"})])
            self.assertEqual(res.finish, "tool_calls")
            self.assertEqual(res.reasoning, "надо прочитать")
            msg = res.assistant_message()
            self.assertEqual(msg["provider_blocks"][0]["encrypted_content"][:4], "enc-")
            hist = MSGS + [msg, {"role": "tool", "tool_call_id": res.tool_calls[0].id, "name": "read_file", "content": "привет"}]
            res2 = await env.app.router.call(hist, tools=[READ])
            self.assertEqual(res2.text, "В файле — привет.")
            sent = [r for r in env.fake.state.requests if r["api"] == "chatgpt"][-1]["body"]
            kinds = [i["type"] for i in sent["input"]]
            self.assertEqual(kinds, ["message", "reasoning", "function_call", "function_call_output"])  # размышления вернулись как есть
            self.assertEqual(sent["tools"][0], {"type": "function", "name": "read_file", "description": "Прочитать файл", "parameters": READ.parameters, "strict": False})

    @async_test
    async def test_refresh_before_expiry(self):
        async with Env(self.tmp) as env:
            old = await env.login(expires=time.time() - 5)
            env.use("echo")
            await env.app.router.call(MSGS)
            self.assertEqual(env.fake.state.refreshes, 1)
            now = env.tokens()
            self.assertNotEqual(now["access"], old["access"])
            self.assertNotEqual(now["refresh"], old["refresh"])  # одноразовый refresh-токен заменён новым
            await env.app.router.call(MSGS)
            self.assertEqual(env.fake.state.refreshes, 1)  # второй раз — свежий токен, без обновления

    @async_test
    async def test_two_turns_refresh_once(self):
        async with Env(self.tmp) as env:
            await env.login(expires=time.time() - 5)
            env.use("echo")
            await asyncio.gather(env.app.router.call(MSGS), env.app.router.call(MSGS))
            self.assertEqual(env.fake.state.refreshes, 1)

    @async_test
    async def test_revoked_access_refreshes_and_retries(self):
        async with Env(self.tmp) as env:
            tokens = await env.login()
            env.use("echo")
            env.fake.state.valid_access.discard(tokens["access"])  # OpenAI отозвал access раньше срока
            res = await env.app.router.call(MSGS)
            self.assertEqual(res.text, "Эхо: привет")
            self.assertEqual(env.fake.state.refreshes, 1)

    @async_test
    async def test_dead_login_asks_to_sign_in_again(self):
        async with Env(self.tmp) as env:
            await env.login(expires=time.time() - 5, refresh="revoked")
            env.use("echo")
            with self.assertRaises(ProviderError) as cm:
                await env.app.router.call(MSGS)
            self.assertEqual(cm.exception.klass, "auth")
            self.assertIn("Войди заново", cm.exception.message)

    @async_test
    async def test_usage_limit_is_rate_limit_with_reset(self):
        async with Env(self.tmp) as env:
            await env.login()
            with self.assertRaises(ProviderError) as cm:
                await env.app.router.call(MSGS, target=(env.provider["id"], "error-429"))
            self.assertEqual(cm.exception.klass, "rate_limit")
            self.assertEqual(cm.exception.retry_after, 3600)

    @async_test
    async def test_browser_login(self):
        async with Env(self.tmp) as env:
            port = free_port()
            flow = chatgpt.BrowserLogin(env.http, env.base, port=port)
            await flow.start()
            self.assertTrue(flow.listening)
            self.assertIn("code_challenge_method=S256", flow.url)
            self.assertIn("originator=bender", flow.url)
            self.assertIn(f"redirect_uri=http%3A%2F%2Flocalhost%3A{port}%2Fauth%2Fcallback", flow.url)
            waiter = asyncio.ensure_future(flow.wait(timeout=10))
            resp = await env.http.request("GET", f"http://127.0.0.1:{port}/auth/callback?code=fake-auth-code&state={flow.state}")
            async with resp:
                self.assertIn("Вход выполнен", await resp.text())
            tokens = await waiter
            self.assertEqual(tokens["account_id"], "acc_fake")
            self.assertIsNone(flow.server)  # порт отпущен

    @async_test
    async def test_browser_login_by_paste(self):
        async with Env(self.tmp) as env:
            flow = chatgpt.BrowserLogin(env.http, env.base, port=free_port())
            self.assertFalse(flow.paste(f"http://localhost:1455/auth/callback?code=x&state=чужой"))
            self.assertTrue(flow.paste(f"http://localhost:1455/auth/callback?code=fake-auth-code&state={flow.state}"))
            tokens = await flow.wait(timeout=5)
            self.assertEqual(tokens["email"], "owner@example.com")

    def test_parse_callback(self):
        self.assertEqual(chatgpt.parse_callback("http://localhost:1455/auth/callback?code=abc&state=s1"), ("abc", "s1"))
        self.assertEqual(chatgpt.parse_callback("?code=abc&state=s1"), ("abc", "s1"))
        self.assertEqual(chatgpt.parse_callback("abc#s1"), ("abc", "s1"))
        self.assertEqual(chatgpt.parse_callback("  abc "), ("abc", None))
        self.assertEqual(chatgpt.parse_callback(""), (None, None))

    @async_test
    async def test_agent_turn_on_subscription(self):
        async with Env(self.tmp) as env:
            await env.login()
            env.use("script")
            env.app.settings.set("guard", {"profile": "normal"})
            env.fake.state.script = [script_call("write_file", path="через-подписку.txt", content="ok"), {"text": "Записал."}]
            res = await env.app.mind.say(env.app.main_session(), "запиши")
            self.assertIsNone(res["error"])
            self.assertEqual((env.app.dirs.work / "через-подписку.txt").read_text(encoding="utf-8"), "ok")
