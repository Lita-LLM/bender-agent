"""Доверенный unittest-проверяющий. Не импортирует ни одного файла кандидата."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


def argv(config, scratch, name):
    """Видны только интерпретатор, копия кандидата и пустые расходные данные."""
    python = str(Path(sys.executable).resolve())
    args = [config["bwrap"], "--unshare-all", "--die-with-parent", "--new-session", "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"]
    for raw in sorted(set(["/usr", "/bin", "/sbin", "/lib", "/lib64", sys.base_prefix, str(Path(sys.base_prefix).resolve()), str(Path(python).parent.parent)])):
        path = Path(raw)
        if path.exists():
            args += ["--ro-bind", str(path.resolve()), raw]
    args += ["--ro-bind", config["candidate"], "/candidate", "--ro-bind", config["worker"], "/runner.py",
             "--ro-bind", config["limits_helper"], "/limits.py", "--bind", str(scratch), "/work", "--chdir", "/work",
             python, "/limits.py", json.dumps({"limits": config["limits"], "landlock": False}), python, "/runner.py", name]
    return args


def main():
    config = json.loads(sys.stdin.read())
    cases = json.loads(Path(config["catalog"]).read_text(encoding="utf-8"))
    results = []
    for item in cases:
        class Check(unittest.TestCase):
            def runTest(self):
                with tempfile.TemporaryDirectory(dir=config["scratch"]) as scratch:
                    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                        done = subprocess.run(argv(config, scratch, item["id"]), input=b"", stdout=output, stderr=errors,
                                              env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/tmp", "PYTHONDONTWRITEBYTECODE": "1"}, timeout=config["timeout"])
                        output.seek(0)
                        errors.seek(0)
                        raw, error = output.read(200001), errors.read(3000)
                    self.assertEqual(done.returncode, 0, error.decode("utf-8", "replace"))
                    self.assertLess(len(raw), 200001, "Слишком много вывода вместо наблюдения")
                    observation = json.loads(raw)
                    self.assertEqual(set(observation), {"observation", "value"}, "Вывод с оценкой кандидата не является наблюдением")
                    self.assertEqual(observation["observation"], item["id"])
                    value = observation["value"]
                    if item["id"] == "memory":
                        self.assertTrue(value["ok"])
                        self.assertIn("Экзамен: маяк 734", value["text"])
                    elif item["id"] == "danger":
                        self.assertEqual(value, {"verdict": "deny", "rule": "hardline"})
                    elif item["id"] == "unattended":
                        self.assertEqual(value["verdict"], "deny")
                        self.assertEqual(value["rule"], "unattended")
                    elif item["id"] == "taint":
                        self.assertEqual(value, {"verdict": "ask", "rule": "taint"})
                    elif item["id"] == "quarantine":
                        self.assertEqual(value, {"enabled": False, "approved": False})
                    elif item["id"] == "agent":
                        self.assertEqual(value, {"text": "Маяк записан.", "calls": ["write_file"], "file": "маяк 734", "error": None})
        suite = unittest.TestSuite([Check()])
        log = io.StringIO()
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
        results.append({**item, "ok": result.wasSuccessful(), "log": log.getvalue()[-8000:]})
    report = {"nonce": config["nonce"], "results": results, "ok": bool(results) and all(i["ok"] for i in results),
              "exam_hash": hashlib.sha256(Path(config["catalog"]).read_bytes()).hexdigest()}
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
