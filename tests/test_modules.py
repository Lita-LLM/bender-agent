"""Карантин модулей, MCP, изолированные страницы, обновления, каналы и архивы."""
import io
import json
import shutil
import tarfile
from pathlib import Path
from types import SimpleNamespace

from bender.core.app import App
from bender.core.errors import HumanError
from bender.core.settings import NeedsOwner
from bender.mcp.manager import tool_name
from bender.net.http_server import Headers, HttpServer
from bender.tools.registry import Denied, ToolContext
from tests.helpers import owner_web as create_web, owner_headers
from tests.helpers import TempDirCase, async_test
from tests.test_import import tree_hash

FIXTURE = Path(__file__).parent / "fixtures/module_demo"


class ModulesTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.app = App(self.tmp / "data")
        self.source = self.tmp / "source"
        shutil.copytree(FIXTURE, self.source)
        self.ctx = ToolContext(self.app, session_id="modules")

    def approve_cards(self):
        self.app.bus.listen(lambda event: self.app.guard.respond(event["card"]["id"], "once") if event.get("type") == "approval" else None)

    async def enable(self, *, discover=True):
        self.app.modules.import_path(self.source)
        await self.app.modules.approve("demo", self.app.modules.package("demo")["hash"])
        sid = self.app.modules.trust("demo")["server"]
        if discover:
            self.approve_cards()
            await self.app.mcp.discover(sid, ToolContext(self.app, actor="owner", session_id="module-owner"))
            server = next(s for s in self.app.mcp.public()["servers"] if s["id"] == sid)
            self.app.mcp.approve(sid, server["review"])
        return sid, tool_name(sid, "echo")

    @async_test
    async def test_quarantine_source_unchanged_and_real_mcp(self):
        try:
            before = tree_hash(self.source)
            result = self.app.modules.import_path(self.source)
            self.assertEqual(result["id"], "demo")
            self.assertFalse(self.app.modules.public()["modules"][0]["enabled"])
            self.assertEqual(self.app.mcp.clients, {})
            result = await self.app.tools.run("read_file", {"path": str(self.app.modules.path("demo") / "server.py")}, self.ctx)
            self.assertFalse(result.ok)
            _, alias = await self.enable()
            result = await self.app.tools.run(alias, {"text": "Проверка"}, self.ctx)
            self.assertTrue(result.ok, result.content)
            self.assertTrue(result.tainted)
            self.assertIn("Модуль: Проверка", result.content)
            self.assertEqual(tree_hash(self.source), before)
        finally:
            await self.app.close()
        self.assertEqual(list(self.app.dirs.local.glob("sandbox-*")), [])
        reopened = App(self.tmp / "data")
        try:
            self.assertTrue(reopened.tools.enabled(alias))
            self.assertEqual(reopened.mcp.clients, {})
        finally:
            await reopened.close()

    @async_test
    async def test_changed_file_snapshot_and_connection_cannot_expand_rights(self):
        try:
            sid, alias = await self.enable()
            snapshot = Path(self.app.modules.trust("demo")["snapshot"])
            (snapshot / "index.html").write_text("Подмена снимка")
            self.assertFalse(self.app.tools.enabled(alias))
            self.assertFalse((await self.app.tools.run(alias, {"text": "нет"}, self.ctx)).ok)
            shutil.copyfile(self.source / "index.html", snapshot / "index.html")
            self.assertTrue(self.app.tools.enabled(alias))
            await self.app.mcp.save({**self.app.mcp.get(sid), "network": True, "readonly": False})
            self.assertFalse(self.app.tools.enabled(alias))
            with self.assertRaises(NeedsOwner):
                self.app.settings.set("module_trust", {"demo": {"hash": "подделка"}}, by="agent")
            (self.app.modules.path("demo") / "index.html").write_text("Подмена исходника")
            self.assertFalse(self.app.modules.public()["modules"][0]["approved"])
        finally:
            await self.app.close()

    @async_test
    async def test_candidate_update_needs_exact_review_and_old_stays_active(self):
        try:
            sid, alias = await self.enable()
            old = self.app.modules.package("demo")["hash"]
            manifest = self.source / "bender-module.json"
            data = json.loads(manifest.read_text())
            data.update(version="2.0.0", capabilities=["net.read"])
            manifest.write_text(json.dumps(data))
            report = self.app.modules.import_path(self.source)
            self.assertTrue(report["candidate"])
            self.assertTrue(self.app.tools.enabled(alias))
            self.assertEqual(self.app.modules.package("demo")["hash"], old)
            with self.assertRaises(HumanError):
                await self.app.modules.approve("demo", old, candidate=True)
            candidate = self.app.modules.package("demo", candidate=True)
            await self.app.modules.approve("demo", candidate["hash"], candidate=True)
            self.assertEqual(self.app.modules.package("demo")["metadata"]["version"], "2.0.0")
            self.assertFalse(self.app.tools.enabled(alias))
            self.assertTrue(self.app.mcp.get(sid)["network"])
            self.assertTrue(list(self.app.modules.archive.iterdir()))
        finally:
            await self.app.close()

    @async_test
    async def test_unattended_start_and_disabled_module(self):
        try:
            sid, alias = await self.enable(discover=False)
            with self.assertRaises(Denied):
                await self.app.mcp.discover(sid, ToolContext(self.app, actor="owner", unattended=True))
            self.assertEqual(self.app.mcp.clients, {})
            self.approve_cards()
            await self.app.mcp.discover(sid, self.ctx)
            self.app.mcp.approve(sid, self.app.mcp.public()["servers"][0]["review"])
            await self.app.modules.set_enabled("demo", False)
            self.assertFalse(self.app.tools.enabled(alias))
            with self.assertRaises(NeedsOwner):
                self.app.settings.set("modules", {"disabled": []}, by="agent")
        finally:
            await self.app.close()

    @async_test
    async def test_static_page_headers_and_stale_version(self):
        server = None
        try:
            await self.enable(discover=False)
            package = self.app.modules.package("demo")
            server = HttpServer(create_web(self.app, allowed_hosts={"127.0.0.1"}))
            await server.start("127.0.0.1", 0)
            url = f"http://127.0.0.1:{server.port}/api/modules/demo/page/{package['hash']}/index.html"
            response = await self.app.http.get(url, headers=owner_headers())
            async with response:
                content = await response.read()
            self.assertEqual(response.status, 200)
            self.assertIn("script-src 'none'", response.headers.get("content-security-policy"))
            self.assertIn("sandbox", response.headers.get("content-security-policy"))
            self.assertIn("Пример модуля", content.decode())
            (self.app.modules.path("demo") / "index.html").write_text("Изменено")
            response = await self.app.http.get(url, headers=owner_headers())
            async with response:
                await response.read()
            self.assertEqual(response.status, 409)
        finally:
            if server:
                await server.stop()
            await self.app.close()

    @async_test
    async def test_channels_off_and_outgoing_guard(self):
        try:
            manifest = self.source / "bender-module.json"
            data = json.loads(manifest.read_text())
            data.update(capabilities=["msg.send"])
            data["channels"][0]["send_tool"] = "echo"
            manifest.write_text(json.dumps(data))
            await self.enable(discover=False)
            hook = self.app.modules.add_channel("demo", "events")
            request = SimpleNamespace(headers=Headers([("Authorization", "Bearer " + hook["secret"])]), body=b'{"text":"event"}', query={}, q=lambda name: None)
            status, _ = await self.app.hooks.receive(hook["id"], request)
            self.assertEqual(status, 202)
            await self.app.modules.set_enabled("demo", False)
            status, _ = await self.app.hooks.receive(hook["id"], request)
            self.assertEqual(status, 403)
            await self.app.modules.set_enabled("demo", True)
            with self.assertRaises(Denied):
                await self.app.modules.send("demo", "events", "чужой адресат", "текст", ToolContext(self.app, unattended=True))
            self.approve_cards()
            sid = self.app.modules.trust("demo")["server"]
            await self.app.mcp.discover(sid, self.ctx)
            self.app.mcp.approve(sid, self.app.mcp.public()["servers"][0]["review"])
            result = await self.app.modules.send("demo", "events", "адресат", "исходящий текст", self.ctx)
            self.assertTrue(result.ok, result.content)
            self.assertIn("исходящий текст", result.content)
            await self.app.modules.remove("demo")
            self.assertEqual(self.app.modules.public()["modules"], [])
            self.assertFalse(self.app.hooks.get(hook["id"])["enabled"])
        finally:
            await self.app.close()

    @async_test
    async def test_archives_links_limits_and_manifest_permissions(self):
        try:
            good = self.tmp / "good.tar"
            with tarfile.open(good, "w") as archive:
                for file in self.source.iterdir():
                    archive.add(file, arcname=file.name)
            self.assertEqual(self.app.modules.import_archive(good)["id"], "demo")
            for name, kind in (("../outside", tarfile.REGTYPE), ("C:/outside", tarfile.REGTYPE), ("link", tarfile.SYMTYPE), ("hard", tarfile.LNKTYPE)):
                bad = self.tmp / "bad.tar"
                with tarfile.open(bad, "w") as archive:
                    info = tarfile.TarInfo(name)
                    info.type, info.linkname, info.size = kind, "/etc/passwd", 1 if kind == tarfile.REGTYPE else 0
                    archive.addfile(info, io.BytesIO(b"x"))
                with self.assertRaises(HumanError):
                    self.app.modules.import_archive(bad)
            self.assertFalse((self.app.dirs.work / "outside").exists())
            (self.source / "link").symlink_to(self.source / "server.py")
            with self.assertRaises(HumanError):
                self.app.modules.import_path(self.source)
            (self.source / "link").unlink()
            manifest = self.source / "bender-module.json"
            data = json.loads(manifest.read_text())
            data["capabilities"] = ["settings"]
            manifest.write_text(json.dumps(data))
            with self.assertRaises(HumanError):
                self.app.modules.import_path(self.source)
        finally:
            await self.app.close()

    @async_test
    async def test_declarative_provider_requires_owner_action_and_current_hash(self):
        server = None
        try:
            manifest = self.source / "bender-module.json"
            data = json.loads(manifest.read_text())
            data["providers"] = [{"name": "API примера", "kind": "openai", "base_url": "https://example.org/v1", "api_key": "не использовать из пакета"}]
            manifest.write_text(json.dumps(data))
            await self.enable(discover=False)
            self.assertEqual(self.app.models.providers(), {})
            server = HttpServer(create_web(self.app, allowed_hosts={"127.0.0.1"}))
            await server.start("127.0.0.1", 0)
            url = f"http://127.0.0.1:{server.port}/api/modules/demo/provider"
            for version, expected in (("old", 400), (self.app.modules.package("demo")["hash"], 200)):
                response = await self.app.http.post(url, json={"index": 0, "hash": version}, headers=owner_headers(**{"X-Bender": "1"}))
                async with response:
                    await response.read()
                self.assertEqual(response.status, expected)
            provider = next(iter(self.app.models.providers().values()))
            self.assertEqual(provider["base_url"], "https://example.org/v1")
            self.assertEqual(provider["keys"], [])
        finally:
            if server:
                await server.stop()
            await self.app.close()
