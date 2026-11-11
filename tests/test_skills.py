"""Навыки: формат, настоящее чтение/запуск, карантин, импорт и контекст модели."""

import asyncio
import json
import os
import unittest
from pathlib import Path
from unittest import mock

from bender.core.app import App
from bender.core.errors import HumanError
from bender.core.settings import NeedsOwner
from bender.guard import sandbox
from bender.mind.context import system_prompt
from bender.skills.format import parse
from bender.skills.manager import file_path, requirements
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test
from tests.test_import import FIXTURE, tree_hash
from tests.test_mind import MindEnv


class FormatTest(unittest.TestCase):
    def test_yaml_and_unknown_fields(self):
        meta, body = parse('''---
name: demo
description: >-
  Первая строка
  вторая строка
metadata:
  openclaw:
    requires:
      bins: [python3, sh]
      env:
        - DEMO_TOKEN
    os: [linux, darwin]
future: {keep: [one, two], enabled: true}
allowed-tools: Bash(python scripts/report.py)
---
ТЕЛО
''')
        self.assertEqual(meta["description"], "Первая строка вторая строка")
        self.assertEqual(meta["future"], {"keep": ["one", "two"], "enabled": True})
        self.assertEqual(requirements(meta), {"bins": ["python3", "sh"], "env": ["DEMO_TOKEN"], "tools": [], "toolsets": [], "platforms": ["linux", "darwin"]})
        self.assertEqual(body, "ТЕЛО")

    def test_quoted_literal_and_json_metadata(self):
        meta, body = parse('''---
name: example
description: 'Текст с # и двоеточием: да'
compatibility: |
  Нужен Python.
  Ничего не устанавливать автоматически.
metadata:
  {
    "vendor": {"tags": ["тест", "код"]},
  }
future: [one,
  two
]
disable-model-invocation: true
---
Содержимое
'''.replace("\n", "\r\n"))
        self.assertEqual(meta["description"], "Текст с # и двоеточием: да")
        self.assertEqual(meta["compatibility"], "Нужен Python.\nНичего не устанавливать автоматически.\n")
        self.assertTrue(meta["disable-model-invocation"])
        self.assertEqual(meta["metadata"]["vendor"]["tags"], ["тест", "код"])
        self.assertEqual(meta["future"], ["one", "two"])

    def test_sandbox_keeps_snapshot_readable_under_private_tmp(self):
        stage = Path("/tmp/bender-data/local/skill-run-example").resolve()
        argv = sandbox.wrap("python report.py", info={"kind": "bwrap", "path": "/usr/bin/bwrap"},
                            work_dir=Path("/tmp/bender-data/work"), allow_net=False, read_dirs=(stage,))
        binding = argv.index(str(stage)) - 1
        self.assertEqual(argv[binding:binding + 3], ["--ro-bind", str(stage), str(stage)])
        self.assertGreater(binding, argv.index("--tmpfs"))
        self.assertNotIn("--share-net", argv)

    def test_reject_ambiguous_and_executable_yaml(self):
        for text in ["# без шапки", "---\nname: x\n", "---\nname: x\nname: y\ndescription: текст\n---",
                     "---\nname: ../x\ndescription: текст\n---", "---\nname: x\ndescription: !!python/object:x\n---",
                     "---\nname: x\ndescription: &anchor текст\n---", "---\nname: x\ndescription: *alias\n---"]:
            with self.subTest(text=text), self.assertRaises(HumanError):
                parse(text)


class SkillsTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.app = App(self.tmp / "data")

    def make(self, name="demo", *, header="", script=None, folder=None):
        root = folder or self.tmp / "incoming" / name
        root.mkdir(parents=True, exist_ok=True)
        (root / "SKILL.md").write_text(f"---\nname: {name}\ndescription: Краткий отчёт по задаче\n{header}---\nBODY_SENTINEL: составь отчёт.\n", encoding="utf-8")
        for rel, text in {"references/guide.md": "REFERENCE_SENTINEL: подробности", "templates/report.txt": "TEMPLATE_SENTINEL"}.items():
            target = root / rel
            target.parent.mkdir(exist_ok=True)
            target.write_text(text, encoding="utf-8")
        if script is not None:
            (root / "scripts").mkdir(exist_ok=True)
            (root / "scripts/report.py").write_text(script, encoding="utf-8")
        return root

    def attach(self, root, *, approve=True, app=None):
        app = app or self.app
        result = app.skills.import_path(root, source="claude")
        self.assertEqual(result["errors"], [])
        name = parse((root / "SKILL.md").read_text())[0]["name"]
        if approve:
            app.skills.approve(name, app.skills.package(name)["hash"])
        return app.skills.root(name)

    def context(self, **kw):
        return ToolContext(self.app, session_id="skills-test", **kw)

    def permit_script(self, path="scripts/report.py"):
        self.app.guard.grant("skills-test", [("skill.run", "demo/" + path)])

    @async_test
    async def test_quarantine_progressive_read_and_prompt(self):
        try:
            source = self.make(header="future: {keep: yes}\n")
            before = tree_hash(source)
            self.attach(source, approve=False)
            self.assertEqual(self.app.skills.index(), "")
            result = await self.app.tools.run("skill_read", {"name": "demo"}, self.context())
            self.assertFalse(result.ok)
            package = self.app.skills.package("demo")
            self.app.skills.approve("demo", package["hash"])
            index = self.app.skills.index()
            self.assertIn("Краткий отчёт", index)
            self.assertNotIn("BODY_SENTINEL", index)
            self.assertNotIn("REFERENCE_SENTINEL", system_prompt(self.app))
            self.assertNotIn("BODY_SENTINEL", system_prompt(self.app))
            result = await self.app.tools.run("skill_read", {"name": "demo"}, self.context())
            self.assertTrue(result.ok)
            self.assertTrue(result.tainted)
            self.assertIn("BODY_SENTINEL", result.content)
            self.assertNotIn("REFERENCE_SENTINEL", result.content)
            result = await self.app.tools.run("skill_read", {"name": "demo", "path": "references/guide.md"}, self.context())
            self.assertIn("REFERENCE_SENTINEL", result.content)
            self.assertNotIn("TEMPLATE_SENTINEL", result.content)
            self.assertEqual(package["metadata"]["future"], {"keep": "yes"})
            self.assertEqual(tree_hash(source), before)
        finally:
            await self.app.close()

    @async_test
    async def test_changes_and_stale_review_and_switches(self):
        try:
            root = self.attach(self.make())
            old = self.app.skills.package("demo")["hash"]
            (root / "references/guide.md").write_text("Новое содержимое", encoding="utf-8")
            self.assertFalse(self.app.skills.public()["skills"][0]["approved"])
            self.assertEqual(self.app.skills.index(), "")
            with self.assertRaises(HumanError):
                self.app.skills.approve("demo", old)
            with self.assertRaises(HumanError):
                self.app.skills.read("demo")
            self.app.skills.approve("demo", self.app.skills.package("demo")["hash"])
            self.app.skills.set_enabled("demo", False)
            self.assertEqual(self.app.skills.index(), "")
            with self.assertRaises(NeedsOwner):
                self.app.settings.set("skills", {"disabled": []}, by="agent")
            with self.assertRaises(NeedsOwner):
                self.app.settings.set("skill_trust", {"demo": {"hash": "подделка"}}, by="agent")
            self.app.skills.set_enabled("demo", True)
            self.app.safe = True
            self.assertEqual(self.app.skills.index(), "")
            with self.assertRaises(HumanError):
                self.app.skills.read("demo")
            self.app.safe = False
            self.app.settings.set("skills", {"enabled": False})
            self.assertFalse(self.app.tools.enabled("skill_read"))
            with self.assertRaises(HumanError):
                self.app.skills.command("/demo задача")
        finally:
            await self.app.close()

    @async_test
    async def test_script_snapshot_output_and_private_environment(self):
        try:
            code = '''import os
from pathlib import Path
text = (Path(os.environ['BENDER_SKILL_DIR']) / 'templates/report.txt').read_text()
Path('skill-result.txt').write_text(text)
print(text)
print('PARENT_VISIBLE=' + str('SKILLS_TEST_PARENT_KEY' in os.environ))
'''
            self.attach(self.make(script=code))
            self.permit_script()
            with mock.patch.dict(os.environ, {"SKILLS_TEST_PARENT_KEY": "не-наследовать"}):
                result = await self.app.tools.run("skill_run", {"name": "demo", "path": "scripts/report.py", "args": ["hello", ""]}, self.context())
            self.assertTrue(result.ok, result.content)
            self.assertTrue(result.tainted)
            self.assertIn("TEMPLATE_SENTINEL", result.content)
            self.assertIn("PARENT_VISIBLE=False", result.content)
            self.assertEqual((self.app.dirs.work / "skill-result.txt").read_text(), "TEMPLATE_SENTINEL")
            self.assertEqual(self.app.skills.processes, set())
            self.assertEqual(list(self.app.dirs.local.glob("skill-run-*")), [])
        finally:
            await self.app.close()

    @async_test
    async def test_unattended_denied_and_change_during_approval(self):
        try:
            root = self.attach(self.make(script="from pathlib import Path\nPath('should-not-exist').write_text('bad')\n"))
            result = await self.app.tools.run("skill_run", {"name": "demo", "path": "scripts/report.py"}, self.context(unattended=True))
            self.assertFalse(result.ok)
            self.assertFalse((self.app.dirs.work / "should-not-exist").exists())
            def on_event(ev):
                if ev.get("type") == "approval":
                    (root / "templates/report.txt").write_text("подмена", encoding="utf-8")
                    self.app.guard.respond(ev["card"]["id"], "once")
            self.app.bus.listen(on_event)
            result = await self.app.tools.run("skill_run", {"name": "demo", "path": "scripts/report.py"}, self.context())
            self.assertFalse(result.ok)
            self.assertFalse((self.app.dirs.work / "should-not-exist").exists())
            self.assertEqual(self.app.skills.processes, set())
        finally:
            await self.app.close()

    @async_test
    async def test_timeout_and_cancel_reap_process(self):
        try:
            self.attach(self.make(script="import time\nprint('started', flush=True)\ntime.sleep(120)\n"))
            self.permit_script()
            self.app.settings.set("skills", {"timeout_seconds": 1})
            result = await self.app.tools.run("skill_run", {"name": "demo", "path": "scripts/report.py"}, self.context())
            self.assertFalse(result.ok)
            self.assertTrue(result.data["timed_out"])
            self.assertEqual(self.app.skills.processes, set())
            self.app.settings.set("skills", {"timeout_seconds": 120})
            task = asyncio.create_task(self.app.tools.run("skill_run", {"name": "demo", "path": "scripts/report.py"}, self.context()))
            for _ in range(100):
                if self.app.skills.processes:
                    break
                await asyncio.sleep(.01)
            proc = next(iter(self.app.skills.processes))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertIsNotNone(proc.returncode)
            self.assertEqual(list(self.app.dirs.local.glob("skill-run-*")), [])
            # Выключение скриптов прерывает уже выполняющийся процесс.
            task = asyncio.create_task(self.app.tools.run("skill_run", {"name": "demo", "path": "scripts/report.py"}, self.context()))
            for _ in range(100):
                if self.app.skills.processes:
                    break
                await asyncio.sleep(.01)
            self.app.settings.set("skills", {"scripts_enabled": False})
            stopped = await task
            self.assertFalse(stopped.ok)
            self.assertEqual(self.app.skills.processes, set())
        finally:
            await self.app.close()

    @async_test
    async def test_required_environment_is_explicit_and_scrubbed(self):
        try:
            self.attach(self.make(header="requires:\n  env: [SKILLS_TEST_DECLARED]\n", script="import os\nprint(os.environ['SKILLS_TEST_DECLARED'])\n"))
            self.permit_script()
            cards = []
            def owner(ev):
                if ev.get("type") == "approval":
                    cards.append(ev["card"])
                    self.app.guard.respond(ev["card"]["id"], "once")
            self.app.bus.listen(owner)
            progress = []
            with mock.patch.dict(os.environ, {"SKILLS_TEST_DECLARED": "sensitive-value"}):
                result = await self.app.tools.run("skill_run", {"name": "demo", "path": "scripts/report.py"}, self.context(progress=progress.append))
            self.assertTrue(result.ok, result.content)
            self.assertEqual(cards[0]["capability"], "secret.use")
            self.assertNotIn("sensitive-value", result.content + "".join(progress) + json.dumps(cards))
            self.assertIn("секрет скрыт", result.content)
        finally:
            await self.app.close()

    @async_test
    async def test_import_collections_conflicts_and_symlinks(self):
        try:
            source = self.make(folder=self.tmp / "hermes" / "category" / "demo")
            self.make("other", folder=self.tmp / "hermes" / "other")
            before = tree_hash(self.tmp / "hermes")
            result = self.app.skills.import_path(self.tmp / "hermes", source="hermes")
            self.assertEqual(set(result["imported"]), {"demo", "other"})
            self.assertEqual(self.app.skills.index(), "")
            result = self.app.skills.import_path(self.tmp / "hermes", source="hermes")
            self.assertEqual(set(result["skipped"]), {"demo", "other"})
            self.assertEqual(tree_hash(self.tmp / "hermes"), before)
            (source / "SKILL.md").write_text((source / "SKILL.md").read_text() + "изменено", encoding="utf-8")
            self.assertTrue(self.app.skills.import_path(source)["errors"])
            self.assertNotIn("изменено", (self.app.skills.root("demo") / "SKILL.md").read_text())
            bad = self.make("unsafe")
            (bad / "references/link.md").symlink_to(source / "SKILL.md")
            self.assertTrue(self.app.skills.import_path(bad)["errors"])
            self.assertFalse(self.app.skills.root("unsafe").exists())
            for path in ["../SKILL.md", "/etc/passwd", "references/../SKILL.md", "scripts\\x.py", "x\nBAD"]:
                with self.subTest(path=path), self.assertRaises(HumanError):
                    file_path(path)
        finally:
            await self.app.close()

    @async_test
    async def test_limits_hardlinks_and_requirements(self):
        try:
            source = self.make()
            self.app.settings.set("skills", {"max_files": 1})
            self.assertTrue(self.app.skills.import_path(source)["errors"])
            self.app.settings.set("skills", {"max_files": 128})
            (source / "copy.md").hardlink_to(source / "SKILL.md")
            self.assertTrue(self.app.skills.import_path(source)["errors"])
            (source / "copy.md").unlink()
            self.attach(source)
            item = self.app.skills.package("demo")
            item = {**item, "metadata": {**item["metadata"], "requires": {"bins": ["no-such-skills-bin-123"], "env": ["NO_SUCH_SKILLS_ENV_123"]},
                                        "requires_tools": ["no-such-tool"], "requires_toolsets": ["no-such-group"], "platforms": ["unknown-os"]}}
            missing = self.app.skills.unavailable(item)
            self.assertEqual(len(missing), 5)
        finally:
            await self.app.close()

    @async_test
    async def test_slash_and_index_do_not_load_body_into_system_or_history(self):
        await self.app.close()
        source = self.make()
        async with MindEnv(self.tmp / "mind") as env:
            self.attach(source, app=env.app)
            env.script([{"text": "Готово"}])
            result = await env.app.mind.say(env.sid, "/demo@bender_bot коротко")
            self.assertIsNone(result["error"])
            self.assertTrue(result["tainted"])
            sent = env.fake.state.requests[0]["body"]["messages"]
            self.assertNotIn("BODY_SENTINEL", sent[0]["content"])
            self.assertIn("BODY_SENTINEL", sent[-1]["content"])
            self.assertNotIn("REFERENCE_SENTINEL", json.dumps(sent))
            self.assertNotIn("BODY_SENTINEL", json.dumps(env.app.store.messages(env.sid)))
            env.script([{"text": "Ещё ответ"}])
            result = await env.app.mind.say(env.sid, "Новая обычная задача")
            self.assertNotIn("BODY_SENTINEL", json.dumps(env.fake.state.requests[-1]["body"]["messages"]))

    @async_test
    async def test_manual_only_skill_and_openclaw_migration(self):
        try:
            self.attach(self.make(header="disable-model-invocation: true\n"))
            self.assertEqual(self.app.skills.index(), "")
            result = await self.app.tools.run("skill_read", {"name": "demo"}, self.context())
            self.assertFalse(result.ok)
            self.assertIn("BODY_SENTINEL", self.app.skills.read("demo", manual=True)["text"])
            self.assertEqual(self.app.skills.command("/skill demo задача"), {"name": "demo", "args": "задача"})
            from bender.importers import openclaw
            before = tree_hash(FIXTURE)
            plan = openclaw.make_plan(self.app, str(FIXTURE))
            report = await asyncio.to_thread(openclaw.apply, plan, self.app, only={"skill:weather"})
            self.assertEqual(report.errors, [])
            self.assertTrue((self.app.dirs.skills / "weather/.quarantine").exists())
            self.assertFalse(next(s for s in self.app.skills.catalog() if s["id"] == "weather")["approved"])
            self.assertEqual(tree_hash(FIXTURE), before)
        finally:
            await self.app.close()
