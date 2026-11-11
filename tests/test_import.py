"""Переезд из OpenClaw — на настоящем состоянии OpenClaw 2026.10.5 (tests/fixtures, секреты вычищены)."""

import asyncio
import hashlib
import json
import os
import shutil
import sqlite3
import unittest
from pathlib import Path

from bender.core import json5
from bender.core.app import App
from bender.importers import openclaw as oc
from bender.mind.context import system_prompt
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv, call

FIXTURE = Path(__file__).parent / "fixtures" / "openclaw-2026.10.5" / ".openclaw"


def tree_hash(root: Path) -> dict:
    return {str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob("*")) if p.is_file()}


class JSON5Test(unittest.TestCase):
    def test_json5_features(self):
        text = """// конфиг
        {
          agents: { defaults: { model: 'openai/gpt-5', }, },  /* висячие запятые */
          hex: 0x1F, half: .5, pos: +3, big: 1e3, inf: -Infinity,
          "quoted key": "a\\
b",
          list: [1, 2, 3,],
          $dollar_key: null, yes: true,
        }"""
        v = json5.loads(text)
        self.assertEqual(v["agents"]["defaults"]["model"], "openai/gpt-5")
        self.assertEqual((v["hex"], v["half"], v["pos"], v["big"]), (31, 0.5, 3, 1000.0))
        self.assertEqual(v["inf"], float("-inf"))
        self.assertEqual(v["quoted key"], "ab")
        self.assertEqual(v["list"], [1, 2, 3])
        self.assertIsNone(v["$dollar_key"])
        self.assertTrue(v["yes"])
        self.assertEqual(json5.loads('"\\u0410\\ud83e\\udd16"'), "А🤖")

    def test_json5_errors_have_position(self):
        with self.assertRaises(json5.JSON5Error) as cm:
            json5.loads("{\n  a: 1\n  b: 2\n}")
        self.assertEqual(cm.exception.line, 3)
        with self.assertRaises(ValueError):
            json5.loads("{a: 1} лишнее")

    def test_plain_json_is_json5(self):
        data = json.loads((FIXTURE / "openclaw.json").read_text(encoding="utf-8"))
        self.assertEqual(json5.loads((FIXTURE / "openclaw.json").read_text(encoding="utf-8")), data)


class ConvertTest(unittest.TestCase):
    def test_events(self):
        ev = lambda role, content, **kw: {"type": "message", "id": "x", "message": {"role": role, "content": content, "timestamp": 1792626224940, **kw}}
        m = oc.convert_event(ev("assistant", [{"type": "thinking", "thinking": "хм"}, {"type": "text", "text": "Готово"},
                                              {"type": "toolCall", "name": "exec", "arguments": {"cmd": "ls"}}]))
        self.assertEqual(m["role"], "assistant")
        self.assertIn("Готово", m["content"])
        self.assertIn('⚙️ exec({"cmd": "ls"})', m["content"])
        self.assertNotIn("хм", m["content"])
        self.assertAlmostEqual(m["ts"], 1792626224.94)
        self.assertEqual(oc.convert_event(ev("assistant", "HEARTBEAT_OK")), {"skip": "heartbeat"})
        self.assertEqual(oc.convert_event(ev("user", "Read HEARTBEAT.md if it exists")), {"skip": "heartbeat"})
        self.assertIsNone(oc.convert_event(ev("toolResult", "вывод")))
        self.assertIsNone(oc.convert_event({"type": "model_change"}))

    def test_meaningful(self):
        self.assertFalse(oc.meaningful("# IDENTITY.md\n\n- **Name:**\n- **Emoji:** _(pick one)_\n<!-- comment -->\n"))
        self.assertTrue(oc.meaningful("# SOUL\n\nSarcastic."))

    def test_add_to_section(self):
        text = "# A\n\n## Окружение\n_заметки_\n\n## Потом\nx\n"
        out = oc.add_to_section(text, "Окружение", "- SSH: homepc")
        self.assertLess(out.index("- SSH: homepc"), out.index("## Потом"))
        self.assertIn("## Новое\n\nz", oc.add_to_section("# A\n", "Новое", "z"))


