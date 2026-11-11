"""Подключения MCP, карантин определений и тулы, проходящие Предохранитель.

Одобрения живут отдельно от редактируемых серверов. Они привязаны к точной
конфигурации подключения и полным определениям тулов, включая незнакомые поля.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import os
import re
import shlex
from contextlib import nullcontext
from pathlib import Path
from urllib.parse import urlsplit

from ..core.errors import HumanError
from ..tools.registry import Tool, ToolResult, ToolContext
from ..tools.web import untrusted
from .client import Client, Http, Stdio


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def fingerprint(server) -> str:
    return digest({k: v for k, v in server.items() if k not in ("enabled", "name", "agents")})  # кому выдан — не меняет само подключение


SECRET_FLAG = re.compile(r"(token|key|secret|pass|hash|auth|session|cookie|phone|bearer)", re.I)


def hide_args(argv):
    """Значения у флагов вида --api-hash/--token скрыты: в аргументах хозяин мог вписать секрет напрямую."""
    out, hide = [], False
    for part in map(str, argv):
        if hide and not part.startswith("-"):
            out.append("[скрыто]"); hide = False; continue
        hide = False
        if part.startswith("-") and "=" in part and SECRET_FLAG.search(part.split("=", 1)[0]):
            out.append(part.split("=", 1)[0] + "=[скрыто]"); continue
        if part.startswith("-") and SECRET_FLAG.search(part):
            hide = True
        out.append(part)
    return out


def tool_name(sid, name):
    slug = re.sub(r"[^a-zA-Z0-9_-]", "_", name)
    if slug != name or len(slug) > 30:
        slug = slug[:20] + "_" + digest(name)[:8]
    return f"mcp__{sid}__{slug}"


class Manager:
    def __init__(self, agent_ctx):
        self.app = agent_ctx
        self.clients = {}
        # Что сервер реально показывает — одно на всю стаю: иначе изменение, замеченное агентом,
        # не видно на экране MCP (там «одобрено», а агенту «нужно одобрение» без кнопки).
        state = self._host_state()
        self.observed = state.setdefault("observed", {})
        self._observed_connections = state.setdefault("observed_connections", {})
        self._locks = {}
        self._registered = set()
        self._cleanup = set()
        self._oauth=None
        self._tasks=None;self._subscriptions=None
        from .content import Catalog
        self.catalog=Catalog(self)
        try:
            self._loop = asyncio.get_running_loop()
        except RuntimeError:
            self._loop = getattr(agent_ctx.bus, "_loop", None)
        self.register()

    def invalidate(self):
        if self._oauth:self._oauth.cancel()
        if self._subscriptions:self._subscriptions.cancel()
        pending=getattr(self.app.host,'_mcp_input_broker',None)
        if pending:pending.cancel(self)
        entries = list(self.clients.values())
        pool = self._host_state()["pool"]
        for sid, entry in list(self.clients.items()):
            if pool.get(sid) is entry:
                pool.pop(sid, None)
            if isinstance(entry[1].transport, Stdio):
                entry[1].transport.closed = True  # закрытие идёт фоном; другие агенты не должны взять этот процесс
        self.clients.clear()
        for _, client in entries:
            def cleanup(c=client):
                task = asyncio.get_running_loop().create_task(c.close())
                self._cleanup.add(task)
                task.add_done_callback(self._cleanup.discard)
            if self._loop and not self._loop.is_closed():
                try:
                    running = asyncio.get_running_loop()
                except RuntimeError:
                    running = None
                if running is self._loop:
                    cleanup()
                else:
                    self._loop.call_soon_threadsafe(cleanup)
        self.register()

    def servers(self):
        raw = self.app.settings.get("mcp", "servers", [])
        return [copy.deepcopy(s) for s in raw if isinstance(s, dict)] if isinstance(raw, list) else []

    @property
    def oauth(self):
        if self._oauth is None:
            from .oauth_flow import OAuth
            self._oauth=OAuth(self)
        return self._oauth

    @property
    def tasks(self):
        if self._tasks is None:
            from .tasks import Tasks
            self._tasks=Tasks(self)
        return self._tasks

    @property
    def subscriptions(self):
        if self._subscriptions is None:
            from .subscriptions import Subscriptions
            self._subscriptions=Subscriptions(self)
        return self._subscriptions

    def get(self, sid):
        server = next((s for s in self.servers() if s.get("id") == sid), None)
        if server is None:
            raise HumanError("mcp_missing", "Нет такого MCP-сервера", "Добавь его в разделе «MCP».")
        return server

    def trust(self, sid):
        return self.app.settings.group("mcp_trust").get(sid) or {}

    def signature(self, server):
        # Ротация значения или разрешений секрета тоже меняет подключение.
        # Значения участвуют только в хэше, в настройках/пульте их нет.
        from ..guard import SECRET_REF

        names = {m.group(1) for field in ("env", "headers") for value in server.get(field, {}).values()
                 for m in SECRET_REF.finditer(value)}
        bindings = {name: (self.app.secrets.get(name), self.app.secrets.allow(name)) for name in sorted(names)}
        return digest({"configuration": fingerprint(server), "bindings": bindings})

    def scrub_schema(self, value):
        if isinstance(value, str):
            from ..tools.web import clean_tokens
            return clean_tokens(self.app.guard.scrub(value))  # описания полей уходят в tools запроса без обёртки
        if isinstance(value, list):
            return [self.scrub_schema(x) for x in value]
        if isinstance(value, dict):
            return {self.app.guard.scrub(k): self.scrub_schema(v) for k, v in value.items()}
        return value

    def active(self, server):
        requested = server.get("enabled") is True
        if server.get("workshop_runtime"):
            # Старое ядро видит enabled=false и не расширяет среду при откате.
            requested = server.get("workshop_runtime") in ("bwrap-v1", "bwrap-v2") and server.get("enabled") is False and bool(server.get("module"))
        return not self.app.safe and bool(self.app.settings.get("mcp", "enabled", True)) and requested and \
            (not server.get("module") or self.app.modules.allowed_server(server))

    def allowed(self, server) -> bool:
        """Сервер виден и доступен только агентам, которых выбрал хозяин; без списка — всей стае.
        Помощник команды пользуется выбором агента, который его позвал."""
        agents = server.get("agents")
        if not isinstance(agents, list):
            return True
        ctx = self.app
        while getattr(ctx, "_task_agent", None) is not None:
            ctx = ctx._task_agent.parent
        return ctx.agent_id in agents

    def definition(self, sid, name):
        try:
            server = self.get(sid)
        except HumanError:
            return None
        trust = self.trust(sid)
        if not self.active(server) or not self.allowed(server) or trust.get("connection") != self.signature(server):
            return None
        approved = trust.get("tools", {}).get(name)
        observed = self.observed.get(sid)
        if not approved or (observed is not None and digest(observed.get(name)) != digest(approved)):
            return None
        return approved

    def register(self):
        for name in self._registered:
            self.app.tools.tools.pop(name, None)
        self._registered.clear()
        for server in self.servers():
            sid = server.get("id")
            trust = self.trust(sid)
            if trust.get("connection") != self.signature(server):
                continue
            for name, definition in trust.get("tools", {}).items():
                alias = tool_name(sid, name)

                def make_handler(server_id, raw_name):
                    async def handler(ctx, **args):
                        return await self.call(server_id, raw_name, args, ctx)
                    return handler

                self.app.tools.add(Tool(alias, f"MCP · {sid} · {name}",
                    untrusted(self.app.guard.scrub(f"описание MCP {sid}/{name}"), self.app.guard.scrub(str(definition.get("description") or name))),
                    self.scrub_schema(definition["inputSchema"]), make_handler(sid, name), group="mcp",
                    available=lambda agent_ctx, s=sid, n=name: self.definition(s, n) is not None, external=True))
                self._registered.add(alias)

    def store_server(self, raw):
        if not isinstance(raw, dict):
            raise HumanError("mcp_config", "Нужны параметры MCP-сервера", "Заполни форму подключения.")
        sid = raw.get("id")
        if not isinstance(sid, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,19}", sid):
            raise HumanError("mcp_config", "Некорректное имя MCP-сервера", "1–20 латинских букв, цифр, _ и -, начало — буква.")
        old = next((s for s in self.servers() if s.get("id") == sid), {})
        keys = set(raw) - {"id"}
        if old and keys and keys <= {"agents", "enabled", "name"}:
            return self._store_meta(old, raw)
        server = {**old, **{k: v for k, v in copy.deepcopy(raw).items() if k not in ("tools", "connected", "review", "content", "content_review", "managed_by_workshop",'rejected_tools','oauth_status','visible','diagnostics')}}
        server.setdefault("enabled", False)
        if (old.get("workshop_runtime") or server.get("workshop_runtime")) and server.get("enabled") is not False:
            raise HumanError("workshop_managed", "Этот MCP-тул включает Мастерская", "Управляй его состоянием в Мастерской; обычный enabled остаётся false для безопасного отката на старое ядро.")
        server.setdefault("name", sid)
        agents = server.get("agents")
        if agents is not None and (not isinstance(agents, list) or len(agents) > 256 or any(not isinstance(a, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", a) for a in agents)):
            raise HumanError("mcp_config", "Неверный список агентов MCP", "Отметь агентов стаи галочками или выбери «Всем агентам».")
        if agents is None:
            server.pop("agents", None)
        if 'tasks_enabled' in server and type(server['tasks_enabled']) is not bool:raise HumanError('mcp_config','Неверный переключатель Tasks','Выбери его в подключении MCP.')
        features=server.get('client_features',{})
        if not isinstance(features,dict) or any(key in features and not isinstance(features[key],bool) for key in ('roots','sampling','elicitation')):raise HumanError('mcp_config','Неверные встречные права MCP','Выбери выключатели в подключении.')
        roots=server.get('client_roots',[])
        if not isinstance(roots,list) or len(roots)>32 or any(not isinstance(path,str) or not path or len(path)>4096 or any(ord(c)<32 for c in path) or not Path(path).expanduser().is_absolute() for path in roots):raise HumanError('mcp_config','Неверные папки MCP','Не более 32 абсолютных путей без управляющих символов, по одному на строке.')
        transport = server.get("transport", "stdio")
        server["transport"] = transport
        if transport == "stdio":
            command = server.get("command")
            if not isinstance(command, list) or not command or not command[0] or len(command) > 128 or any(not isinstance(x, str) or "\x00" in x for x in command):
                raise HumanError("mcp_config", "Нужна команда списком аргументов", 'Например: ["python3", "/путь/server.py"]. Оболочка не добавляется.')
            server.setdefault("cwd", str(self.app.dirs.work))
            server.setdefault("network", False)
            server["cwd"] = str(Path(server["cwd"]).expanduser().resolve())
        elif transport in ("http","sse"):
            try:
                url = urlsplit(server.get("url", ""))
                valid = url.scheme in ("http", "https") and bool(url.hostname) and not url.username and not url.password and not url.fragment
                _ = url.port
            except ValueError:
                valid = False
            if not valid:
                raise HumanError("mcp_config", "Нужен адрес HTTP(S) без пароля в URL", "Авторизацию укажи в заголовках через сейф.")
        else:
            raise HumanError("mcp_config", "Неизвестный транспорт MCP", "Выбери stdio, http или старый sse.")
        for field in ("env", "headers"):
            values = server.get(field) or {}
            if not isinstance(values, dict) or any(not isinstance(k, str) or not isinstance(v, str) or "\x00" in k + v for k, v in values.items()):
                raise HumanError("mcp_config", "Переменные и заголовки должны быть строками", "Используй словарь имя → значение.")
            if field == "headers" and any(k.lower() in ("host", "content-length", "connection", "proxy-authorization") or "\n" in k + v or "\r" in k + v for k, v in values.items()):
                raise HumanError("mcp_config", "Недопустимый заголовок MCP", "Не меняй транспортные заголовки и не добавляй переводы строк.")
            # Значения формы не храним в настройках, даже если человек забыл ссылку на сейф.
            clean = {}
            for key, value in values.items():
                if "{{secret:" in value or not value:
                    clean[key] = value
                else:
                    secret = f"mcp:{sid}:{field}:{key}"
                    host = urlsplit(server.get("url", "")).hostname
                    secret=self.app.secrets.reuse(secret, value, note=f"MCP {sid}: {key}",
                                                 allow={"hosts": [host] if host else [], "tools": [f"mcp:{sid}"]})
                    clean[key] = "{{secret:" + secret + "}}"
            server[field] = clean
        servers = self.servers()
        servers = [server if s.get("id") == sid else s for s in servers]
        if not old:
            servers.append(server)
        self.app.settings.set("mcp", {"servers": servers}, by="owner")
        self.observed.pop(sid, None)
        self.catalog.observed.pop(sid,None);self.catalog.connections.pop(sid,None)
        self.register()
        self.app.journal.write("mcp", f"MCP {sid}: параметры сохранены; изменённое подключение требует одобрения", actor="owner")
        return self.public()

    def _store_meta(self, old, raw):
        """Включение, имя и «Кому доступен» не трогают подключение: одобрение сохраняется, остальное не переписывается."""
        server, sid = copy.deepcopy(old), old["id"]
        if "enabled" in raw:
            if type(raw["enabled"]) is not bool:
                raise HumanError("mcp_config", "Неверный выключатель MCP", "Нужно true или false.")
            if old.get("workshop_runtime") and raw["enabled"] is not False:
                raise HumanError("workshop_managed", "Этот MCP-тул включает Мастерская", "Управляй его состоянием в Мастерской; обычный enabled остаётся false для безопасного отката на старое ядро.")
            server["enabled"] = raw["enabled"]
        if "name" in raw:
            if not isinstance(raw["name"], str) or not raw["name"].strip() or len(raw["name"]) > 100:
                raise HumanError("mcp_config", "Неверное имя MCP-сервера", "До 100 символов.")
            server["name"] = raw["name"].strip()
        if "agents" in raw:
            agents = raw["agents"]
            if agents is not None and (not isinstance(agents, list) or len(agents) > 256 or any(not isinstance(a, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", a) for a in agents)):
                raise HumanError("mcp_config", "Неверный список агентов MCP", "Отметь агентов стаи галочками или выбери «Всем агентам».")
            if agents is None:
                server.pop("agents", None)
            else:
                server["agents"] = list(dict.fromkeys(agents))
        self.app.settings.set("mcp", {"servers": [server if s.get("id") == sid else s for s in self.servers()]}, by="owner")
        self.register()
        self.app.journal.write("mcp", f"MCP {sid}: " + ", ".join(f"{k} изменено" for k in sorted(set(raw) - {"id"})), actor="owner")
        return self.public()

    async def save(self, raw):
        result = self.store_server(raw)
        await self.disconnect(raw["id"])
        return result

    def import_servers(self, config, *, source="claude", secrets=True):
        """Из явного mcpServers/servers; не исполнять и не одобрять импортированное."""
        if not isinstance(config, dict):
            raise HumanError("mcp_import", "В файле нет объекта конфигурации", "Нужен mcpServers или mcp.servers.")
        entries = config.get("mcpServers")
        if entries is None:
            entries = (config.get("mcp") or {}).get("servers") if isinstance(config.get("mcp"), dict) else config.get("servers")
        if not isinstance(entries, dict):
            raise HumanError("mcp_import", "Список MCP-серверов не найден", "Поддерживаются mcpServers и mcp.servers.")
        imported, errors, warnings = [], [], []
        for name, raw in entries.items():
            sid = (source[:5] + "_" + re.sub(r"[^a-z0-9_]", "_", str(name).lower())[:7] + "_" + digest(str(name))[:6])[:20]
            if any(s.get("id") == sid for s in self.servers()):
                continue  # повторный импорт не затирает настроенное хозяином
            try:
                if not isinstance(raw, dict):
                    raise HumanError("mcp_import", "Описание сервера не является объектом", str(name))
                server = {"id": sid, "name": str(name), "enabled": False, "transport": "http" if raw.get("url") else "stdio"}
                if raw.get('type')=='sse':server['transport']='sse'
                if raw.get("url"):
                    server["url"] = raw["url"]
                else:
                    command = raw.get("command")
                    server["command"] = command if isinstance(command, list) else [command, *raw.get("args", [])]
                    if raw.get("cwd"):
                        server["cwd"] = raw["cwd"]
                for field in ("env", "headers"):
                    values = raw.get(field, {}) if secrets else {}
                    if not isinstance(values, dict):
                        raise HumanError("mcp_import", "Переменные и заголовки должны быть словарями", str(name))
                    resolved = {}
                    for key, value in values.items():
                        env_ref = re.fullmatch(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", value) if isinstance(value, str) else None
                        if env_ref:
                            value = os.environ.get(env_ref.group(1))
                        elif isinstance(value, dict):
                            value = os.environ.get(str(value.get("id"))) if value.get("source") == "env" else None
                        if value is None:
                            warnings.append(f"{name}: {field}.{key} не перенесено — нет доступного значения; добавь его в пульте")
                        else:
                            resolved[key] = value
                    server[field] = resolved
                self.store_server(server)
                imported.append(sid)
            except (HumanError, TypeError, ValueError) as exc:
                errors.append(f"{name}: {exc}")
        return {"imported": imported, "errors": errors, "warnings": warnings, **self.public()}

    async def disconnect(self, sid):
        if self._subscriptions:await self._subscriptions.stop(sid)
        entry = self.clients.pop(sid, None)
        if entry:
            pool = self._host_state()["pool"]
            if pool.get(sid) is entry:
                pool.pop(sid, None)
            await entry[1].close()

    async def remove(self, sid):
        self.get(sid)
        await self.disconnect(sid)
        if self._oauth:self._oauth.cancel(sid)
        with self.app.settings.transaction():
            self.app.settings.set("mcp", {"servers": [s for s in self.servers() if s.get("id") != sid]}, by="owner")
            trust = self.app.settings.group("mcp_trust")
            trust.pop(sid, None)
            self.app.settings.replace("mcp_trust", trust)
            oauth=self.app.settings.group('mcp_oauth');grants=oauth.get('servers')
            if isinstance(grants,dict) and sid in grants:
                grants.pop(sid,None);self.app.settings.replace('mcp_oauth',{**oauth,'servers':grants})
        self.observed.pop(sid, None)
        self.catalog.observed.pop(sid,None);self.catalog.connections.pop(sid,None)
        self.register()

    async def connect(self, server, ctx):
        self._loop = asyncio.get_running_loop()
        sid, signature = server["id"], self.signature(server)
        if not self.active(server):
            raise HumanError("mcp_disabled", "MCP-сервер выключен", "Включи его в разделе «MCP»; в безопасном режиме подключения запрещены.")
        if ctx.actor != "owner" and not self.allowed(server):
            raise HumanError("mcp_agent", "Этот MCP-сервер не выдан агенту", "Хозяин выбирает агентов в разделе «MCP» → сервер → «Кому доступен».")
        current = self.clients.get(sid)
        if current and current[0] == signature and current[1].version and not getattr(current[1].transport,'closed',False):
            return current[1]
        await self.disconnect(sid)
        if not self.shared(server):
            client = await self._open(server, ctx, signature)
            self.clients[sid] = (signature, client)
            return client
        # Один процесс stdio-сервера на всю стаю: две копии юзербота — две сессии одного аккаунта.
        state = self._host_state()
        async with state["locks"].setdefault(sid, asyncio.Lock()):
            entry = state["pool"].get(sid)
            if entry and entry[0] == signature and entry[1].version and entry[1].transport.alive():
                await self._check_connect(server, ctx, signature, self._protection(server, ctx))
                self.clients[sid] = entry
                return entry[1]
            if entry:
                state["pool"].pop(sid, None)
                await entry[1].close()
            client = await self._open(server, ctx, signature)
            entry = (signature, client)
            state["pool"][sid] = entry
            self.clients[sid] = entry
            return client

    def connected(self, sid):
        if sid in self.clients and not getattr(self.clients[sid][1].transport, "closed", False):
            return True
        entry = self._host_state()["pool"].get(sid)
        return bool(entry and entry[1].transport.alive())

    def diagnostics(self, server, *, lines=60):
        """Запуск и журнал сервера без секретов: значения env и заголовков не показываются, известные секреты вычищены."""
        sid = server["id"]
        info = self._host_state()["diag"].get(sid) or {}
        scrub = self.app.guard.scrub
        command = [scrub(part) for part in hide_args(server.get("command") or [])]
        out = {"transport": server.get("transport"), "shared": self.shared(server), "connected": self.connected(sid),
               "command": shlex.join(command) if command else "", "cwd": server.get("cwd", ""), "network": bool(server.get("network")),
               "readonly": bool(server.get("readonly")), "url": scrub(urlsplit(server.get("url", ""))._replace(query="", fragment="").geturl()) if server.get("url") else "",
               "env": sorted(server.get("env", {})), "headers": sorted(server.get("headers", {})),
               "started": info.get("started"), "launches": info.get("launches", 0), "exit": info.get("exit"),
               "error": info.get("error", ""), "error_at": info.get("error_at"), "launched_by": info.get("agent", "")}
        entry = self._host_state()["pool"].get(sid) or self.clients.get(sid)
        process = getattr(entry[1].transport, "process", None) if entry else None
        if process is not None:
            out["pid"] = process.pid
            if process.returncode is not None:
                out["exit"] = process.returncode
        out["stderr"] = [scrub(line) for line in list(info.get("log") or [])[-lines:]]
        return out

    def shared(self, server):
        return server.get("transport") == "stdio" and not server.get("module")

    def _host_state(self):
        host = self.app.host
        state = getattr(host, "_mcp_shared_state", None)
        if state is None:
            state = {"pool": {}, "locks": {}, "diag": {}, "observed": {}, "observed_connections": {}, "content": {}, "content_connections": {}}
            host._mcp_shared_state = state
        return state

    def diag(self, sid):
        from collections import deque
        return self._host_state()["diag"].setdefault(sid, {"log": deque(maxlen=300), "started": None, "launches": 0, "exit": None,
                                                           "error": "", "error_at": None, "agent": ""})

    def note_error(self, sid, exc):
        """Запомнить сбой сервера: текст ошибки, код выхода и хвост stderr — для хозяина и mcp_diagnose."""
        if not isinstance(exc, Exception):
            return
        import time
        info = self.diag(sid)
        entry = self.clients.get(sid) or self._host_state()["pool"].get(sid)
        transport = entry[1].transport if entry else None
        process = getattr(transport, "process", None)
        code = process.returncode if process is not None else getattr(transport, "returncode", None)
        title = exc.title + (": " + str(exc.detail) if exc.detail else "") if isinstance(exc, HumanError) else f"{type(exc).__name__}: {exc}"
        info.update(error=self.app.guard.scrub(title)[:2000], error_at=time.time(), exit=code if code is not None else info["exit"])
        tail = [self.app.guard.scrub(line) for line in list(info["log"])[-20:]]
        self.app.journal.write("mcp", f"MCP {sid}: {exc.title if isinstance(exc, HumanError) else type(exc).__name__}",
                               data={"server": sid, "exit": code, "stderr": tail})

    def _protection(self, server, ctx):
        from ..guard import sandbox
        if server["transport"] != "stdio":
            return {}
        # общий процесс запускается от имени стаи: песочница главного агента без чьих-либо личных папок
        protection = sandbox.for_app(self.app.host if self.shared(server) else self.app)
        if self.shared(server):
            protection["agent_read"] = []
        if ctx.actor == 'owner':
            protection['agent_isolation'] = False
        # сервер живёт часами: лимит процессорного времени убил бы его молча; память и число процессов остаются
        protection["limits"] = {k: v for k, v in (protection.get("limits") or {}).items() if k != "RLIMIT_CPU"}
        return protection

    async def _check_connect(self, server, ctx, signature, protection):
        command = server.get("command") or []
        await ctx.check("mcp.connect", f"{server['id']}/{signature}", f"подключить MCP {server['id']}",
                        detail={"transport": server["transport"], "command": shlex.join(command), "url": server.get("url", ""),
                                "cwd": server.get("cwd", ""), "network": server.get("network", False),
                                "sandbox": protection.get("title"), "limits": protection.get("limits")})

    async def _open(self, server, ctx, signature):
        sid = server["id"]
        command = server.get("command") or []
        from ..guard import sandbox

        protection = self._protection(server, ctx)
        if server.get("module"):
            ctx.tainted = True
        await self._check_connect(server, ctx, signature, protection)
        # После карточки конфигурация могла измениться; запускать старую нельзя.
        if self.signature(self.get(sid)) != signature or not self.active(self.get(sid)):
            raise HumanError("mcp_changed", "Подключение MCP изменилось во время одобрения", "Повтори действие с новой конфигурацией.")
        timeout = self.app.settings.get("mcp", "timeout_seconds", 30)
        if server["transport"] == "stdio":
            env = {k: v for k, v in os.environ.items() if k in {
                "PATH", "HOME", "USER", "LANG", "LC_ALL", "TMP", "TEMP", "TMPDIR", "SYSTEMROOT", "SystemRoot",
                "PATHEXT", "COMSPEC", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy",
                "all_proxy", "no_proxy", "SSL_CERT_FILE", "SSL_CERT_DIR"}}
            for key, value in server.get("env", {}).items():
                env[key], _ = self.app.guard.expand_secrets(value, tool=f"mcp:{sid}", session_id=ctx.session_id)
            info = {**protection, "write_work": not server.get("readonly", False)}
            cwd = Path(server["cwd"])
            from ..guard.control import sealed

            if info["write_work"] and sealed(cwd, "fs.write", self.app.dirs.root):
                raise HumanError("sealed", "MCP нельзя открыть запись в контур доверия", "Выбери отдельную рабочую папку или режим только чтения.")
            prepared = sandbox.prepare(shlex.join(command), info=info, work_dir=cwd, allow_net=bool(server.get("network")),
                                       read_dirs=(server["module_snapshot"],) if server.get("module") else (), temp_root=self.app.dirs.local,raw_argv=command)
            if server.get("module") and self.app.modules.package(server["module"])["metadata"].get("workshop"):
                prepared.close()
                from ..workshop.isolation import Confined

                package = self.app.modules.package(server["module"])
                prepared = Confined(self.app, server["module_snapshot"], ["/package/" + package["metadata"]["mcp"]["entry"]], capabilities=package["metadata"].get("capabilities", []),
                                    network_hosts=package["metadata"].get("network_hosts"), ctx=ctx, active=lambda: self.active(self.get(sid)))
                if prepared.proxy:
                    prepared.proxy.module = package["id"]
                await prepared.start()
                env = dict(prepared.env)
            env.update(prepared.env)
            info_log = self.diag(sid)
            transport = Stdio(prepared.argv or command, cwd=cwd, env=env, cleanup=prepared.close,resource=prepared,log=info_log["log"])
        else:
            host = urlsplit(server["url"]).hostname
            headers = {k: self.app.guard.expand_secrets(v, host=host, tool=f"mcp:{sid}", session_id=ctx.session_id)[0] for k, v in server.get("headers", {}).items()}
            if server['transport']=='sse':
                from .legacy_sse import LegacySse
                transport=LegacySse(self.app.http,server['url'],headers,timeout)
            else:transport = Http(self.app.http, server["url"], headers, timeout)
        client = Client(transport, timeout=timeout)
        if server["transport"] == "stdio":
            import time
            info_log = self.diag(sid)
            info_log.update(started=time.time(), launches=info_log["launches"] + 1, exit=None, agent=self.app.agent_id)
            info_log["log"].append(f"— запуск {time.strftime('%Y-%m-%d %H:%M:%S')} ({'общий для стаи' if self.shared(server) else 'агент ' + self.app.agent_id}) —")
        try:
            await client.connect(input_handler=self.input_handler(server,ctx))
        except BaseException as exc:
            self.clients[sid] = (signature, client)
            self.note_error(sid, exc)
            self.clients.pop(sid, None)
            await client.close()
            raise
        return client

    def input_handler(self,server,ctx):
        if not self.app.settings.get('mcp','client_enabled',False) or not any(server.get('client_features',{}).get(key) is True for key in ('roots','sampling','elicitation')):return None
        from .requests import Requests
        return Requests(self,server,ctx)

    async def discover(self, sid, ctx):
        async with self._locks.setdefault(sid, asyncio.Lock()):
            server = self.get(sid)
            signature = self.signature(server)
            try:
                client = await self.connect(server, ctx)
                definitions = await client.tools()
                found = {}
                for definition in definitions:
                    name = definition.get("name")
                    if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name) or name in found or not isinstance(definition.get("inputSchema"), dict):
                        raise HumanError("mcp_tools", "Некорректное или повторяющееся имя/схема MCP-тула", "Проверь сервер.")
                    found[name] = definition
                if self.signature(self.get(sid)) != signature or not self.active(self.get(sid)):
                    raise HumanError("mcp_changed", "Подключение изменилось во время обнаружения", "Обнови список ещё раз.")
                self.observed[sid] = found
                self._observed_connections[sid] = signature
                self.register()
                return self.public()
            except BaseException as exc:
                self.observed[sid] = {}
                self._observed_connections.pop(sid, None)
                self.note_error(sid, exc)
                await self.disconnect(sid)
                self.register()
                raise

    def approve(self, sid, expected):
        server, found = self.get(sid), self.observed.get(sid)
        signature=self.signature(server)
        if found is None or self._observed_connections.get(sid) != signature or expected != digest({"connection": signature, "tools": found}):
            raise HumanError("mcp_changed", "Список MCP-тулов изменился или ещё не загружен", "Обнови список и просмотри определения перед одобрением.", status=409)
        with self.app.settings.transaction():
            if self.signature(self.get(sid))!=signature:raise HumanError('mcp_changed','Подключение изменилось','Обнаружь определения заново.')
            trust = self.app.settings.group("mcp_trust")
            previous=trust.get(sid,{})
            entry=previous if previous.get('connection')==signature else {}
            trust[sid] = {**entry,"connection": signature, "tools": copy.deepcopy(found)}
            self.app.settings.replace("mcp_trust", trust)
        self.register()
        self.app.journal.write("mcp", f"MCP {sid}: хозяин одобрил определения ({len(found)} тулов)", actor="owner")
        return self.public()

    def public(self):
        servers = []
        for server in self.servers():
            sid, trust = server["id"], self.trust(server["id"])
            found = self.observed.get(sid)
            definitions = found if found is not None else trust.get("tools", {})
            tools = []
            for name in sorted(set(definitions) | set(trust.get("tools", {}))):
                definition = definitions.get(name)
                approved = trust.get("tools", {}).get(name)
                tools.append({"name": name, "alias": tool_name(sid, name), "definition": definition, "previous": approved,
                              "approved": trust.get("connection") == self.signature(server) and definition == approved,
                              "enabled": self.definition(sid, name) is not None})
            servers.append({**server, "agents": server.get("agents"), "visible": self.allowed(server), "enabled": self.active(server) if server.get("workshop_runtime") else server.get("enabled", False),
                            "managed_by_workshop": bool(server.get("workshop_runtime")), "connected": self.connected(sid), "tools": tools,
                            "diagnostics": self.diagnostics(server), **self.catalog.state(sid),
                            'rejected_tools':self.scrub_schema(self.clients[sid][1].rejected_tools) if sid in self.clients else [],
                            'oauth_status':self.oauth.status(sid) if self.app.settings.get('mcp','oauth_enabled',False) else None,
                            "review": digest({"connection": self.signature(server), "tools": found}) if found is not None and self._observed_connections.get(sid) == self.signature(server) else None})
        pending=getattr(self.app.host,'_mcp_input_broker',None)
        return {"enabled": self.app.settings.get("mcp", "enabled", True), "content_enabled":self.app.settings.get('mcp','content_enabled',False),
                'tasks_enabled':self.app.settings.get('mcp','tasks_enabled',False),'subscriptions_enabled':self.app.settings.get('mcp','subscriptions_enabled',False),
                'subscriptions':self._subscriptions.public() if self._subscriptions else {},
                'tasks':self.tasks.public(ToolContext(self.app,actor='owner')) if self.app.settings.get('mcp','tasks_enabled',False) else [],
                'client_enabled':self.app.settings.get('mcp','client_enabled',False),'oauth_enabled':self.app.settings.get('mcp','oauth_enabled',False),'pending_inputs':pending.public() if pending else [],"safe": self.app.safe, "servers": servers}

    async def call(self, sid, name, args, ctx):
        async with self._locks.setdefault(sid, asyncio.Lock()):
            approved = copy.deepcopy(self.definition(sid, name))
            if approved is None:
                raise HumanError("mcp_unapproved", "Определение MCP-тула не одобрено", "Хозяин одобряет его в разделе «MCP»: «Обновить список», затем «Одобрить».")
            server = self.get(sid)
            signature = self.signature(server)
            if server.get("module"):
                await self.app.modules.check_call(server, ctx)
            await ctx.check("mcp.call", f"{sid}/{name}", f"MCP {sid}: {name}", detail={"arguments": self.app.guard.scrub(json.dumps(args, ensure_ascii=False))[:2000]})
            client = await self.connect(server, ctx)
            try:
                # Заново проверяем определения перед каждым выполнением; замеченные изменения не запускаем.
                definitions = await client.tools()
                found = {t.get("name"): t for t in definitions if isinstance(t.get("name"), str)}
                self.observed[sid] = found
                self._observed_connections[sid] = signature
                self.register()
                if self.signature(self.get(sid)) != signature or self.definition(sid,name)!=approved:
                    self.app.host.bus.publish({"type": "mcp_changed", "agent_id": "*", "server": sid})
                    raise HumanError("mcp_changed", "Сервер изменил определение MCP-тула или подключение", "Тул выключен до нового одобрения: хозяин видит кнопку «Одобрить» в разделе «MCP».")
                task_support=self.tasks.enabled(server) if self.app.settings.get('mcp','tasks_enabled',False) else False
                if task_support:
                    self.tasks.capacity()
                    if client.version!='2026-07-28' or 'io.modelcontextprotocol/tasks' not in client.capabilities.get('extensions',{}):task_support=False
                progress = (lambda text: ctx.progress(self.app.guard.scrub(text))) if ctx.progress else None
                with self.tasks.reserve() if task_support else nullcontext():
                    result = await client.request("tools/call", {"name": name, "arguments": args}, progress=progress,schema=approved['inputSchema'],input_handler=self.input_handler(server,ctx),task_support=task_support)
                    if result.get('resultType')=='task':return self.tasks.capture(sid,name,approved,signature,result,ctx)
                content = result.get("content") or []
                if not isinstance(content, list):
                    raise HumanError("mcp_protocol", "Некорректный результат MCP-тула", "Проверь сервер.")
                from .parts import render
                text, parts = await render(ctx, content, origin=f"{sid}/{name}", attach=not result.get("isError"))
                if result.get("structuredContent") is not None:
                    text += "\n" + json.dumps(result["structuredContent"], ensure_ascii=False)
                return ToolResult(untrusted(f"MCP {sid}/{name}", text), ok=not bool(result.get("isError")),
                                  summary=f"MCP {sid}/{name}", tainted=True, model_parts=parts)
            except BaseException as exc:
                if not isinstance(exc, HumanError) or exc.code not in ("mcp_unapproved", "mcp_agent", "mcp_disabled", "denied"):
                    self.note_error(sid, exc)
                await self.disconnect(sid)
                raise

    async def close(self):
        if self._oauth:self._oauth.close()
        if self._subscriptions:await self._subscriptions.close()
        pending=getattr(self.app.host,'_mcp_input_broker',None)
        if pending:pending.cancel(self)
        for sid in list(self.clients):
            await self.disconnect(sid)
        if self._cleanup:
            await asyncio.gather(*list(self._cleanup), return_exceptions=True)
