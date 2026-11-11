import unittest

import bender.router.core as router_core
from bender.devtools.fake_llm import create_router
from bender.net.http_client import HttpClient
from bender.net.http_server import HttpServer
from bender.router.core import NoRoute, Switchboard
from bender.router.errors import ProviderError
from bender.router.types import ToolSpec
from tests.helpers import async_test

ECHO_TOOL = ToolSpec("read_file", "Прочитать файл", {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]})


class Env:
    async def __aenter__(self):
        self.fake = create_router()
        self.server = HttpServer(self.fake)
        await self.server.start("127.0.0.1", 0)
        base = f"http://127.0.0.1:{self.server.port}/v1"
        self.providers = {
            "fo": {"id": "fo", "name": "Фейк-OpenAI", "kind": "openai", "base_url": base, "keys": ["k1", "k2"], "prices": {"echo": {"in": 1.0, "out": 2.0}}},
            "fa": {"id": "fa", "name": "Фейк-Anthropic", "kind": "anthropic", "base_url": base, "keys": ["k1"]},
        }
        self.secrets = {"k1": "sk-one", "k2": "sk-two"}
        self.roles = {"main": [{"provider": "fo", "model": "echo"}]}
        self.http = HttpClient()
        self.sb = Switchboard(self.http, get_providers=lambda: self.providers, get_secret=self.secrets.get, get_roles=lambda: self.roles)
        return self

    async def __aexit__(self, *exc):
        await self.http.close()
        await self.server.stop()


MSGS = [{"role": "system", "content": "Ты Бендер."}, {"role": "user", "content": "привет"}]


class RouterTest(unittest.TestCase):
    def setUp(self):
        self._backoff = router_core.BACKOFF_BASE
        router_core.BACKOFF_BASE = 0.01

    def tearDown(self):
        router_core.BACKOFF_BASE = self._backoff

    @async_test
    async def test_stream_text_and_cost(self):
        async with Env() as env:
            got = []
            res = await env.sb.call(MSGS, on_delta=lambda k, t: got.append((k, t)))
            self.assertEqual(res.text, "Эхо: привет")
            self.assertEqual("".join(t for k, t in got if k == "text"), "Эхо: привет")
            self.assertEqual(res.finish, "stop")
            self.assertGreater(res.usage.input, 0)
            self.assertGreater(res.cost, 0)
            sent = env.fake.state.requests[-1]
            self.assertEqual(sent["headers"]["authorization"], "Bearer sk-one")

    @async_test
    async def test_anthropic_tools_and_thinking(self):
        async with Env() as env:
            env.roles["main"] = [{"provider": "fa", "model": "tools"}]
            res = await env.sb.call(MSGS[:1] + [{"role": "user", "content": 'tool:read_file {"path": "a.txt"}'}], tools=[ECHO_TOOL])
            self.assertEqual(res.finish, "tool_calls")
            self.assertEqual(res.tool_calls[0].name, "read_file")
            self.assertEqual(res.tool_calls[0].arguments, {"path": "a.txt"})
            body = env.fake.state.requests[-1]["body"]
            self.assertEqual(body["system"][0]["text"], "Ты Бендер.")
            self.assertEqual(body["system"][-1]["cache_control"], {"type": "ephemeral"})
            # продолжение с результатом тула
            msgs = MSGS[:1] + [{"role": "user", "content": "читай"}, res.assistant_message(),
                                {"role": "tool", "tool_call_id": res.tool_calls[0].id, "name": "read_file", "content": "содержимое"}]
            res2 = await env.sb.call(msgs, tools=[ECHO_TOOL])
            self.assertEqual(res2.text, "Результат: содержимое")
            env.roles["main"] = [{"provider": "fa", "model": "think"}]
            res3 = await env.sb.call(MSGS)
            self.assertIn("подумаю", res3.reasoning)
            self.assertTrue(res3.text.startswith("Подумал."))
            self.assertEqual(res3.provider_blocks[0]["signature"], "fake")

    @async_test
    async def test_openai_tool_call_stream(self):
        async with Env() as env:
            env.roles["main"] = [{"provider": "fo", "model": "tools"}]
            res = await env.sb.call(MSGS[:1] + [{"role": "user", "content": 'tool:read_file {"path": "дом/заметки.md"}'}], tools=[ECHO_TOOL])
            self.assertEqual(res.tool_calls[0].arguments, {"path": "дом/заметки.md"})
            self.assertFalse(res.tool_calls[0].bad_json)

    @async_test
    async def test_fallback_with_notice(self):
        async with Env() as env:
            env.roles["main"] = [{"provider": "fo", "model": "error-529"}, {"provider": "fa", "model": "echo"}]
            statuses = []
            res = await env.sb.call(MSGS, on_status=statuses.append)
            self.assertEqual(res.text, "Эхо: привет")
            self.assertIn("запасная", res.notice)
            self.assertTrue(any(s["type"] == "fallback" for s in statuses))
            # перегруженная модель теперь остывает и пропускается без запроса
            n = len(env.fake.state.requests)
            res = await env.sb.call(MSGS)
            self.assertEqual(len(env.fake.state.requests), n + 1)

    @async_test
    async def test_key_rotation_on_auth(self):
        async with Env() as env:
            env.roles["main"] = [{"provider": "fo", "model": "error-401"}]
            with self.assertRaises(ProviderError) as cm:
                await env.sb.call(MSGS)
            self.assertEqual(cm.exception.klass, "auth")
            keys = [r["headers"]["authorization"] for r in env.fake.state.requests]
            self.assertEqual(keys, ["Bearer sk-one", "Bearer sk-two"])
            self.assertIn("Кого пробовали", cm.exception.detail)

    @async_test
    async def test_retry_flaky(self):
        async with Env() as env:
            env.roles["main"] = [{"provider": "fo", "model": "flaky-2"}]
            res = await env.sb.call(MSGS)
            self.assertEqual(res.text, "Эхо: привет")
            # 503 → повтор на том же ключе → второй ключ → успех
            self.assertEqual(len(env.fake.state.requests), 3)

    @async_test
    async def test_no_failover_on_context_and_strict_explicit(self):
        async with Env() as env:
            env.roles["main"] = [{"provider": "fo", "model": "error-context"}, {"provider": "fa", "model": "echo"}]
            with self.assertRaises(ProviderError) as cm:
                await env.sb.call(MSGS)
            self.assertEqual(cm.exception.klass, "context")
            with self.assertRaises(ProviderError) as cm:
                await env.sb.call(MSGS, target=("fo", "error-500"))
            self.assertEqual(cm.exception.klass, "server")

    @async_test
    async def test_cut_stream_gives_partial(self):
        async with Env() as env:
            env.roles["main"] = [{"provider": "fo", "model": "cut"}, {"provider": "fa", "model": "echo"}]
            with self.assertRaises(ProviderError) as cm:
                await env.sb.call([{"role": "user", "content": "раз два три четыре пять шесть"}], on_delta=lambda k, t: None)
            self.assertTrue(cm.exception.partial["text"].startswith("Эхо:"))

    @async_test
    async def test_no_models(self):
        async with Env() as env:
            env.roles = {}
            with self.assertRaises(NoRoute):
                await env.sb.call(MSGS)