class ImportCase(TempDirCase):
    def setUp(self):
        super().setUp()
        self.src = self.tmp / "oc"
        shutil.copytree(FIXTURE, self.src)
        self.app = App(self.tmp / "data")

    def tearDown(self):
        asyncio.run(self.app.close())
        super().tearDown()

    def plan(self, **kw):
        return oc.make_plan(self.app, str(self.src), **kw)


class ImportTest(ImportCase):
    def test_source_is_never_touched(self):
        before = tree_hash(self.src)
        oc.apply(self.plan(), self.app)
        self.assertEqual(tree_hash(self.src), before)

    @async_test
    async def test_work_files_become_untrusted_attachments(self):
        plan=self.plan()
        item=next(i for i in plan.items if i.kind=='work' and i.action!='skip' and str(i.payload['src']).endswith('project-notes.txt'))
        rep=oc.apply(plan,self.app,only=[item.id])
        self.assertEqual(rep.errors,[])
        files=list(self.app.store.meta_get('attachments:files',{}).values())
        self.assertEqual(len(files),1)
        self.assertEqual(files[0]['source'],'openclaw')
        read=await self.app.tools.run('read_file',{'path':files[0]['path']},ToolContext(self.app,session_id=self.app.main_session()))
        self.assertTrue(read.ok)
        self.assertTrue(read.tainted)

    def test_plan_skips_openclaw_templates(self):
        plan = self.plan()
        ids = {i.id for i in plan.items}
        # AGENTS.md в образце — нетронутая заготовка OpenClaw (хэш совпадает) — её не тащим
        self.assertNotIn("self:agents-ref", ids)
        template=self.src/'workspace'/'AGENTS.md'
        raw=template.read_bytes().replace(b'\r\n',b'\n')
        template.write_bytes(raw.replace(b'\n',b'\r\n'))
        self.assertNotIn('self:agents-ref',{i.id for i in self.plan().items})
        template.write_bytes(raw+b'\nOwner-specific instruction.\n')
        self.assertIn('self:agents-ref',{i.id for i in self.plan().items})
        self.assertIn("self:SOUL.md", ids)
        self.assertEqual(plan.agent.id, "tin")
        self.assertEqual(plan.install.version, "2026.10.5")
        # пути в конфиге — с другой машины; находим относительно папки установки
        self.assertEqual(plan.agent.workspace, self.src / "workspace")
        pub = json.dumps(plan.public(), ensure_ascii=False)
        self.assertNotIn("sk-or-v1-fixture0000000000000000", pub)
        self.assertNotIn("sk-fake-123456", pub)

    def test_full_relocation(self):
        rep = oc.apply(self.plan(), self.app)
        self.assertEqual(rep.errors, [])
        sd = self.app.selfdir
        ident = sd.identity()
        self.assertEqual((ident["name"], ident["emoji"], ident["avatar"]), ("Tin", "🤖", "avatars/avatar.png"))
        self.assertTrue(sd.born())
        self.assertTrue(sd.relocating())
        self.assertIn("Sarcastic but loyal", sd.read("SOUL.md"))
        self.assertIn("Миша", sd.read("USER.md"))
        self.assertIn("Бендера на Python", sd.read("MEMORY.md"))
        self.assertIn("кластер из тысячи Raspberry Pi", sd.read("DREAMS.md"))
        agents = sd.read("AGENTS.md")
        self.assertGreater(agents.index("SSH: homepc"), agents.index("## Окружение"))
        d = self.app.dirs
        self.assertTrue((d.diary / "2026-10-20.md").is_file() and (d.diary / "2026-10-21.md").is_file())
        self.assertTrue((d.skills / "weather" / "SKILL.md").is_file())
        self.assertTrue((d.skills / "weather" / ".quarantine").is_file())
        self.assertTrue((d.skills / "weather" / "references" / "notes.md").is_file())
        self.assertTrue((d.self_dir / "avatars" / "avatar.png").is_file())
        self.assertTrue((d.work / "project-notes.txt").is_file())
        # модели: свой провайдер из конфига (ключ — строкой) и OpenRouter из базы ключей
        provs = {p.get("oc_id"): p for p in self.app.models.providers().values()}
        self.assertEqual(set(provs), {"custom-127-0-0-1-5199", "openrouter"})
        self.assertEqual(self.app.secrets.get(provs["openrouter"]["keys"][0]), "sk-or-v1-fixture0000000000000000")
        self.assertEqual(self.app.secrets.get(provs["custom-127-0-0-1-5199"]["keys"][0]), "sk-fake-123456")
        self.assertEqual(provs["custom-127-0-0-1-5199"]["base_url"], "http://127.0.0.1:5199/v1")
        main = self.app.models.roles()["main"]
        self.assertEqual([(e["provider"], e["model"]) for e in main], [(provs["custom-127-0-0-1-5199"]["id"], "echo")])
        # история: в главный разговор, по порядку, с отметкой откуда
        msgs = self.app.store.messages(self.app.main_session())
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user", "assistant"])
        self.assertEqual(msgs[0]["content"], "Привет! Меня зовут Миша, я люблю краткость.")
        self.assertEqual([m["id"] for m in msgs], sorted(m["id"] for m in msgs))
        self.assertAlmostEqual(msgs[0]["created_at"], 1792626224.94, places=2)
        self.assertEqual(msgs[0]["meta"]["imported"], "openclaw")
        # расписание — в архив и задачей сердца, выключенной до просмотра хозяином
        cron = self.app.store.meta_get("import:openclaw:cron")
        self.assertEqual([j["name"] for j in cron], ["Утренняя сводка"])
        jobs = self.app.heart.jobs.all()
        self.assertEqual([(j.name, j.enabled, j.source, j.schedule, j.action["session"]) for j in jobs],
                         [("Утренняя сводка", False, "openclaw", {"kind": "cron", "expr": "0 9 * * *"}, "isolated")])
        self.assertEqual(jobs[0].action["prompt"], "Утренняя сводка: почта, календарь, погода")
        # архив: снимок до, оригиналы, отчёт; секретов в нём нет
        adir = Path(rep.dir)
        for f in ("report.md", "plan.json", "before/bender.db", "originals/IDENTITY.md", "originals/openclaw.redacted.json"):
            self.assertTrue((adir / f).is_file(), f)
        self.assertNotIn("sk-fake-123456", (adir / "originals" / "openclaw.redacted.json").read_text(encoding="utf-8"))
        self.assertNotIn("sk-fake-123456", (adir / "plan.json").read_text(encoding="utf-8"))
        # ритуал и промпт
        prompt = system_prompt(self.app)
        self.assertIn("Переселение", prompt)
        self.assertIn("Tin", prompt)
        self.assertNotIn("BOOTSTRAP.md — пробуждение", prompt)
        self.assertEqual(len(rep.public()["body"]), 2)
        self.assertEqual(self.app.store.meta_get("import:openclaw:last")["agent"], "tin")

    def test_reimport_adds_nothing(self):
        oc.apply(self.plan(), self.app)
        soul = self.app.selfdir.read("SOUL.md")
        plan2 = self.plan()
        for it in plan2.items:
            if it.kind in ("self", "diary", "session", "provider", "role", "skill", "avatar", "work", "archive"):
                self.assertEqual(it.action, "skip", it.id)
        self.assertIsNone(plan2.item("ritual"))
        oc.apply(plan2, self.app)
        self.assertEqual(len(self.app.store.messages(self.app.main_session())), 4)
        self.assertEqual(self.app.selfdir.read("SOUL.md"), soul)
        self.assertEqual(len(self.app.models.providers()), 2)
        self.assertEqual(len(self.app.models.roles()["main"]), 1)
        self.assertEqual(len(self.app.store.meta_get("import:openclaw:cron")), 1)
        self.assertEqual(len(self.app.heart.jobs.all()), 1)  # повторный переезд задач не дублирует

    def test_new_messages_in_openclaw_come_over_later(self):
        oc.apply(self.plan(), self.app)
        db = sqlite3.connect(self.src / "agents" / "tin" / "agent" / "openclaw-agent.sqlite")
        sid = db.execute("SELECT session_id FROM session_windows").fetchone()[0]
        ev = {"type": "message", "id": "new-1", "parentId": "x", "timestamp": "2026-10-22T08:00:00.000Z", "message": {"role": "user", "content": "Ещё одно"}}
        db.execute("PRAGMA foreign_keys=OFF")
        db.execute("INSERT INTO transcript_events(session_id, seq, event_json, created_at) VALUES(?, 10, ?, 1792656000000)", (sid, json.dumps(ev)))
        db.execute("DELETE FROM session_transcript_active_events")  # пусть читает все события по порядку
        db.commit()
        db.close()
        plan = self.plan()
        it = plan.item("session:agent:tin:main")
        self.assertEqual(it.action, "copy")
        self.assertIn("новых 1", it.note)
        oc.apply(plan, self.app)
        msgs = self.app.store.messages(self.app.main_session())
        self.assertEqual(len(msgs), 5)
        self.assertEqual(msgs[-1]["content"], "Ещё одно")

    def test_without_secrets(self):
        oc.apply(self.plan(), self.app, secrets=False)
        provs = list(self.app.models.providers().values())
        self.assertEqual(len(provs), 2)
        self.assertTrue(all(not p["keys"] for p in provs))
        self.assertFalse(self.app.dirs.secrets.exists() and "fixture" in self.app.dirs.secrets.read_text(encoding="utf-8"))

    def test_existing_bender_keeps_itself(self):
        sd = self.app.selfdir
        sd.set_identity({"name": "Бендер", "emoji": "🍺"})
        sd.write("SOUL.md", "# SOUL\n\nЯ уже кто-то.")
        sd.finish_awakening()
        self.app.store.add_message(self.app.main_session(), {"role": "user", "content": "мой старый разговор"})
        p = self.app.models.add_provider(preset="fake")
        self.app.models.set_role("main", [{"provider": p["id"], "model": "slow"}])
        plan = self.plan()
        self.assertIn("сейчас Бендера зовут Бендер", plan.item("self:identity").note)
        self.assertEqual(plan.item("self:SOUL.md").action, "append")
        self.assertEqual(plan.item("provider:custom-127-0-0-1-5199").action, "merge")  # тот же адрес, что у тестового
        self.assertEqual(plan.item("role:main").action, "append")
        self.assertIn("отдельным разговором", plan.item("session:agent:tin:main").detail)
        oc.apply(plan, self.app)
        soul = sd.read("SOUL.md")
        self.assertIn("Я уже кто-то.", soul)
        self.assertIn("## Из OpenClaw", soul)
        self.assertIn("Sarcastic", soul)
        self.assertEqual(len(self.app.store.messages(self.app.main_session())), 1)
        imported = self.app.store.sessions(kind="imported")
        self.assertEqual(len(imported), 1)
        self.assertEqual(len(self.app.store.messages(imported[0]["id"])), 4)
        main = self.app.models.roles()["main"]
        self.assertEqual([e["model"] for e in main], ["slow", "echo"])
        self.assertEqual(len(self.app.models.providers()), 2)  # фейк объединён, OpenRouter новый

    def test_config_variants(self):
        cfg = """// написано руками
        {
          agents: { entries: { tin: { workspace: "~/.openclaw/workspace", agentDir: "~/.openclaw/agents/tin/agent",
                                       model: { primary: "deep/chat", fallbacks: ["openrouter/x-ai/grok", "ghost/model"] },
                                       heartbeat: { every: "2h", activeHours: { start: "09:00", end: "24:00" } } } } },
          models: { providers: {
            deep: { baseUrl: "https://api.deepseek.com/v1", api: "openai-completions", apiKey: "${OC_TEST_KEY}",
                    models: [{ id: "chat", cost: { input: 0.27, output: 1.1 } }] },
            vault: { baseUrl: "https://example.com/v1", api: "openai-completions", apiKey: { source: "exec", id: "x" } },
          } },
          channels: { telegram: { botToken: "123:abc", allowFrom: ["42"] } },
          mcp: { servers: { docs: { command: "uvx", args: ["mcp-server-fetch"], env: { API_TOKEN: "t0k" } } } },
          memory: { search: { provider: "openai-compatible", model: "text-embedding-3-small", remote: { baseUrl: "https://emb.example.com/v1/", apiKey: "sk-emb" } } },
        }"""
        (self.src / "openclaw.json").write_text(cfg, encoding="utf-8")
        db = sqlite3.connect(self.src / "agents" / "tin" / "agent" / "openclaw-agent.sqlite")
        store = {"version": 1, "profiles": {"openrouter:manual": {"type": "api_key", "provider": "openrouter", "key": "sk-or-1"},
                                            "openai-codex:me": {"type": "oauth", "provider": "openai-codex", "access": "a", "refresh": "r"}}}
        db.execute("UPDATE auth_profile_store SET store_json = ?", (json.dumps(store),))
        db.commit()
        db.close()
        os.environ["OC_TEST_KEY"] = "sk-deep-env"
        try:
            plan = self.plan()
        finally:
            os.environ.pop("OC_TEST_KEY")
        warnings = "\n".join(plan.warnings)
        self.assertIn("OAuth", warnings)
        self.assertIn("exec", warnings)
        self.assertIn("ghost/model", warnings)
        self.assertEqual(plan.item("setting:heart").payload["changes"], {"interval_minutes": 120, "quiet_hours": "00:00-09:00"})
        self.assertEqual(plan.item("role:main").payload["entries"], [["deep", "chat"], ["openrouter", "x-ai/grok"]])
        self.assertIsNotNone(plan.item("secret:channels.telegram.botToken"))
        self.assertIsNotNone(plan.item("mcp:servers"))
        rep = oc.apply(plan, self.app)
        self.assertEqual(rep.errors, [])
        provs = {p.get("oc_id"): p for p in self.app.models.providers().values()}
        self.assertNotIn("preset", provs["deep"])
        self.assertEqual(self.app.secrets.get(provs["deep"]["keys"][0]), "sk-deep-env")
        self.assertEqual(provs["deep"]["prices"]["chat"], {"in": 0.27, "out": 1.1})
        self.assertEqual(provs["vault"]["keys"], [])  # ключ через exec не переносится — провайдер без ключа
        self.assertEqual(self.app.settings.get("heart", "interval_minutes"), 120)
        self.assertEqual(self.app.secrets.get("openclaw:channels.telegram.botToken"), "123:abc")
        self.assertNotEqual(self.app.store.meta_get("import:openclaw:channels")[0]["telegram"]["botToken"], "123:abc")
        mcp = self.app.mcp.servers()[0]
        self.assertEqual(mcp["command"], ["uvx", "mcp-server-fetch"])
        self.assertFalse(mcp["enabled"])
        self.assertEqual(self.app.guard.expand_secrets(mcp["env"]["API_TOKEN"], tool=f"mcp:{mcp['id']}")[0], "t0k")
        emb = self.app.models.roles()["embed"][0]
        self.assertEqual(emb["model"], "text-embedding-3-small")
        self.assertEqual(self.app.models.providers()[emb["provider"]]["base_url"], "https://emb.example.com/v1")
        self.assertEqual(self.app.secrets.get(self.app.models.providers()[emb["provider"]]["keys"][0]), "sk-emb")

    def test_compressed_events_fall_back_to_search_index(self):
        db = sqlite3.connect(":memory:")
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE transcript_events (session_id TEXT, seq INTEGER, event_json TEXT, created_at INTEGER, event_zstd BLOB);
            CREATE TABLE session_transcript_fts_content (id INTEGER PRIMARY KEY, c0, c1, c2, c3, c4);
            INSERT INTO transcript_events VALUES ('s1', 0, NULL, 1, x'28b52ffd00');
            INSERT INTO session_transcript_fts_content VALUES (1, 'вопрос', 's1', 'm1', 'user', 1792626236320.0);
            INSERT INTO session_transcript_fts_content VALUES (2, 'ответ', 's1', 'm2', 'assistant', 1792626237000.0);
            INSERT INTO session_transcript_fts_content VALUES (3, 'чужое', 's2', 'm3', 'user', 1.0);
        """)
        msgs, how = oc.read_transcript(db, "s1")
        db.close()
        self.assertEqual(how, "fts")
        self.assertEqual([(m["role"], m["content"], m["oc_id"]) for m in msgs], [("user", "вопрос", "m1"), ("assistant", "ответ", "m2")])

    def test_find_installs(self):
        os.environ["OPENCLAW_STATE_DIR"] = str(self.src)
        try:
            self.assertIn(self.src, [i.root for i in oc.find_installs()])
        finally:
            os.environ.pop("OPENCLAW_STATE_DIR")
        home = self.tmp / "home"
        shutil.copytree(self.src, home / ".openclaw")
        self.assertEqual(oc.find_installs(str(home))[0].root, home / ".openclaw")  # указали домашнюю папку — нашли .openclaw
        with self.assertRaises(Exception):
            oc.find_installs(str(self.tmp / "пусто"))

    @async_test
    async def test_update_self_done_ends_relocation(self):
        oc.apply(self.plan(), self.app)
        res = await self.app.tools.run("update_self", {"file": "done"}, ToolContext(app=self.app, session_id="t", actor="agent"))
        self.assertTrue(res.ok, res.content)
        self.assertFalse(self.app.selfdir.relocating())
        self.assertNotIn("RELOCATION", system_prompt(self.app))


class ImportToolTest(TempDirCase):
    @async_test
    async def test_agent_moves_itself_with_owner_approval(self):
        src = self.tmp / "oc"
        shutil.copytree(FIXTURE, src)
        async with MindEnv(self.tmp) as env:
            env.auto_approve("once")
            env.script([call("import_openclaw", path=str(src)), call("import_openclaw", path=str(src), apply=True), {"text": "Переехал."}])
            res = await env.app.mind.say(env.sid, "переезжай из OpenClaw")
            self.assertIsNone(res["error"])
            self.assertEqual(sum(a['capability']=='self.rules' for a in env.approvals), 1)
            self.assertIsNone(env.app.selfdir.identity().get('name'))
            self.assertFalse(env.app.selfdir.relocating())
            imported=[a for a in env.app.agents.public() if a['id']!='main']
            self.assertEqual(len(imported),1)
            child=env.app.agents.get(imported[0]['id'])
            self.assertEqual(child.selfdir.identity()['name'],'Tin')
            self.assertTrue(child.selfdir.relocating())
            self.assertTrue(child.store.messages(child.main_session()))
            self.assertEqual(env.app.store.sessions(kind='imported'),[])

    @async_test
    async def test_agent_cannot_move_unattended(self):
        src = self.tmp / "oc"
        shutil.copytree(FIXTURE, src)
        async with MindEnv(self.tmp) as env:
            app = env.app
            res = await app.tools.run("import_openclaw", {"path": str(src), "apply": True}, ToolContext(app=app, session_id="t", actor="agent", unattended=True))
            self.assertFalse(res.ok)
            self.assertIsNone(app.selfdir.identity().get("name"))


class LegacyJsonlTest(ImportCase):
    """Старые стенограммы JSONL (agents/<агент>/sessions/) — у установок, ещё не переехавших на SQLite."""

    def _export_main(self) -> str:
        db = sqlite3.connect(self.src / "agents" / "tin" / "agent" / "openclaw-agent.sqlite")
        sid = db.execute("SELECT session_id FROM session_windows").fetchone()[0]
        lines = [r[0] for r in db.execute("SELECT event_json FROM transcript_events WHERE session_id = ? ORDER BY seq", (sid,))]
        db.close()
        sdir = self.src / "agents" / "tin" / "sessions"
        sdir.mkdir(parents=True, exist_ok=True)  # Git не сохраняет пустые каталоги фикстуры
        (sdir / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
        (sdir / "sessions.json").write_text(json.dumps({"agent:tin:main": {"sessionId": sid, "updatedAt": 1792626300000}}), encoding="utf-8")
        return sid

    def _old(self, name: str, texts: list[tuple[str, str]], day: str = "2026-07-01") -> None:
        evs = [{"type": "session", "version": 3, "id": "old", "timestamp": f"{day}T10:00:00.000Z"}]
        parent = None
        for i, (role, text) in enumerate(texts):
            evs.append({"type": "message", "id": f"{name[:6]}-{i}", "parentId": parent, "timestamp": f"{day}T10:0{i}:00.000Z", "message": {"role": role, "content": text}})
            parent = evs[-1]["id"]
        (self.src / "agents" / "tin" / "sessions" / name).write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in evs), encoding="utf-8")

    def test_active_branch_follows_leaf(self):
        evs = [{"type": "session", "id": "s"},
               {"type": "message", "id": "a", "parentId": None, "message": {"role": "user", "content": "раз"}},
               {"type": "message", "id": "b", "parentId": "a", "message": {"role": "assistant", "content": "два"}},
               {"type": "message", "id": "c", "parentId": "b", "message": {"role": "user", "content": "брошенная ветка"}},
               {"type": "leaf", "targetId": "b"},
               {"type": "message", "id": "d", "parentId": "b", "message": {"role": "user", "content": "новая ветка"}}]
        self.assertEqual([m["content"] for m in oc.events_to_messages(oc.active_branch(evs))], ["раз", "два", "новая ветка"])
        old = [{"type": "message", "message": {"role": "user", "content": "без id"}}, {"type": "message", "message": {"role": "assistant", "content": "тоже"}}]
        self.assertEqual(len(oc.events_to_messages(oc.active_branch(old))), 2)

    def test_jsonl_merges_with_database_and_archives(self):
        self._export_main()  # та же история, что в базе (как после переезда OpenClaw на SQLite)
        self._old("1111aaaa-0000-4000-8000-000000000001.jsonl.reset.2026-07-01T10-30-00.000Z",
                  [("user", "Давно: напомни купить хлеб"), ("assistant", "Напомню")])
        self._old("2222bbbb-0000-4000-8000-000000000002.jsonl.deleted.2026-07-02T10-30-00.000Z", [("user", "СЕКРЕТНОЕ УДАЛЁННОЕ")])
        self._old("3333cccc.trajectory.jsonl", [("user", "служебная траектория")])
        plan = self.plan()
        main = plan.item("session:agent:tin:main")
        self.assertIn("4 сообщ.", main.detail)  # повторы из файла и базы отсеяны по id событий
        old = plan.item("session:?:1111aaaa-0000-4000-8000-000000000001")
        self.assertIsNotNone(old)
        self.assertIn("старый разговор от 2026-07-01", old.title)
        rep = oc.apply(plan, self.app)
        self.assertEqual(rep.errors, [])
        self.assertEqual(len(self.app.store.messages(self.app.main_session())), 4)
        everything = json.dumps([self.app.store.messages(s["id"]) for s in self.app.store.sessions()], ensure_ascii=False)
        self.assertIn("купить хлеб", everything)
        self.assertNotIn("СЕКРЕТНОЕ УДАЛЁННОЕ", everything)
        self.assertNotIn("служебная траектория", everything)
        self.assertTrue(self.app.index.search("купить хлеб", limit=3))  # перенесённое находится памятью
        # повторный перенос ничего не добавляет
        for it in self.plan().items:
            if it.kind == "session":
                self.assertEqual(it.action, "skip", it.id)

    def test_install_without_database(self):
        self._export_main()
        (self.src / "agents" / "tin" / "agent" / "openclaw-agent.sqlite").unlink()
        plan = self.plan()
        it = plan.item("session:agent:tin:main")
        self.assertIn("из старых файлов JSONL", it.detail)
        rep = oc.apply(plan, self.app)
        self.assertEqual([e for e in rep.errors if "сесс" in e.lower() or "разговор" in e.lower()], [])
        msgs = self.app.store.messages(self.app.main_session())
        self.assertEqual(msgs[0]["content"], "Привет! Меня зовут Миша, я люблю краткость.")
        self.assertEqual(len(msgs), 4)


class TelegramImportTest(ImportCase):
    def test_telegram_bot_and_owner_come_over_disabled(self):
        from bender.channels.telegram import OWNER_META, TOKEN_SECRET

        cfg_path = self.src / "openclaw.json"
        cfg = json5.loads(cfg_path.read_text(encoding="utf-8"))
        cfg["channels"] = {"telegram": {"enabled": True, "botToken": "123456789:AAEfixture-token-fixture-token-xx", "dmPolicy": "allowlist", "allowFrom": ["tg:424242"], "groupAllowFrom": ["tg:555555", "777777", "@name", 12]}}
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
        plan = self.plan()
        it = plan.item("archive:channels")
        self.assertIn("Telegram станет каналом", it.note)
        self.assertEqual(it.payload["telegram_owner_ids"], [424242])
        self.assertEqual(it.payload["telegram_group_allow"], [555555, 777777])  # имена и короткие числа не берём
        self.assertNotIn("AAEfixture", json.dumps(plan.public(), ensure_ascii=False))
        rep = oc.apply(plan, self.app)
        self.assertEqual(rep.errors, [])
        self.assertEqual(self.app.secrets.get(TOKEN_SECRET), "123456789:AAEfixture-token-fixture-token-xx")
        self.assertEqual(self.app.store.meta_get(OWNER_META)["user_id"], 424242)
        self.assertFalse(self.app.settings.get("telegram", "enabled"))  # включает хозяин, когда остановит OpenClaw
        self.assertIn("Telegram готов к включению в «Связи»", rep.done)
        self.assertEqual(self.app.store.meta_get("telegram:group_allow_import"), [555555, 777777])


if __name__ == "__main__":
    unittest.main()
