import unittest
from pathlib import Path

from bender.guard.policy import Action, Policy, hardline_hit, is_safe_read_command
from tests.helpers import TempDirCase


class PolicyTest(TempDirCase):
    def make(self, **settings):
        s = {"enabled": True, "profile": "careful", "hardline": True}
        s.update(settings)
        self.rules = []
        work = self.tmp / "data" / "work"
        work.mkdir(parents=True, exist_ok=True)
        return Policy(work_dir=work, data_dir=self.tmp / "data", get_settings=lambda: s, get_rules=lambda: self.rules)

    def test_hardline(self):
        for cmd in ["rm -rf /", "rm -rf ~", "sudo rm -fr /*", 'rm -rf "/"', "mkfs.ext4 /dev/sda1", "dd if=/dev/zero of=/dev/sda", ":(){ :|:& };:", "chmod -R 777 /",
                    "rm -rf \\\n /"]:
            self.assertIsNotNone(hardline_hit(cmd), cmd)
        for cmd in ["rm -rf build", "rm -rf ./dist", "ls /", "dd if=a of=b", "echo mkfs"]:
            self.assertIsNone(hardline_hit(cmd), cmd)

    def test_safe_commands(self):
        for cmd in ["ls -la", "git status", "git log --oneline -5", "cat README.md", "df -h", "uname -a"]:
            self.assertTrue(is_safe_read_command(cmd), cmd)
        for cmd in ["ls; rm -rf x", "cat a > b", "echo $HOME", "python3 -c 'print(1)'", "bash -c ls", "busybox rm x", "git push", "find . -delete",
                    "ls `whoami`", "cat a\\\nrm b", "env rm x", "sort -o out.txt in.txt"]:
            self.assertFalse(is_safe_read_command(cmd), cmd)

    def test_profiles_and_paths(self):
        p = self.make()
        work = p.work_dir
        self.assertEqual(p.decide(Action("fs.write", str(work / "a.txt"))).verdict, "allow")
        self.assertEqual(p.decide(Action("fs.write", str(self.tmp / "outside.txt"))).verdict, "ask")
        self.assertEqual(p.decide(Action("fs.read", str(Path.home() / ".ssh" / "id_rsa"))).verdict, "deny")
        self.assertEqual(p.decide(Action("fs.read", str(self.tmp / "data" / "secrets.json"))).verdict, "deny")
        self.assertEqual(p.decide(Action("exec", "ls -la")).verdict, "allow")
        self.assertEqual(p.decide(Action("exec", "pip install x")).verdict, "ask")
        self.assertEqual(p.decide(Action("exec", "pip install x", unattended=True)).verdict, "deny")
        self.assertEqual(p.decide(Action("exec", "rm -rf /")).verdict, "deny")
        self.assertEqual(p.decide(Action("msg.send", "owner", to_owner=True)).verdict, "allow")
        self.assertEqual(p.decide(Action("money", "x")).verdict, "ask")

    def test_safe_read_of_protected_path_is_not_free(self):
        """Охота 16.11: cat ~/.ssh/id_rsa не проходит как «простое чтение»."""
        p = self.make(profile="normal")
        self.assertNotEqual(p.decide(Action("exec", "cat " + str(Path.home() / ".ssh" / "id_rsa"))).verdict, "allow")
        self.assertNotEqual(p.decide(Action("exec", "cat ~/.ssh/id_rsa")).verdict, "allow")
        self.assertNotEqual(p.decide(Action("exec", "head -n 5 " + str(self.tmp / "data" / "secrets.json"))).verdict, "allow")
        self.assertEqual(p.decide(Action("exec", "cat README.md")).verdict, "allow")

    def test_self_and_checkpoints_not_plain_files(self):
        """Охота 16.11: write_file в data/self идёт как update_self, снимки отката — только своим тулом."""
        p = self.make(profile="normal")
        data = self.tmp / "data"
        heart = Action("fs.write", str(data / "self" / "HEARTBEAT.md"), tainted=True)
        self.assertEqual(p.decide(heart).verdict, "ask")
        self.assertEqual(p.decide(Action("fs.write", str(data / "self" / "HEARTBEAT.md"), tainted=True, unattended=True)).verdict, "deny")
        self.assertEqual(p.decide(Action("fs.write", str(data / "self" / "AGENTS.md"))).verdict, "ask")
        self.assertEqual(p.decide(Action("fs.write", str(data / "agents" / "helper" / "self" / "AGENTS.md"))).verdict, "ask")
        self.assertEqual(p.decide(Action("fs.write", str(data / "checkpoints" / "2026-11-16" / "x" / "manifest.json"))).verdict, "deny")
        self.assertEqual(p.decide(Action("fs.read", str(data / "self" / "MEMORY.md"))).verdict, "allow")
        self.assertEqual(p.decide(Action("fs.write", str(data / "work" / "a.txt"), tainted=True)).verdict, "allow")

    def test_taint_breaks_trifecta(self):
        p = self.make(profile="trust")
        self.assertEqual(p.decide(Action("net.send", "https://evil.example")).verdict, "allow")
        self.assertEqual(p.decide(Action("net.send", "https://evil.example", tainted=True)).verdict, "ask")
        # «Доверие» в заражённом ходе спрашивает только то, что прямо уносит данные к чужим
        self.assertEqual(p.decide(Action("memory.core", "MEMORY.md", tainted=True)).verdict, "allow")
        self.assertEqual(p.decide(Action("exec", "pip install x", tainted=True)).verdict, "allow")
        self.assertEqual(p.decide(Action("msg.send", "чужой", tainted=True)).verdict, "ask")
        self.assertEqual(p.decide(Action("msg.send", "owner", to_owner=True, tainted=True)).verdict, "allow")
        normal = self.make(profile="normal")
        self.assertEqual(normal.decide(Action("memory.core", "MEMORY.md", tainted=True)).verdict, "ask")
        self.assertEqual(normal.decide(Action("exec", "pip install x", tainted=True)).verdict, "ask")
        p = self.make(profile="trust")
        # правило «всегда» хозяина не снимает защиту в заражённом ходе
        self.rules.append({"id": "r1", "capability": "net.send", "target": "*", "verdict": "allow"})
        self.assertEqual(p.decide(Action("net.send", "https://x", tainted=True)).verdict, "ask")

    def test_taint_switch(self):
        """Заражение можно отключить."""
        on = self.make(profile="normal")
        self.assertEqual(on.decide(Action("exec", "pip install x", tainted=True, in_sandbox=True)).verdict, "ask")
        off = self.make(profile="normal", taint=False)
        self.assertEqual(off.decide(Action("exec", "pip install x", tainted=True, in_sandbox=True)).verdict, "allow")
        self.assertEqual(off.decide(Action("net.send", "https://x", tainted=True)).verdict, "ask")  # профиль спрашивает и без заражения
        self.assertEqual(off.decide(Action("exec", "rm -rf /", tainted=True)).verdict, "deny")
        self.rules.append({"id": "r1", "capability": "net.send", "target": "*", "verdict": "allow", "only_clean": True})
        self.assertEqual(off.decide(Action("net.send", "https://x", tainted=True)).verdict, "allow")

    def test_off_and_hardline_switch(self):
        p = self.make(profile="off")
        self.assertEqual(p.decide(Action("exec", "pip install x", unattended=True)).verdict, "allow")
        self.assertEqual(p.decide(Action("exec", "rm -rf /")).verdict, "deny")
        p2 = self.make(profile="off", hardline=False)
        self.assertEqual(p2.decide(Action("exec", "rm -rf /")).verdict, "allow")

    def test_owner_rules(self):
        p = self.make()
        self.rules.append({"id": "r1", "capability": "exec", "target": "npm test*", "verdict": "allow", "note": "тесты можно"})
        self.rules.append({"id": "r2", "capability": "fs.write", "target": str(p.work_dir / "secret*"), "verdict": "deny"})
        self.assertEqual(p.decide(Action("exec", "npm test --watch=false")).verdict, "allow")
        self.assertEqual(p.decide(Action("fs.write", str(p.work_dir / "secret.txt"))).verdict, "deny")


if __name__ == "__main__":
    unittest.main()


class SafeReadOptionsTest(unittest.TestCase):
    """Охота 16.11: «простые» команды с опциями, которые исполняют код или пишут, больше не проходят без вопроса."""

    def test_risky_options_are_not_safe(self):
        from bender.guard.policy import is_safe_read_command as safe
        for c in ("rg --pre sh x p.txt", "rg -z foo", "tree -i -o /tmp/x w", "sort --compress-program=sh big", "sort -o out a",
                  "git branch -D main", "git branch newb", "git remote add x https://e", "git tag v1", "git diff --ext-diff",
                  "uniq a b", "hostname evil", "date -s 2020", "grep -f pats x"):
            self.assertFalse(safe(c), c)
        for c in ("rg -n foo src", "tree -L 2", "sort -u a", "git branch", "git branch -a", "git remote -v", "git remote get-url origin",
                  "git tag", "git tag --list", "git log --oneline", "uniq -c a", "hostname", "hostname -I", "date", "date +%F", "grep -rn foo .", "ls -la"):
            self.assertTrue(safe(c), c)