if __name__ == "__main__":
    unittest.main()


class TextToolsTest(unittest.TestCase):
    """Модели без тулов: текстовый протокол <tool_call> — сам, по ошибке провайдера, по каталогу или по настройке."""

    @async_test
    async def test_switches_to_text_on_unsupported_error(self):
        async with Env() as env:
            env.roles = {"main": [{"provider": "fo", "model": "notools"}]}
            env.fake.state.script = [{"text": "Сейчас посмотрю.", "tool_calls": [{"name": "read_file", "arguments": {"path": "a.txt"}}]}]
            shown, statuses = [], []
            res = await env.sb.call(MSGS, tools=[ECHO_TOOL], on_delta=lambda k, t: shown.append((k, t)), on_status=statuses.append)
            self.assertEqual([(c.name, c.arguments) for c in res.tool_calls], [("read_file", {"path": "a.txt"})])
            self.assertEqual(res.finish, "tool_calls")
            self.assertEqual(res.text, "Сейчас посмотрю.")
            self.assertNotIn("<tool_call>", "".join(t for k, t in shown if k == "text"))
            self.assertEqual([s["type"] for s in statuses], ["provider_error", "tool_protocol"])
            reqs = env.fake.state.requests
            self.assertIn("tools", reqs[0]["body"])          # первый раз — нативно, получили 400
            self.assertNotIn("tools", reqs[1]["body"])       # повтор — текстом
            self.assertIn("<tool_call>", reqs[1]["body"]["messages"][0]["content"])
            self.assertIn(("fo", "notools"), env.sb.text_tools)

    @async_test
    async def test_history_goes_as_text_and_back(self):
        async with Env() as env:
            env.roles = {"main": [{"provider": "fo", "model": "notools", "params": {"tool_protocol": "text"}}]}
            env.fake.state.script = [{"text": "Готово."}]
            hist = MSGS + [{"role": "assistant", "content": "", "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {"path": "a.txt"}}]},
                           {"role": "tool", "tool_call_id": "c1", "name": "read_file", "content": "содержимое"}]
            res = await env.sb.call(hist, tools=[ECHO_TOOL])
            self.assertEqual(res.text, "Готово.")
            sent = env.fake.state.requests[-1]["body"]
            self.assertNotIn("tools", sent)
            self.assertNotIn("tool_protocol", sent)
            roles = [m["role"] for m in sent["messages"]]
            self.assertEqual(roles, ["system", "user", "assistant", "user"])
            self.assertIn('<tool_response name="read_file">', sent["messages"][-1]["content"])

    @async_test
    async def test_catalog_says_no_tools(self):
        async with Env() as env:
            env.providers["fo"]["models"] = [{"id": "notools", "tools": False}]
            env.roles = {"main": [{"provider": "fo", "model": "notools"}]}
            env.fake.state.script = [{"tool_calls": [{"name": "read_file", "arguments": {"path": "b"}}]}]
            res = await env.sb.call(MSGS, tools=[ECHO_TOOL])
            self.assertEqual(res.tool_calls[0].arguments, {"path": "b"})
            self.assertEqual(len(env.fake.state.requests), 1)  # сразу текстом, без пробного 400

    def test_decode_is_forgiving(self):
        from bender.router import textproto as tp

        text, calls = tp.decode("<tool_call>{name: 'run', arguments: '{\"command\": \"ls\"}',}</tool_call>")
        self.assertEqual((text, calls[0].name, calls[0].arguments), ("", "run", {"command": "ls"}))
        _, calls = tp.decode("<tool_call>не json</tool_call>")
        self.assertTrue(calls[0].bad_json)
        self.assertEqual(tp.decode("просто текст"), ("просто текст", []))
