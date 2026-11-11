"""Модули без доступа к App: карантин пакета и отдельный процесс через MCP."""
from __future__ import annotations

import hashlib
import json
import shlex
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

from ..core.errors import HumanError
from ..core.ids import new_id
from ..mcp.manager import fingerprint, tool_name
from ..skills.manager import ID, bytes_from, digest, file_path, scan

CAPABILITIES = {"fs.read", "fs.write", "exec", "net.read", "net.send", "msg.send"}


def error(message, hint="Просмотри модуль в пульте и одобри конкретный пакет."):
    return HumanError("module", message, hint)


class Modules:
    def __init__(self, agent_ctx):
        self.app = agent_ctx
        self.root = agent_ctx.dirs.root / "modules"
        self.root.mkdir(exist_ok=True)
        self.candidates = agent_ctx.dirs.local / "module-candidates"
        self.snapshots = agent_ctx.dirs.local / "module-snapshots"
        self.archive = agent_ctx.dirs.local / "module-archive"
        self.cache = {}

    def path(self, name, *, candidate=False):
        if not isinstance(name, str) or not ID.fullmatch(name):
            raise error("Некорректное имя модуля")
        root = self.candidates if candidate else self.root
        if root.is_symlink():
            raise error("Каталог модулей не должен быть ссылкой")
        return root / name

    def limits(self):
        return self.app.settings.get("modules", "max_files", 128), self.app.settings.get("modules", "max_total_mb", 8)*1024*1024

    def package_at(self, root, *, full=False):
        paths, stamp = scan(root, *self.limits(), required="bender-module.json")
        cached = self.cache.get(str(root))
        if cached and cached[0] == stamp and not full:
            return cached[1]
        data = {rel: bytes_from(path, 65536 if rel == "bender-module.json" else 1024*1024) for rel, path in sorted(paths.items())}
        if scan(root, *self.limits(), required="bender-module.json")[1] != stamp:
            raise error("Модуль изменился во время чтения")
        try:
            meta = json.loads(data["bender-module.json"].decode("utf-8-sig"))
            json.dumps(meta,ensure_ascii=False,allow_nan=False).encode("utf-8")
            if not isinstance(meta, dict) or not isinstance(meta.get("name"), str) or not ID.fullmatch(meta["name"]):
                raise ValueError("name: 1–64 строчных латинских буквы, цифры, _ или -")
            if not isinstance(meta.get("version"), str) or not 0 < len(meta["version"]) <= 64:
                raise ValueError("нужна строка version")
            if not isinstance(meta.get("description", ""), str) or len(meta.get("description", "")) > 2048:
                raise ValueError("description: строка до 2048 символов")
            caps = meta.get("capabilities", [])
            if not isinstance(caps, list) or any(c not in CAPABILITIES for c in caps) or len(caps) != len(set(caps)):
                raise ValueError("способности: " + ", ".join(sorted(CAPABILITIES)))
            if meta.get("workshop") and "net.read" in caps:
                from ..workshop.proxy import hosts
                if hosts(meta.get("network_hosts")) != meta.get("network_hosts"):
                    raise ValueError("network_hosts: упорядоченные точные домены без повторов")
            for field in ("pages", "providers", "channels"):
                rows = meta.get(field, [])
                if not isinstance(rows, list) or len(rows) > 32 or any(not isinstance(row, dict) for row in rows):
                    raise ValueError(field + ": список не более 32 объектов")
                for row in rows:
                    if any(not isinstance(row.get(key, ""), str) or len(row.get(key, "")) > 256 for key in ("title", "name")):
                        raise ValueError(field + ": name/title должны быть строками до 256 символов")
            mcp = meta.get("mcp") or {}
            if not isinstance(mcp, dict):
                raise ValueError("mcp должен быть объектом")
            if mcp:
                entry = str(file_path(mcp.get("entry", "")))
                if entry not in data or mcp.get("runtime", "python") not in ("python", "sh", "node"):
                    raise ValueError("mcp: entry внутри пакета и runtime python/sh/node")
            for page in meta.get("pages", []):
                if not isinstance(page, dict) or str(file_path(page.get("path", ""))) not in data or not page["path"].endswith(".html"):
                    raise ValueError("pages: список {path: страница.html, title: название}")
            for provider in meta.get("providers", []):
                parsed = urlsplit(provider.get("base_url", ""))
                if provider.get("kind") not in ("openai", "anthropic") or parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username:
                    raise ValueError("providers: kind openai/anthropic и base_url http(s) без пароля")
            for channel in meta.get("channels", []):
                if not isinstance(channel, dict) or not ID.fullmatch(str(channel.get("name", ""))):
                    raise ValueError("channels: нужно имя name")
                if channel.get("send_tool") and (not mcp or "msg.send" not in caps):
                    raise ValueError("каналу с send_tool нужны MCP и способность msg.send")
                if channel.get("send_tool") and not isinstance(channel["send_tool"], str):
                    raise ValueError("send_tool: имя MCP-инструмента")
            channels = [c["name"] for c in meta.get("channels", [])]
            if len(channels) != len(set(channels)):
                raise ValueError("имена каналов не должны повторяться")
        except (ValueError, TypeError, AttributeError, KeyError, RecursionError) as exc:
            raise error("Не удалось разобрать bender-module.json", str(exc)) from None
        manifest = {rel: hashlib.sha256(value).hexdigest() for rel, value in data.items()}
        package = {"id": meta["name"], "metadata": meta, "hash": digest(manifest), "manifest": manifest,
                   "files": [{"path": rel, "bytes": len(value)} for rel, value in data.items()]}
        self.cache[str(root)] = (stamp, package)
        return {**package, "data": data} if full else package

    def package(self, name, *, candidate=False, full=False):
        package = self.package_at(self.path(name, candidate=candidate), full=full)
        if package["id"] != name:
            raise error("Имя в манифесте не совпадает с установленным модулем")
        return package

    def trust(self, name):
        return self.app.settings.group("module_trust").get(name) or {}

    def active(self, package):
        if package["metadata"].get("workshop") and (not self.app.settings.get("workshop", "enabled", True) or not self.app.settings.get("workshop", "tools_enabled", True)):
            return False
        if package["metadata"].get("workshop") and "net.read" in package["metadata"].get("capabilities", []) and not self.app.settings.get("workshop", "network_proxy", False):
            return False
        return not self.app.safe and self.app.settings.get("modules", "enabled", True) and package["id"] not in self.app.settings.get("modules", "disabled", []) and self.trust(package["id"]).get("hash") == package["hash"]

    def allowed_server(self, server):
        try:
            item = self.package(server["module"])
            trust = self.trust(item["id"])
            if bool(item["metadata"].get("workshop")) != bool(server.get("workshop_runtime")):
                return False
            if server.get("workshop_runtime") and (server.get("workshop_runtime") != ("bwrap-v2" if "net.read" in item["metadata"].get("capabilities", []) else "bwrap-v1") or server.get("enabled") is not False):
                return False
            return self.active(item) and trust.get("server_config") == fingerprint(server) and \
                self.package_at(Path(trust["snapshot"]))["hash"] == item["hash"]
        except (HumanError, OSError, ValueError, KeyError):
            return False

    def public(self):
        items = []
        for root in sorted(self.root.iterdir()):
            if root.name.startswith("."):
                continue
            try:
                package = self.package(root.name)
                trust = self.trust(root.name)
                candidate = self.package(root.name, candidate=True) if self.path(root.name, candidate=True).exists() else None
                items.append({**package, "approved": trust.get("hash") == package["hash"], "enabled": self.active(package),
                              "candidate": candidate, "previous": trust.get("manifest"), "server": trust.get("server"),
                              "channels": {k: v for k,v in self.app.settings.group("module_channels").items() if v.get("module") == root.name}})
            except (HumanError, OSError, ValueError) as exc:
                items.append({"id": root.name, "error": str(exc), "enabled": False, "approved": False})
        return {"enabled": self.app.settings.get("modules", "enabled", True), "safe": self.app.safe, "modules": items}

    def copy(self, package, target):
        if target.exists():
            raise error("Целевая папка модуля уже существует")
        target.mkdir(parents=True)
        for rel, content in package["data"].items():
            path = target / rel
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def import_path(self, source):
        source = Path(source).expanduser()
        if source.is_symlink() or self.app.guard.policy.protected(source.resolve()):
            raise error("Импорт из ссылки или защищённого места запрещён")
        package = self.package_at(source, full=True)
        for rel in package["data"]:
            if self.app.guard.policy.protected((source / rel).resolve()):
                raise error("В пакете есть файл из защищённого места")
        name = package["id"]
        target = self.path(name)
        if target.exists() and self.package(name)["hash"] == package["hash"]:
            return {"id": name, "skipped": True}
        if target.exists():
            self.candidates.mkdir(exist_ok=True)
            target = self.path(name, candidate=True)
            if target.exists():
                self.archive.mkdir(exist_ok=True)
                target.rename(self.archive / (name + "-candidate-" + new_id()))
        with tempfile.TemporaryDirectory(prefix=".import-", dir=target.parent) as folder:
            staged = Path(folder) / name
            self.copy(package, staged)
            staged.rename(target)
        self.app.journal.write("module", f"Модуль {name} {package['metadata']['version']} импортирован в карантин", actor="owner")
        return {"id": name, "candidate": target.parent == self.candidates}

    def import_bundled(self,name):
        """Встроенный пакет только в карантин; обход защищённых путей не доступен импорту произвольного пути."""
        if name!='matrix':raise error('Нет такого встроенного пакета')
        package=self.package_at(Path(__file__).parent/'bundled'/name,full=True)
        target=self.path(name)
        if target.exists():
            if self.package(name)['hash']==package['hash']:return {'id':name,'skipped':True}
            self.candidates.mkdir(exist_ok=True);target=self.path(name,candidate=True)
            if target.exists():
                self.archive.mkdir(exist_ok=True);target.rename(self.archive/(name+'-candidate-'+new_id()))
        self.copy(package,target)
        self.app.journal.write('module','Встроенный Matrix установлен в карантин',actor='owner')
        return {'id':name,'candidate':target.parent==self.candidates}

    async def import_git(self, url, ctx, *, ref="", subdir=""):
        parsed = urlsplit(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise error("Нужен HTTPS-адрес git без логина и пароля")
        await ctx.check("net.read", url, "скачать пакет модуля из git")
        with tempfile.TemporaryDirectory(prefix="module-git-", dir=self.app.dirs.work) as folder:
            folder = Path(folder)
            git = ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false", "-c", "protocol.file.allow=never"]
            tree = "HEAD:" + str(file_path(subdir)) if subdir else "HEAD"
            commands = [git + ["clone", "--depth", "1", "--no-checkout", "--no-recurse-submodules"] + (["--branch", ref] if ref else []) + ["--", url, str(folder / "repo")],
                        git + ["-C", str(folder / "repo"), "archive", "--format=tar", "--output=" + str(folder / "package.tar"), tree]]
            for argv in commands:
                result = await self.app.tools.run("run", {"command": shlex.join(argv), "argv": argv, "cwd": str(folder), "allow_net": True}, ctx)
                if not result.ok:
                    raise error("Не удалось получить модуль из git", result.content)
            return self.import_archive(folder / "package.tar")

    def import_archive(self, archive):
        """Распаковываем только обычные ограниченные файлы; tar.extract не используется."""
        with tempfile.TemporaryDirectory(prefix="module-archive-", dir=self.app.dirs.work) as temporary:
            max_files, max_size = self.limits()
            if archive.stat().st_size > max_size + max_files*4096:
                raise error("Архив модуля слишком большой")
            staging = Path(temporary) / "package"
            staging.mkdir()
            total, seen = 0, set()
            with tarfile.open(archive) as tar:
                for member in tar:
                    if member.isdir():
                        continue
                    rel = str(file_path(member.name))
                    total += member.size
                    if not member.isfile() or rel in seen or len(seen) >= max_files or total > max_size or member.size > 1024*1024:
                        raise error("Архив содержит ссылки, повторы или слишком большие файлы")
                    seen.add(rel)
                    path = staging / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(tar.extractfile(member).read(member.size))
            return self.import_path(staging)

    async def approve(self, name, expected, *, candidate=False):
        package = self.package(name, candidate=candidate, full=True)
        if package["hash"] != expected:
            raise error("Пакет изменился после просмотра")
        old = self.trust(name)
        if old.get("server"):
            await self.app.mcp.disconnect(old["server"])
        if candidate:
            self.archive.mkdir(exist_ok=True)
            previous = self.archive / (name + "-" + new_id())
            current = self.path(name)
            current.rename(previous)
            try:
                self.path(name, candidate=True).rename(current)
            except OSError:
                previous.rename(current)
                raise
        self.snapshots.mkdir(exist_ok=True)
        snapshot = self.snapshots / package["hash"]
        if not snapshot.exists():
            self.copy(package, snapshot)
        if self.package_at(snapshot)["hash"] != package["hash"]:
            raise error("Снимок модуля изменён; запуск запрещён")
        trust = {"hash": package["hash"], "manifest": package["manifest"], "snapshot": str(snapshot)}
        mcp = package["metadata"].get("mcp")
        if mcp:
            runtime = mcp.get("runtime", "python")
            program = sys.executable if runtime == "python" else shutil.which(runtime)
            if not program:
                raise error("Не найден интерпретатор модуля", runtime)
            sid = "mod_" + digest(name)[:16]
            caps = package["metadata"].get("capabilities", [])
            managed = bool(package["metadata"].get("workshop"))
            await self.app.mcp.save({"id": sid, "name": "Модуль " + name, "module": name, "module_snapshot": str(snapshot),
                "transport": "stdio", "command": [program, str(snapshot / mcp["entry"])], "cwd": str(self.app.dirs.work), "enabled": not managed,
                **({"workshop_runtime": "bwrap-v2" if "net.read" in caps else "bwrap-v1"} if managed else {}),
                "readonly": "fs.write" not in caps, "network": any(c in caps for c in ("net.read", "net.send"))})
            trust.update(server=sid, server_config=fingerprint(self.app.mcp.get(sid)))
        elif old.get("server"):
            await self.app.mcp.remove(old["server"])
        trusts = self.app.settings.group("module_trust")
        trusts[name] = trust
        self.app.settings.replace("module_trust", trusts)
        await self.set_enabled(name, True)
        self.app.journal.write("module", f"Хозяин одобрил пакет {name}: {package['hash'][:12]}", actor="owner")
        return self.public()

    async def set_enabled(self, name, enabled):
        package = self.package(name)
        if enabled and self.trust(name).get("hash") != package["hash"]:
            raise error("Сначала нужно одобрить модуль")
        disabled = set(self.app.settings.get("modules", "disabled", []))
        disabled.discard(name) if enabled else disabled.add(name)
        self.app.settings.set("modules", {"disabled": sorted(disabled)}, by="owner")
        if not enabled and self.trust(name).get("server"):
            await self.app.mcp.disconnect(self.trust(name)["server"])

    async def remove(self, name):
        trust = self.trust(name)
        if trust.get("server"):
            await self.app.mcp.remove(trust["server"])
        self.archive.mkdir(exist_ok=True)
        self.path(name).rename(self.archive / (name + "-removed-" + new_id()))
        trusts = self.app.settings.group("module_trust")
        trusts.pop(name, None)
        self.app.settings.replace("module_trust", trusts)
        for mapping in self.app.settings.group("module_channels").values():
            if mapping.get("module") == name and self.app.hooks.get(mapping["hook"]):
                self.app.hooks.update(mapping["hook"], {"enabled": False})
        return self.public()

    def read(self, name, path="bender-module.json", *, candidate=False):
        package = self.package(name, candidate=candidate, full=True)
        rel = str(file_path(path))
        if rel not in package["data"]:
            raise error("Нет такого файла в модуле")
        return package, package["data"][rel]

    async def check_call(self, server, ctx):
        if not self.allowed_server(server):
            raise error("Модуль изменён или выключен")
        ctx.tainted = True
        for capability in self.package(server["module"])["metadata"].get("capabilities", []):
            target = str(self.app.dirs.work) if capability.startswith("fs.") else "module/" + server["module"]
            await ctx.check(capability, target, f"способность модуля {server['module']}: {capability}")

    def channel_enabled(self, hook):
        for value in self.app.settings.group("module_channels").values():
            if value.get("hook") == hook:
                try:
                    return self.active(self.package(value["module"]))
                except (HumanError, OSError):
                    return False
        return True

    def add_channel(self, name, channel):
        package = self.package(name)
        if not self.active(package) or channel not in [c["name"] for c in package["metadata"].get("channels", [])]:
            raise error("Канал не одобрен в этом модуле")
        mappings = self.app.settings.group("module_channels")
        key = name + ":" + channel
        if key not in mappings:
            hook = self.app.hooks.create("Модуль " + key, action="notify")
            mappings[key] = {"module": name, "channel": channel, "hook": hook["id"]}
            self.app.settings.replace("module_channels", mappings)
        return self.app.hooks.public(self.app.hooks.get(mappings[key]["hook"]))

    async def send(self, name, channel, target, text, ctx):
        package = self.package(name)
        entry = next((c for c in package["metadata"].get("channels", []) if c["name"] == channel), None)
        if not self.active(package) or not entry or not entry.get("send_tool"):
            raise error("Исходящий канал недоступен")
        await ctx.check("msg.send", target, f"отправить через {name}/{channel}: {target}")
        return await self.app.tools.run(tool_name(self.trust(name)["server"], entry["send_tool"]), {"target": target, "text": text}, ctx)
