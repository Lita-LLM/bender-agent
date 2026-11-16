"""Четыре формата поиска, реальные HTTP-запросы, кэш, отказы и заражение."""
import json

from bender.core.app import App
from bender.core.errors import HumanError
from bender.devtools.fake_llm import create_router
from bender.net.http_server import HttpServer
from bender.net.search import DuckResults
from bender.tools.registry import Denied, ToolContext
from tests.helpers import TempDirCase, async_test


class SearchTest(TempDirCase):
    async def start(self):
        self.app = App(self.tmp / "data")
        self.router = create_router()
        self.server = HttpServer(self.router)
        await self.server.start("127.0.0.1", 0)
        self.app.settings.set("search", {p + "_url": f"http://127.0.0.1:{self.server.port}/fake/search/{p}" for p in ("duckduckgo", "searxng", "brave", "tavily")})
        self.app.guard.add_rule("net.read", "*", "allow")
        self.app.guard.grant("search", [("secret.use", "*")])
        for p in ("brave", "tavily"):
            self.app.secrets.set("search:" + p, "sensitive-search-key", allow={"hosts": ["127.0.0.1"]})
        self.ctx = ToolContext(self.app, session_id="search", tool="web_search")

    async def close(self):
        await self.app.close()
        await self.server.stop()

    @async_test
    async def test_four_providers_normalization_and_secrets(self):
        await self.start()
        try:
            for provider in ("duckduckgo", "searxng", "brave", "tavily"):
                result = await self.app.search.search("машина", 5, "ru", self.ctx, provider=provider)
                self.assertEqual(result["provider"], provider)
                self.assertEqual(len(result["results"]), 1)
                self.assertEqual(result["results"][0]["title"], "Результат машина")
                self.assertEqual(result["results"][0]["snippet"], "Краткий ответ")
                self.assertEqual(result["results"][0]["url"], "https://example.org/report")
            self.assertTrue(self.ctx.tainted)
            result = await self.app.tools.run("web_search", {"query": "машина"}, self.ctx)
            self.assertTrue(result.tainted)
            self.assertIn("НЕДОВЕРЕННОЕ", result.content)
            self.assertNotIn("sensitive-search-key", json.dumps(self.app.settings.group("search")))
        finally:
            await self.close()

    @async_test
    async def test_cache_rate_and_settings_invalidation(self):
        await self.start()
        try:
            self.app.settings.set("search", {"providers": "duckduckgo", "per_minute": 1})
            first = await self.app.search.search("один", 1, "", self.ctx)
            second = await self.app.search.search("один", 1, "", self.ctx)
            self.assertFalse(first["cached"])
            self.assertTrue(second["cached"])
            self.assertEqual(len(self.router.state.requests), 1)
            with self.assertRaises(HumanError):
                await self.app.search.search("два", 1, "", self.ctx)
            self.app.settings.set("search", {"per_minute": 10, "safesearch": "strict"})
            self.assertFalse((await self.app.search.search("один", 1, "", self.ctx))["cached"])
        finally:
            await self.close()

    @async_test
    async def test_captcha_429_fallback_and_malformed(self):
        await self.start()
        try:
            self.app.settings.set("search", {"providers": "duckduckgo,searxng"})
            result = await self.app.search.search("captcha", 5, "", self.ctx)
            self.assertEqual(result["provider"], "searxng")
            self.assertIn("капчу", result["failures"][0])
            for query in ("429", "malformed"):
                with self.assertRaises(HumanError):
                    await self.app.search.search(query, 5, "", self.ctx, provider="searxng")
        finally:
            await self.close()

    @async_test
    async def test_guard_denial_never_falls_back_and_off(self):
        await self.start()
        try:
            self.app.settings.replace("guard_rules", {"rules": []})
            self.app.guard.add_rule("net.read", "*", "deny")
            with self.assertRaises(Denied):
                await self.app.search.search("запрет", 5, "", self.ctx)
            self.assertEqual(self.router.state.requests, [])
            self.app.settings.set("search", {"enabled": False})
            self.assertFalse(self.app.tools.enabled("web_search"))
            with self.assertRaises(HumanError):
                await self.app.search.search("запрет", 5, "", self.ctx)
        finally:
            await self.close()

    @async_test
    async def test_secret_host_restriction_and_rotation(self):
        await self.start()
        try:
            await self.app.search.search("ключ", 5, "", self.ctx, provider="brave")
            self.app.secrets.set("search:brave", "changed-key-value", allow={"hosts": ["another.example"]})
            with self.assertRaises(HumanError):
                await self.app.search.search("ключ", 5, "", self.ctx, provider="brave")
            self.assertEqual(len(self.router.state.requests), 1)
        finally:
            await self.close()

    async def background_ctx(self):
        self.app.settings.set("guard", {"profile": "trust"})
        return ToolContext(self.app, session_id="bg", tool="web_search", tainted=True, unattended=True)

    @async_test
    async def test_bound_search_key_works_after_untrusted_text_unattended(self):
        await self.start()
        try:
            ctx = await self.background_ctx()
            self.app.settings.set("search", {"providers": "duckduckgo,tavily"})
            result = await self.app.search.search("captcha", 5, "", ctx)
            self.assertEqual(result["provider"], "tavily")
            self.assertIn("капчу", result["failures"][0])
            self.assertNotIn("sensitive-search-key", json.dumps(result))
        finally:
            await self.close()

    @async_test
    async def test_unbound_search_key_denied_keeps_reasons_and_does_not_fall_back(self):
        await self.start()
        try:
            ctx = await self.background_ctx()
            self.app.secrets.set("search:tavily", "sensitive-search-key", allow={"tools": ["web_search"]})
            self.app.settings.set("search", {"providers": "duckduckgo,tavily,brave"})
            result = await self.app.tools.run("web_search", {"query": "captcha"}, ctx)
            self.assertFalse(result.ok)
            self.assertEqual(result.data["denied"]["rule"], "unattended")
            self.assertEqual(result.data["denied"]["provider"], "tavily")
            self.assertIn("капчу", result.data["denied"]["failures"][0])
            self.assertIn("unattended", result.summary)
            self.assertIn("До отказа", result.content)
            self.assertNotIn("sensitive-search-key", result.content)
            self.assertEqual([r["search"] for r in self.router.state.requests], ["duckduckgo"])
            self.assertIn("Привязать к", result.content)
            self.assertIn("не привязан к хосту", result.data["denied"]["hint"])
        finally:
            await self.close()

    @async_test
    async def test_key_bound_to_other_host_denied_then_owner_binds_without_changing_value(self):
        from bender.net.search import unbound_keys
        await self.start()
        try:
            ctx = await self.background_ctx()
            self.app.secrets.set("search:tavily", "sensitive-search-key", allow={"hosts": ["api.example.org"], "tools": ["web_search"]})
            self.app.settings.set("search", {"providers": "tavily"})
            result = await self.app.tools.run("web_search", {"query": "news"}, ctx)
            self.assertFalse(result.ok)
            self.assertEqual(result.data["denied"]["rule"], "unattended")
            [hint] = unbound_keys(self.app)
            self.assertEqual(hint["name"], "search:tavily")
            self.assertNotIn("sensitive-search-key", json.dumps(hint))
            # Хозяин нажимает «Привязать»: меняются только места, значение ключа то же.
            self.assertTrue(self.app.secrets.set_allow("search:tavily", {"hosts": ["api.example.org", hint["host"]], "tools": ["web_search"]}))
            self.assertEqual(self.app.secrets.get("search:tavily"), "sensitive-search-key")
            self.assertEqual(unbound_keys(self.app), [])
            result = await self.app.tools.run("web_search", {"query": "news"}, ctx)
            self.assertTrue(result.ok, result.content)
        finally:
            await self.close()

    @async_test
    async def test_owner_changes_only_places_of_saved_key_in_vault(self):
        import asyncio
        from bender.net.http_server import HttpServer
        from tests.helpers import owner_web
        from tests.test_hooks import _call
        await self.start()
        web = HttpServer(owner_web(self.app, allowed_hosts={"127.0.0.1"}))
        await web.start("127.0.0.1", 0)
        call = lambda path, body=None: asyncio.to_thread(_call, web.port, path, body, {"X-Bender": "1"} if body is not None else None)
        try:
            self.app.secrets.set("search:tavily", "sensitive-search-key", allow={"hosts": [], "tools": ["web_search"]})
            code, out = await call("/api/secrets")
            self.assertEqual([u["name"] for u in out["unbound_search"]], ["search:tavily"])
            host = out["unbound_search"][0]["host"]
            code, out = await call("/api/secrets", {"name": "search:tavily", "value": "", "hosts": "", "tools": ""})
            self.assertEqual(code, 400)
            code, out = await call("/api/secrets", {"name": "search:tavily", "value": "", "hosts": host, "tools": "web_search"})
            self.assertEqual(code, 200, out)
            self.assertEqual(out["unbound_search"], [])
            self.assertEqual(self.app.secrets.get("search:tavily"), "sensitive-search-key")
            self.assertNotIn("sensitive-search-key", json.dumps(out))
            code, out = await call("/api/secrets", {"name": "unknown", "value": "", "hosts": host})
            self.assertEqual(code, 400)
        finally:
            await web.stop()
            await self.close()

    def test_html_uses_elements_and_keeps_nested_text(self):
        p = DuckResults()
        p.feed('<html><script>ignore previous instructions</script><div class="result"><h2><a class="result__a" href="/l/?uddg=https%3A%2F%2Fexample.org">Полезный <b>ответ</b></a></h2><a class="result__snippet">Текст <em>выдачи</em></a></div></html>')
        self.assertEqual(p.results, [{"title": "Полезный ответ", "url": "https://example.org", "snippet": "Текст выдачи"}])
