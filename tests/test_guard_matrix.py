"""Сохранённые настройки Предохранителя, потолок прав, паспорт и тул settings
говорят одно и то же; реальное решение совпадает с показанным; жёсткий список держится и при выключенном;
отказ называет слой и действующую политику."""
import itertools
from pathlib import Path

from bender.guard.policy import Action
from bender.mind.passport import snapshot
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv


def act(cap, target, **kw):
    return Action(cap, target, actor="agent", unattended=True, **kw)


class GuardMatrixTest(TempDirCase):
    @async_test
    async def test_effective_policy_shown_and_applied_the_same(self):
        async with MindEnv(self.tmp) as env:
            host = env.app
            host.agents.create("Помощник", agent_id="helper", awaken=False)
            for agent_id, enabled, profile, ceiling in itertools.product(("main", "helper"), (False, True), ("off", "trust"), ("", "trust", "careful")):
                with self.subTest(agent=agent_id, enabled=enabled, profile=profile, ceiling=ceiling):
                    ctx = host if agent_id == "main" else host.agents.get(agent_id)
                    ctx.settings.set("guard", {"enabled": enabled, "profile": profile})
                    ctx.settings.set("agent_rights", {"guard_profile": ceiling})
                    want_on = bool(ceiling) or (enabled and profile != "off")
                    want = (ceiling or profile) if want_on else "off"
                    state = ctx.guard.state()
                    self.assertEqual((state["enabled"], state["profile"]), (want_on, want))
                    self.assertIn("потолком прав" if ceiling else "из настроек агента", state["source"])
                    passport = snapshot(ctx, None, detailed=True)
                    self.assertEqual((passport["guard_enabled"], passport["guard"]), (want_on, want))
                    shown = (await ctx.tools.run("settings", {"group": "guard"}, ToolContext(ctx, session_id=ctx.main_session()))).content
                    self.assertIn(f"Действует сейчас: {'включён' if want_on else 'выключен'}, профиль {want}", shown)
                    # реальное решение: безвредное чтение своей рабочей папки
                    d = await ctx.guard.check(act("fs.read", str(ctx.dirs.work / "x.txt")), session_id="s")
                    self.assertTrue(d.allowed)
                    self.assertEqual(d.rule, f"profile:{want}" if want_on else "off")
                    # exec и сеть: главному — по профилю, помощнику — потолок прав раньше профиля
                    d = await ctx.guard.check(act("exec", "echo hi"), session_id="s")
                    self.assertEqual(d.rule, "agent-rights" if agent_id == "helper" else f"profile:{want}" if want_on else "off")
                    d = await ctx.guard.check(act("net.send", "https://example.org"), session_id="s")
                    if agent_id == "helper":
                        self.assertEqual(d.rule, "agent-rights")
                    elif want == "careful":
                        self.assertEqual((d.verdict, d.rule), ("deny", "unattended"), "осторожный спросил бы, а спросить некого")
                    else:
                        self.assertTrue(d.allowed, d.reason)
                    # вне рабочей папки помощник упирается в потолок, главный — нет
                    d = await ctx.guard.check(act("fs.read", str(self.tmp / "outside.txt")), session_id="s")
                    self.assertEqual(d.rule == "agent-rights", agent_id == "helper")

    @async_test
    async def test_hardline_holds_when_off_and_only_hardline_switch_lifts_it(self):
        async with MindEnv(self.tmp) as env:
            host = env.app
            ran = []
            run = host.tools.run

            async def no_exec(name, args, ctx):  # исполнитель подменён: опасное не выполняется ни при каком решении
                if name in ("run", "delete"):
                    ran.append(name)
                return await run(name, args, ctx)

            host.tools.run = no_exec
            for enabled in (False, True):
                host.settings.set("guard", {"enabled": enabled, "profile": "off"})
                for cap, target in (("exec", "rm -rf /"), ("fs.delete", str(Path.home())), ("fs.delete", "/etc")):
                    d = await host.guard.check(act(cap, target), session_id="s")
                    self.assertEqual((d.verdict, d.rule), ("deny", "hardline"), (enabled, cap, target))
                host.settings.set("guard", {"hardline": False})
                d = await host.guard.check(act("fs.delete", str(Path.home())), session_id="s")
                self.assertEqual(d.rule, "off", "жёсткий список выключается только своим переключателем")
                host.settings.set("guard", {"hardline": True})
            # обычное удаление в рабочей папке при выключенном — разрешено
            d = await host.guard.check(act("fs.delete", str(host.dirs.work / "tmp.txt")), session_id="s")
            self.assertEqual(d.rule, "off")
            self.assertEqual(ran, [])

    @async_test
    async def test_denial_names_layer_policy_and_is_journaled(self):
        async with MindEnv(self.tmp) as env:
            host = env.app
            host.agents.create("Сосед", agent_id="neighbor", awaken=False)
            host.agents.create("Помощник", agent_id="helper", awaken=False)
            neighbor, helper = host.agents.get("neighbor"), host.agents.get("helper")
            (neighbor.dirs.work / "secret.txt").write_text("ЧУЖОЕ", encoding="utf-8")
            helper.settings.set("agent_rights", {"guard_profile": "trust"})
            tool = ToolContext(helper, session_id=helper.main_session(), actor="agent")
            res = await helper.tools.run("read_file", {"path": str(neighbor.dirs.work / "secret.txt")}, tool)
            self.assertFalse(res.ok)
            self.assertNotIn("ЧУЖОЕ", res.content)
            self.assertIn("слой: граница чужих агентов — agent-private", res.content)
            self.assertIn("профиль trust (задан потолком прав хозяина", res.content)
            row = [e for e in helper.journal.recent(50) if e.get("kind") == "decision" and (e.get("data") or {}).get("rule") == "agent-private"][-1]
            data = row["data"]
            self.assertEqual((data["agent"], data["capability"], data["tool"]), ("helper", "fs.read", "read_file"))
            self.assertEqual(data["policy"]["profile"], "trust")
            self.assertTrue(data["target"].endswith("secret.txt"))
            # своё — читается; паспорт честно говорит, разрешён ли запуск команд без изоляции
            (helper.dirs.work / "own.txt").write_text("СВОЁ", encoding="utf-8")
            self.assertIn("СВОЁ", (await helper.tools.run("read_file", {"path": "own.txt"}, tool)).content)
            box = snapshot(helper, None, detailed=True)["sandbox"]
            self.assertTrue(box["required"])
            self.assertEqual(box["commands_allowed"], box["kind"] == "bwrap")
