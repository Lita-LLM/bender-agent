"""Переезд без изменения источника, подмены плана и исполнения чужого кода."""
import json
import shutil
import sqlite3
from pathlib import Path

from bender.core.app import App
from bender.core.errors import HumanError
from bender.importers import portable
from bender.net.http_server import HttpServer
from tests.helpers import owner_web as create_web, owner_headers
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test
from tests.test_import import tree_hash

FIXTURES = Path(__file__).parent / "fixtures"


def hermes_fixture(target):
    shutil.copytree(FIXTURES / "hermes", target)
    db = sqlite3.connect(target / "state.db")
    db.executescript((target / "state.sql").read_text())
    db.close()
    return target


class MigrationTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.app = App(self.tmp/"data")
        self.source = hermes_fixture(self.tmp/"hermes")

    def tearDown(self):
        self.app.store.close()
        super().tearDown()

    def apply(self, kind="hermes", path=None, only=None):
        plan = portable.make_plan(self.app, kind, path or str(self.source))
        return portable.apply(self.app, plan, plan["hash"], only if only is not None else [i["id"] for i in plan["items"] if i["selected"]])

    def test_preview_selection_idempotence_and_readonly_source(self):
        before = tree_hash(self.source)
        soul = self.app.selfdir.read("SOUL.md")
        plan = portable.make_plan(self.app, "hermes", str(self.source))
        self.assertEqual(tree_hash(self.source), before)
        self.assertEqual(self.app.store.sessions("imported"), [])
        self.assertNotIn("НЕ_НАСТОЯЩИЙ_КЛЮЧ", json.dumps(portable.public(plan), ensure_ascii=False))
        report = self.apply()
        self.assertEqual(report.errors, [])
        self.assertEqual(report.counts["messages"], 2)
        self.assertEqual(len(self.app.heart.jobs.all()), 1)
        self.assertFalse(self.app.heart.jobs.all()[0].enabled)
        self.assertEqual(self.app.heart.jobs.all()[0].authority, [])
        self.assertFalse(self.app.skills.public()["skills"][0]["approved"])
        self.assertEqual(self.app.selfdir.read("SOUL.md"), soul)
        self.assertTrue(Path(report.dir, "before/bender.db").exists())
        providers = self.app.models.providers()
        report2 = self.apply()
        self.assertEqual(report2.done, [])
        self.assertEqual(self.app.models.providers(), providers)
        self.assertEqual(len(self.app.store.sessions("imported")), 1)
        self.assertEqual(tree_hash(self.source), before)
        self.app.index.sync()
        hits = self.app.index.search("ананас")
        self.assertTrue(hits)
        self.assertTrue(all(h.provenance == "untrusted" for h in hits))
        self.assertTrue(all(h.provenance == "untrusted" for h in self.app.index.search("арбуз")))

    def test_changed_source_rejected_then_new_messages_only(self):
        plan = portable.make_plan(self.app, "hermes", str(self.source))
        (self.source/"SOUL.md").write_text("Другая личность")
        changed = portable.make_plan(self.app, "hermes", str(self.source))
        with self.assertRaises(HumanError):
            portable.apply(self.app, changed, plan["hash"], ["note:SOUL.md"])
        self.assertEqual(self.app.store.sessions("imported"), [])
        self.apply()
        sid = self.app.store.sessions("imported")[0]["id"]
        db = sqlite3.connect(self.source/"state.db")
        db.execute("INSERT INTO messages VALUES(4,'session-1','user','Новое сообщение',1700000003,NULL)")
        db.commit(); db.close()
        report = self.apply()
        self.assertEqual(report.counts["messages"], 1)
        self.assertEqual(len(self.app.store.messages(sid)), 3)

    def test_claude_notes_require_selection_and_never_replace_rules(self):
        path = self.tmp/"claude"
        shutil.copytree(FIXTURES/"claude-notes", path)
        before = tree_hash(path)
        rules = self.app.selfdir.read("AGENTS.md")
        plan = portable.make_plan(self.app, "claude", str(path))
        self.assertTrue(plan["items"])
        self.assertFalse(any(i["selected"] for i in plan["items"]))
        self.assertEqual(portable.apply(self.app, plan, plan["hash"], []).done, [])
        self.apply("claude", str(path), ["note:CLAUDE.md"])
        self.app.index.sync()
        self.assertEqual(self.app.selfdir.read("AGENTS.md"), rules)
        self.assertEqual(self.app.mcp.servers(), [])
        self.assertTrue(all(h.provenance == "untrusted" for h in self.app.index.search("мандарин")))
        self.apply("claude", str(path), ["mcp:.mcp.json"])
        self.assertFalse(self.app.mcp.servers()[0]["enabled"])
        self.assertEqual(self.app.mcp.servers()[0].get("env"), {})
        self.assertEqual(tree_hash(path), before)

    def test_disable_limits_symlinks_bad_config_and_database(self):
        self.app.settings.set("migration", {"enabled": False})
        with self.assertRaises(HumanError):
            portable.make_plan(self.app, "hermes", str(self.source))
        self.app.settings.set("migration", {"enabled": True, "messages": 1})
        with self.assertRaises(HumanError):
            portable.make_plan(self.app, "hermes", str(self.source))
        self.app.settings.set("migration", {"messages": 10000})
        soul = self.source/"SOUL.md"
        soul.unlink(); soul.symlink_to(self.tmp/"data/self/SOUL.md")
        with self.assertRaises(HumanError):
            portable.make_plan(self.app, "hermes", str(self.source))
        soul.unlink()
        (self.source/"config.yaml").write_text("model: !python/object:bad {}")
        (self.source/"state.db").write_bytes("не SQLite".encode())
        plan = portable.make_plan(self.app, "hermes", str(self.source))
        self.assertTrue(any("config.yaml" in w for w in plan["warnings"]))
        self.assertTrue(any("state.db" in w for w in plan["warnings"]))
        with self.assertRaises(HumanError):
            portable.make_plan(self.app, "hermes", str(self.app.dirs.local))

    def test_wal_database_readonly_and_existing_owner_model_preserved(self):
        provider = self.app.models.add_provider(name="Своя", base_url="https://owner.invalid/v1")
        self.app.models.set_role("main", [{"provider":provider["id"], "model":"owner"}])
        role = self.app.models.roles()["main"]
        db = sqlite3.connect(self.source/"state.db")
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("INSERT INTO messages VALUES(4,'session-1','user','Живой WAL',1700000004,NULL)")
            db.commit()
            before = tree_hash(self.source)
            report = self.apply()
            self.assertEqual(report.counts["messages"], 3)
            self.assertEqual(tree_hash(self.source), before)
            self.assertEqual(self.app.models.roles()["main"], role)
        finally:
            db.close()

    @async_test
    async def test_http_hash_selection_and_csrf(self):
        server = HttpServer(create_web(self.app, allowed_hosts={"127.0.0.1"}))
        await server.start("127.0.0.1", 0)
        self.app.bus.listen(lambda e: self.app.guard.respond(e["card"]["id"], "once") if e.get("type") == "approval" else None)
        try:
            url = f"http://127.0.0.1:{server.port}/api/import/hermes"
            async def post(path, data, headers=None):
                response = await self.app.http.post(url+path, json=data, headers=owner_headers(**(headers or {"X-Bender":"1"})))
                async with response:
                    value = json.loads(await response.read())
                return response.status, value
            status, plan = await post("/plan", {"path":str(self.source)})
            self.assertEqual(status, 200)
            status, _ = await post("/apply", {"path":str(self.source), "hash":"stale", "items":[]})
            self.assertEqual(status, 409)
            status, report = await post("/apply", {"path":str(self.source), "hash":plan["hash"], "items":["note:memories/USER.md"]})
            self.assertEqual(status, 200, report)
            self.assertEqual(len(report["done"]), 1)
            self.assertEqual(self.app.store.sessions("imported"), [])
            child = self.app.agents.get(report["agents"][0]["id"])
            note = next((child.dirs.diary/"hermes").glob("*.md"))
            result = await self.app.tools.run("read_file", {"path":str(note)}, ToolContext(self.app))
            self.assertFalse(result.ok)
        finally:
            await server.stop()
            await self.app.close()
