"""Реальные лимиты процессов и, когда ядро позволяет, границы файлов и сети."""
import asyncio
import json
import os
import shlex
import sys
import tempfile
from pathlib import Path

from bender.core.app import App
from bender.core.settings import NeedsOwner
from bender.guard import sandbox
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test


class SandboxTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.app = App(self.tmp / "data")
        self.app.settings.set("guard", {"memory_mb": 1024})  # по умолчанию без лимита; здесь проверяем, что лимит доходит
        self.app.guard.grant("sandbox", [("exec", "*"), ("skill.run", "*"), ("mcp.call", "*")])
        self.ctx = ToolContext(self.app, session_id="sandbox")

    async def execute(self, code, *, info=None, allow_net=False):
        info = info or sandbox.for_app(self.app)
        command = shlex.join([sys.executable, "-c", code])
        with sandbox.prepare(command, info=info, work_dir=self.app.dirs.work, allow_net=allow_net, temp_root=self.app.dirs.local) as prepared:
            proc = await asyncio.create_subprocess_exec(*prepared.argv, cwd=self.app.dirs.work,
                        env={**os.environ, **prepared.env}, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            output, _ = await asyncio.wait_for(proc.communicate(), 8)
            return proc.returncode, output.decode("utf-8", "replace")

    @async_test
    async def test_resource_limits_without_namespaces(self):
        try:
            if os.name != "posix":
                self.skipTest("resource.setrlimit доступен на POSIX")
            info = {"kind": "none", "landlock": False, "limits": {"RLIMIT_AS": 64 * 1024 * 1024}}
            code, text = await self.execute("bytearray(256*1024*1024)", info=info)
            self.assertNotEqual(code, 0)
            self.assertIn("MemoryError", text)
            target = self.app.dirs.work / "large.bin"
            info["limits"] = {"RLIMIT_FSIZE": 1024 * 1024}
            code, _ = await self.execute(f"open({str(target)!r}, 'wb').write(b'x' * (2*1024*1024))", info=info)
            self.assertNotEqual(code, 0)
            self.assertLessEqual(target.stat().st_size, 1024 * 1024)
            info["limits"] = {"RLIMIT_CPU": 1}
            code, _ = await self.execute("while True: pass", info=info)
            self.assertNotEqual(code, 0)
            self.assertEqual(list(self.app.dirs.local.glob("sandbox-*")), [])
        finally:
            await self.app.close()

    @async_test
    async def test_agent_command_sees_only_own_data_and_no_control_api(self):
        try:
            self.app.agents.create('Сосед',agent_id='neighbor',awaken=False)
            other=self.app.agents.get('neighbor')
            (other.dirs.work/'private.txt').write_text('ПРИВАТНОЕ-СОСЕДА')
            (self.app.dirs.work/'mine.txt').write_text('МОЁ')
            info=sandbox.for_app(self.app)
            if info.get('kind')!='bwrap':self.skipTest('Нужен настоящий bubblewrap')
            code,output=await self.execute(f"from pathlib import Path; print(Path('mine.txt').read_text()); print(Path({str(other.dirs.work/'private.txt')!r}).exists()); print(Path({str(self.app.dirs.db)!r}).exists())",info=info)
            self.assertEqual(code,0,output);self.assertEqual(output.strip().splitlines(),['МОЁ','False','False'])
            with sandbox.prepare('true',info=info,work_dir=self.app.dirs.work,allow_net=True) as prepared:
                self.assertIn('--share-net',prepared.argv)
        finally:await self.app.close()

    @async_test
    async def test_write_boundary(self):
        try:
            info = sandbox.for_app(self.app)
            if not info.get("fs"):
                self.skipTest("Полная изоляция записи недоступна: " + info["title"])
            # /tmp в bwrap отдельный: запись в его тень не меняет хост и допустима.
            # Проверяем видимую хостовую папку вне рабочей, а не эту тень.
            outside_root = Path(__file__).resolve().parents[1]
            if not os.access(outside_root, os.W_OK):
                outside_root = Path.cwd()
            with tempfile.TemporaryDirectory(prefix=".sandbox-boundary-", dir=outside_root) as folder:
                outside = Path(folder) / "outside.txt"
                code, output = await self.execute(f"open({str(outside)!r},'w').write('bad')", info=info)
                self.assertNotEqual(code, 0)
                self.assertIn("PermissionError", output.replace("OSError: [Errno 30] Read-only file system", "PermissionError"))
                self.assertFalse(outside.exists())
            code, output = await self.execute("import tempfile; open('inside.txt','w').write('ok'); print(tempfile.TemporaryFile().write(b'ok'))", info=info)
            self.assertEqual(code, 0, output)
            self.assertEqual((self.app.dirs.work / "inside.txt").read_text(), "ok")
        finally:
            await self.app.close()

    @async_test
    async def test_process_limit_ignores_owner_processes(self):
        """Хозяин уже держит больше процессов, чем лимит: команды всё равно запускаются, а лимит держится."""
        children = []
        try:
            if os.name != "posix":
                self.skipTest("RLIMIT_NPROC есть только на POSIX")
            if os.getuid() == 0:
                self.skipTest("для root ядро не применяет RLIMIT_NPROC")
            self.app.settings.set("guard", {"process_count": 16}, by="owner")
            for _ in range(40):
                children.append(await asyncio.create_subprocess_exec("sleep", "30"))
            code = ("import os,time\nn=0\ntry:\n for _ in range(200):\n  if os.fork()==0:\n   time.sleep(2);os._exit(0)\n  n+=1\n"
                    "except OSError:pass\nprint('forked',n)")
            for allow_net in (False, True):
                exit_code, output = await self.execute(code, allow_net=allow_net)
                self.assertEqual(exit_code, 0, output)
                forked = int(output.split("forked")[-1])
                self.assertGreater(forked, 0, output)
                self.assertLess(forked, 40, output)
        finally:
            for child in children:
                child.kill()
                await child.wait()
            await self.app.close()

    def test_process_limit_set_where_it_counts(self):
        work = self.tmp / "work"
        work.mkdir()
        limits = {"RLIMIT_NPROC": 16}
        info = {"kind": "landlock", "landlock": True, "unshare": "/usr/bin/unshare", "limits": limits}
        prepared = sandbox.prepare("true", info=info, work_dir=work, allow_net=False)
        try:
            # Лимит ставится уже внутри нового пространства имён: снаружи unshare не смог бы даже fork.
            self.assertEqual(prepared.argv[0], "/usr/bin/unshare")
            config = json.loads(prepared.argv[prepared.argv.index(str(sandbox.CHILD)) + 1])
            self.assertEqual(config["limits"], limits)
            self.assertTrue(config["landlock"])
        finally:
            prepared.close()
        prepared = sandbox.prepare("true", info=info, work_dir=work, allow_net=True)
        try:
            config = json.loads(prepared.argv[2])
            self.assertEqual(prepared.argv[1], str(sandbox.CHILD))
            self.assertTrue(config["nproc_relative"])
        finally:
            prepared.close()
        from bender.guard import sandbox_child
        if sys.platform.startswith("linux"):
            self.assertGreaterEqual(sandbox_child.user_tasks(), 1)

    def test_ssh_config_include_readable_under_bwrap(self):
        """В namespace файлы root чужие, и ssh отказывается читать /etc/ssh/ssh_config.d/*.conf."""
        if os.name != "posix" or os.getuid() != 0:
            self.skipTest("нужен root, чтобы подложить файл чужого владельца")
        source = self.tmp / "ssh_config.d"
        source.mkdir()
        (source / "20-systemd-ssh-proxy.conf").write_text("Host .host\n")
        os.chown(source / "20-systemd-ssh-proxy.conf", 65534, 65534)
        work = self.tmp / "work"
        work.mkdir()
        info = {"kind": "bwrap", "path": "/usr/bin/bwrap", "limits": {}}
        with sandbox.prepare("ssh host true", info=info, work_dir=work, allow_net=False) as prepared:
            self.assertNotIn("--ro-bind-data", prepared.argv)
            self.assertIsNone(prepared.temporary)
            prepared._ssh_config(self.tmp, source)
            at = prepared.argv.index(str(source.resolve()))
            copy = Path(prepared.argv[at - 1])
            self.assertEqual(prepared.argv[at - 2], "--ro-bind")
            item = copy / "20-systemd-ssh-proxy.conf"
            self.assertEqual(item.read_text(), "Host .host\n")
            self.assertEqual(item.stat().st_uid, os.getuid())
            self.assertEqual(item.stat().st_mode & 0o777, 0o644)
            self.assertEqual(prepared.argv[-3:-1], ["/bin/sh", "-c"])
        self.assertFalse(copy.exists())

    @async_test
    async def test_network_namespace(self):
        try:
            info = sandbox.for_app(self.app)
            if not info.get("net"):
                self.skipTest("Пространство имён сети недоступно: " + info["title"])
            server = await asyncio.start_server(lambda r,w: w.close(), "127.0.0.1", 0)
            try:
                port = server.sockets[0].getsockname()[1]
                code, output = await self.execute(f"import socket; socket.create_connection(('127.0.0.1',{port}),timeout=1)", info=info)
                self.assertNotEqual(code, 0)
                self.assertNotIn("bwrap:", output)
            finally:
                server.close()
                await server.wait_closed()
        finally:
            await self.app.close()

    @async_test
    async def test_run_and_background_share_limits_cleanup_and_halt(self):
        try:
            if os.name != "posix":
                self.skipTest("resource.setrlimit доступен на POSIX")
            script = "import resource,os; print(resource.getrlimit(resource.RLIMIT_AS)[0]); print(os.environ['TMPDIR'])"
            result = await self.app.tools.run("run", {"command": shlex.join([sys.executable, "-c", script])}, self.ctx)
            self.assertTrue(result.ok, result.content)
            self.assertIn(str(1024*1024*1024), result.content)
            self.assertEqual(list(self.app.dirs.local.glob("sandbox-*")), [])
            script += "; import time; time.sleep(120)"
            result = await self.app.tools.run("process_start", {"command": shlex.join([sys.executable, "-u", "-c", script])}, self.ctx)
            self.assertTrue(result.ok, result.content)
            identifier = result.data["id"]
            for _ in range(100):
                status = await self.app.tools.run("process_status", {"id": identifier}, self.ctx)
                if "1073741824" in status.content:
                    break
                await asyncio.sleep(.01)
            self.assertIn("1073741824", status.content)
            self.assertTrue(status.tainted)
            self.app.halt()
            await self.app.processes.items[identifier]["task"]
            self.assertEqual(list(self.app.dirs.local.glob("sandbox-*")), [])
        finally:
            await self.app.close()

    @async_test
    async def test_skill_and_mcp_inherit_limits(self):
        try:
            if os.name != "posix":
                self.skipTest("resource.setrlimit доступен на POSIX")
            root = self.tmp / "incoming"
            (root / "scripts").mkdir(parents=True)
            (root / "SKILL.md").write_text("---\nname: limits\ndescription: Проверить лимиты\n---\nПроверка")
            (root / "scripts/check.py").write_text("import resource\nprint(resource.getrlimit(resource.RLIMIT_AS)[0])")
            self.app.skills.import_path(root)
            self.app.skills.approve("limits", self.app.skills.package("limits")["hash"])
            result = await self.app.tools.run("skill_run", {"name": "limits", "path": "scripts/check.py"}, self.ctx)
            self.assertTrue(result.ok, result.content)
            self.assertIn("1073741824", result.content)
            state = self.app.dirs.work / "state.json"
            state.write_text(json.dumps({"include_limits": True}))
            await self.app.mcp.save({"id": "limits", "transport": "stdio", "command": [sys.executable, str(Path(__file__).parent / "fixtures/fake_mcp.py"), str(state)], "cwd": str(self.app.dirs.work), "enabled": True})
            await self.app.mcp.discover("limits", ToolContext(self.app, actor="owner"))
            self.app.mcp.approve("limits", self.app.mcp.public()["servers"][0]["review"])
            result = await self.app.tools.run("mcp__limits__echo", {"text": "лимит"}, self.ctx)
            self.assertTrue(result.ok, result.content)
            self.assertIn('"memory_limit": 1073741824', result.content)
            await self.app.mcp.close()
            self.assertEqual(list(self.app.dirs.local.glob("sandbox-*")), [])
        finally:
            await self.app.close()

    def test_memory_unlimited_by_default(self):
        """Лимит на процесс агента — адресное пространство, а не реальная память."""
        try:
            fresh = App(self.tmp / "fresh")
            try:
                self.assertNotIn("RLIMIT_AS", sandbox.for_app(fresh)["limits"])
                self.assertIn("RLIMIT_CPU", sandbox.for_app(fresh)["limits"])
                fresh.settings.set("guard", {"memory_mb": 2048}, by="agent")  # поставить лимит — не ослабление
                self.assertEqual(sandbox.for_app(fresh)["limits"]["RLIMIT_AS"], 2048 * 1024 * 1024)
            finally:
                asyncio.run(fresh.close())
        finally:
            asyncio.run(self.app.close())

    def test_owner_required_to_weaken_and_partial_is_not_full_sandbox(self):
        try:
            for changes in ({"resource_limits": False}, {"memory_mb": 2048}, {"memory_mb": 0}, {"cpu_seconds": 240}, {"file_mb": 128}, {"process_count": 2048}):
                with self.assertRaises(NeedsOwner):
                    self.app.settings.set("guard", changes, by="agent")
            self.assertFalse(sandbox.confined({"fs": True, "net": False}))
            self.assertFalse(sandbox.confined({"fs": False, "net": True}))
            self.assertTrue(sandbox.confined({"fs": True, "net": True}))
        finally:
            asyncio.run(self.app.close())
