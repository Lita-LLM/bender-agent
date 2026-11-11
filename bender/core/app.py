"""Бендер целиком: хранилище, настройки, сейф, Коммутатор, Предохранитель, тулы, Разум.

App собирается одинаково для пульта, терминального чата, Полигона и тестов. Ничего не
запускается в фоне при создании — фоновое (сердце, каналы) включает run().
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from ..core.bus import EventBus
from ..core.ids import new_id
from ..core.paths import DataDir
from ..core.secrets import Secrets
from ..core.settings import Settings
from ..core.store import Store
from ..body import Body
from ..channels.hooks import Hooks
from ..channels.mail import Mail
from ..channels.telegram import Telegram
from ..guard import Guard
from ..guard.journal import Journal
from ..heart import Heart
from ..mcp.server import McpLoop
from ..memory.facts import Facts
from ..memory.index import MemoryIndex
from ..memory.vectors import Embedder
from ..memory.selfdir import SelfDir
from ..mind.loop import Mind
from ..net.http_client import HttpClient
from ..router.config import ModelsConfig
from ..router.core import Switchboard
from ..sleep import Sleep
from ..team import Team
from ..tools import register_all
from ..tools.registry import Registry


class AgentContext:
    def __init__(self, data_dir: Path | str, *, safe: bool = False, http: HttpClient | None = None, host=None, agent_id="main"):
        self.host = host or self
        self.agent_id = agent_id
        self.started = self.host.started if host else time.monotonic()
        from .paths import AgentDataDir
        self.dirs = AgentDataDir(host.dirs, agent_id) if host else DataDir(data_dir)
        self.dirs.ensure()
        self.safe = safe
        from ..agents.store import NamespaceStore
        self.store = NamespaceStore(host.store, agent_id) if host else Store(self.dirs.db)
        try:
            self._assemble(http)
        except BaseException:
            self.store.close()
            raise

    def _assemble(self, http):
        from ..agents.runtime import Bus
        self.bus = Bus(self.host.bus, self.agent_id) if self.host is not self else EventBus()
        self._mcp = None
        self._skills = None
        self._search = None
        self._processes = None
        self._modules = None
        self._matrix=None
        self._speakers=None
        self._userbot=None
        self._userbot_runtime=None
        self._api_runtime=None
        self._versions = None
        self._updater = None
        self._workshop = None
        self._backups = None
        self._voice = None
        self._inspector = None
        if self.host is self:
            self.settings = Settings(self.store, on_change=self._settings_changed, migrate=True)
        else:
            from ..agents.settings import AgentSettings
            self.settings = AgentSettings(self.host.settings, self.store, self._settings_changed)
        self._workshop_flags = (self.settings.get("workshop", "enabled", True), self.settings.get("workshop", "tools_enabled", True), self.settings.get("workshop", "network_proxy", False))
        self.secrets = Secrets(self.dirs.secrets) if self.host is self else self.host.secrets
        if self.dirs.secrets.exists():self.secrets.names()
        if self.host is self:
            self.journal = Journal(self.store, self.dirs.local / "journal.key", on_entry=lambda e: self.bus.publish({"type": "journal", "agent_id":e.get("data",{}).get("agent_id","main"), "entry": e}))
        else:
            from ..agents.runtime import Journal as AgentJournal
            self.journal = AgentJournal(self.host._shared_journal, self.agent_id)
        if self.host is self:
            self._shared_journal = self.journal
            from ..agents.runtime import Journal as AgentJournal
            self.journal = AgentJournal(self._shared_journal, 'main')
        self.settings.on_role_repair=lambda notice,actor:self.journal.write('models','Удалены повреждённые ссылки в ролях: '+str(notice['count']),actor=actor,data=notice)
        self.selfdir = SelfDir(self.dirs.self_dir)
        self.fresh = self.selfdir.ensure()
        if self.fresh:
            self.journal.write("system", "Новый агент: созданы файлы личности, ждёт пробуждения")
        self.http = self.host.http if self.host is not self else http or HttpClient()
        self.models = ModelsConfig(self.settings, self.secrets)
        if self.host is self:
            from ..agents.runtime import ACTIVE
            self.router = Switchboard(self.http, get_providers=self.models.providers, get_secret=self.secrets.get,
                                      get_roles=lambda: (ACTIVE.get() or self).models.roles(), set_secret=lambda name,value:self.secrets.set(name,value))
            self.router.observe = self._observe_fuel
            self.router.role_warning=self._role_warning
            self.router.get_reasoning=lambda:(ACTIVE.get() or self).settings.group('reasoning')
        else:
            from ..agents.runtime import Router as AgentRouter
            self.router = AgentRouter(self.host.router, self)
        self._fuel = None
        self.index = MemoryIndex(self.store, self.dirs)
        self.guard = Guard(self)
        self.tools = Registry(self)
        register_all(self.tools)
        self.mind = Mind(self)
        self.vectors = Embedder(self)
        self.index.embedder = self.vectors
        self.facts = Facts(self.store, self.index)
        self.body = Body(self)
        if self.host is self:
            from ..agents.runtime import ACTIVE
            self.router.economy = lambda: self.agents.economy(ACTIVE.get() or self) if hasattr(self,"agents") else self.body.economy()
        self.heart = Heart(self)  # бегун задач запускает run() (server_main), не конструктор
        self.sleep = Sleep(self)
        self.telegram = self.host.telegram if self.host is not self else Telegram(self)  # канал запускает run() (server_main)
        self.mail = self.host.mail if self.host is not self else Mail(self)  # почта — тоже из server_main
        self.hooks = self.host.hooks if self.host is not self else Hooks(self)
        self.team = Team(self)
        self._cli_bridge = McpLoop(self)
        if self.host is self:
            from ..agents.runtime import ACTIVE
            self.router.bridge_provider = lambda: (ACTIVE.get() or self)._cli_bridge
        #  # Claude Code получает тулы Бендера через петлевой MCP
        self.questions: dict[str, dict] = {} if self.host is self else self.host.questions
        if not self.safe and self.settings.get("mcp", "servers", []):
            _ = self.mcp  # только регистрация одобренных схем; процессы запускаются по требованию

    def __getattr__(self, name):
        if self.host is not self and name in ('agents', 'request_restart', 'watched', 'web_port'):
            return getattr(self.host, name)
        raise AttributeError(name)

    def _observe_fuel(self, candidate, account, kind, data):
        self.fuel.observe(candidate, account, kind, data)

    def _role_warning(self, role, entry, reason):
        from ..agents.runtime import ACTIVE
        ctx=ACTIVE.get() or self
        ctx.journal.write('models','Пропущен кандидат роли '+role+': '+reason,data={'role':role,'candidate':entry})

    @property
    def api_runtime(self):
        if self.host is not self:return self.host.api_runtime
        if self._api_runtime is None:
            from ..channels.api_context import Runtime
            self._api_runtime=Runtime(self)
        return self._api_runtime

    @property
    def matrix(self):
        if self.host is not self:return self.host.matrix
        if self._matrix is None:
            from ..modules.matrix_channel import Matrix
            self._matrix=Matrix(self)
        return self._matrix

    @property
    def speakers(self):
        if self.host is not self:return self.host.speakers
        if self._speakers is None:
            from ..speakers.channel import Speakers
            self._speakers=Speakers(self)
        return self._speakers

    @property
    def userbot(self):
        """Аккаунт Telegram этого агента — свой у каждого."""
        if self._userbot is None:
            from ..userbot.channel import Userbot
            self._userbot=Userbot(self)
        return self._userbot

    @property
    def userbot_runtime(self):
        if self.host is not self:return self.host.userbot_runtime
        if self._userbot_runtime is None:
            from ..userbot.runtime import Runtime
            self._userbot_runtime=Runtime(self)
        return self._userbot_runtime

    @property
    def fuel(self):
        if self.host is not self: return self.host.fuel
        if self._fuel is None:
            from ..router.fuel import Fuel
            self._fuel = Fuel(self)
        return self._fuel

    @property
    def inspector(self):
        if self._inspector is None:
            from ..inspectors.memory import Inspector

            self._inspector = Inspector(self)
        return self._inspector

    @property
    def voice(self):
        if self._voice is None:
            from ..voice.manager import Voice

            self._voice = Voice(self)
        return self._voice

    @property
    def backups(self):
        if self.host is not self: return self.host.backups
        if self._backups is None:
            from ..backups.manager import Backups

            self._backups = Backups(self)
        return self._backups

    @property
    def mcp(self):
        if self._mcp is None:
            from ..mcp.manager import Manager

            self._mcp = Manager(self)
        return self._mcp

    @property
    def workshop(self):
        if self._workshop is None:
            from ..workshop.manager import Workshop

            self._workshop = Workshop(self)
        return self._workshop

    @property
    def updater(self):
        if self.host is not self: return self.host.updater
        if self._updater is None:
            from ..updates.git import GitUpdate
            self._updater = GitUpdate(self)
        return self._updater

    @property
    def versions(self):
        if self.host is not self: return self.host.versions
        if self._versions is None:
            from ..updates.manager import Versions

            self._versions = Versions(self)
        return self._versions

    @property
    def modules(self):
        if self._modules is None:
            from ..modules.manager import Modules

            self._modules = Modules(self)
        return self._modules

    @property
    def processes(self):
        if self._processes is None:
            from ..tools.processes import Processes

            self._processes = Processes(self)
        return self._processes

    @property
    def search(self):
        if self._search is None:
            from ..net.search import Search

            self._search = Search(self)
        return self._search

    @property
    def skills(self):
        if self._skills is None:
            from ..skills.manager import Skills

            self._skills = Skills(self)
        return self._skills

    def _settings_changed(self, group, changes):
        loop=getattr(self.host.bus,'_loop',None)
        if loop and not loop.is_closed():
            try:current=asyncio.get_running_loop()
            except RuntimeError:current=None
            if current is not loop:
                loop.call_soon_threadsafe(self._settings_changed,group,dict(changes))
                return
        from .settings import SCHEMA
        schema=SCHEMA.get(group)
        changed=[group+'.'+key for key in changes if schema and schema.field(key) and schema.field(key).restart]
        if changed:
            if not hasattr(self.host,'_restart_fields'):self.host._restart_fields=set()
            self.host._restart_fields.update(changed)
        if group == "team_roles" and hasattr(self, "team"):
            self.team.roles_changed()
        runtime=getattr(self.host,'_api_runtime',None)
        if runtime is not None and group in ('api','api_rights','agent_rights','guard','tools','team_roles','mcp','mcp_trust','modules','module_trust','workshop','processes','skills'):
            runtime.invalidate(None if self is self.host else self.agent_id)
        event = {"type": "settings", "group": group, "keys": list(changes)}
        if self is self.host and group == 'flock': event['agent_id'] = '*'
        self.bus.publish(event)
        if self is self.host and group in ('matrix','modules','module_trust','mcp','mcp_trust') and self._matrix is not None:
            self._matrix.reconfigure()
        if group=='userbot' and self._userbot is not None:self._userbot.reconfigure()
        if self is self.host and group=='speakers':
            # канал колонок ленивый: включили на ходу — запустить, а не ждать перезапуска Бендера
            if self._speakers is not None:self._speakers.reconfigure()
            elif getattr(self,'_speakers_ready',False):self.speakers.start()
        if group == "mcp" and self._mcp is not None:
            self._mcp.invalidate()
        if group == "skills" and self._skills is not None:
            self._skills.stop()
        if group == "processes" and self._processes is not None:
            self._processes.stop()
        if group == "modules" and self._mcp is not None:
            self._mcp.invalidate()
        if group == "workshop" and self._workshop is not None and not self.settings.get("workshop", "enabled", True):
            self._workshop.stop()
        if group == "workshop":
            flags = (self.settings.get("workshop", "enabled", True), self.settings.get("workshop", "tools_enabled", True), self.settings.get("workshop", "network_proxy", False))
            if self._mcp is not None and flags != self._workshop_flags:
                self._mcp.invalidate()
            self._workshop_flags = flags

    # главная сессия: один разговор с хозяином на все его каналы

    def main_session(self) -> str:
        sid = self.store.meta_get("main_session")
        if not sid or not self.store.session(sid):
            sid = self.store.new_session("main", "Главная")
            self.store.meta_set("main_session", sid)
        return sid

    # вопрос хозяину (тул ask_owner)

    async def ask_owner(self, question: str, options: list[str], *, session_id: str | None = None, timeout: float = 1800) -> str | None:
        qid = new_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        card = {"id": qid, "question": question, "options": options, "session": session_id, "agent_id":self.agent_id, "ts": time.time()}
        self.questions[qid] = {"card": card, "future": fut}
        self.bus.publish({"type": "question", "agent_id":"*", "card": card})
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self.questions.pop(qid, None)
            self.bus.publish({"type": "question_done", "agent_id":"*", "id": qid})

    def answer(self, qid: str, text: str) -> bool:
        q = self.questions.get(qid)
        if not q or q["future"].done():
            return False
        q["future"].set_result(text)
        return True

    # деньги

    def cost_today(self) -> float:
        t = time.localtime()
        start = time.mktime((t.tm_year, t.tm_mon, t.tm_mday, 0, 0, 0, 0, 0, -1))
        return self.store.cost_since(start)

    # уборка: старые снимки запросов к моделям и снимки файлов (при старте и раз в сутки)

    def housekeeping(self, keep_days: float = 7.0) -> dict:
        done = {"call_snapshots": self.store.prune_call_requests(keep_days), "checkpoint_days": self.guard.checkpoints.prune(keep_days)}
        if self.host is self and (self.dirs.root / 'agents').is_dir():  # снимки файлов агентов стаи — тот же срок
            from ..guard.checkpoints import Checkpoints
            for private in (self.dirs.root / 'agents').iterdir():
                if private.is_dir() and not private.is_symlink() and not (private / 'checkpoints').is_symlink():
                    done["checkpoint_days"] += Checkpoints(private / 'checkpoints').prune(keep_days)
        if self.host is self:
            done["migration_copies"] = self.store.prune_migration_copies()
        if any(done.values()):
            self.journal.write("system", f"Уборка: снимков запросов — {done['call_snapshots']}, дней снимков файлов — {done['checkpoint_days']}, "
                               f"старых копий базы — {done.get('migration_copies', 0)}", data=done)
        return done

    # стоп-кран

    def halt(self) -> None:
        if self.host is not self: return self.host.halt()
        if hasattr(self, 'agents'):
            self.agents.correspondence.cancel()
            for ctx in list(self.agents.loaded.values()):
                if ctx is not self:
                    ctx.mind.stop()
                    if ctx._processes is not None: ctx._processes.stop()
        if self._api_runtime is not None:self._api_runtime.invalidate()
        self.guard.halt()
        if self._workshop is not None:
            self._workshop.stop()
        self.mind.stop()
        if self._processes is not None:
            self._processes.stop()
        if self._mcp is not None:
            self._mcp.invalidate()
        if self._skills is not None:
            self._skills.stop()

    async def close(self) -> None:
        if getattr(self,'_closed',False):return
        self._closed=True
        if self.host is not self and getattr(self.host,'_api_runtime',None) is not None:self.host._api_runtime.invalidate(self.agent_id)
        if self.host is self and self._api_runtime is not None:await self._api_runtime.close()
        if self.host is self and self._matrix is not None:await self._matrix.close()
        if self.host is self and self._speakers is not None:await self._speakers.close()
        if self._userbot is not None:await self._userbot.close()
        if self.host is self and hasattr(self,'_lifecycle'):await self._lifecycle.close()
        if self.host is self:await self.hooks.close()  # раньше остановки ходов: остановка — не «провал» события
        contexts=list(self.agents.loaded.values()) if self.host is self and hasattr(self,'agents') else [self]
        primary=[t.task for ctx in contexts for t in list(ctx.mind.turns.values()) if t.task and t.task is not asyncio.current_task()]
        for ctx in contexts:ctx.mind.stop()
        for ctx in contexts:await ctx.heart.stop()
        if primary:await asyncio.gather(*primary,return_exceptions=True)
        background=[task for ctx in contexts for task in list(ctx.mind.background) if task is not asyncio.current_task()]
        if background:await asyncio.gather(*background,return_exceptions=True)
        if self.host is self:
            jobs=[job for job in getattr(self,'_import_jobs',()) if job is not asyncio.current_task()]
            if jobs:await asyncio.gather(*jobs,return_exceptions=True)
        await self.team.stop()
        if self.host is self and hasattr(self, 'agents'):
            await self.agents.correspondence.close()
            for ctx in list(self.agents.loaded.values()):
                if ctx is not self and ctx.agent_id in self.agents.loaded: await ctx.close()
        self.mind.stop()
        if self._workshop is not None:
            self._workshop.stop()
        if self._processes is not None:
            await self._processes.close()
        if self._skills is not None:
            await self._skills.close()
        if self._mcp is not None:
            await self._mcp.close()
        await self.team.stop()
        await self._cli_bridge.stop()
        if self.host is self:
            await self.telegram.stop()
            await self.mail.stop()
        await self.heart.stop()
        self.mind.stop()
        if self.host is self: await self.http.close()
        else: self.bus.dispose()
        self.store.close()


class App(AgentContext):
    """Общее ядро; прежние атрибуты остаются контекстом main для старых клиентов."""
    def __init__(self, data_dir, *, safe=False, http=None):
        super().__init__(data_dir, safe=safe, http=http)
        from ..agents.manager import Agents
        self.agents = Agents(self)
        self.settings.authorize=lambda group,changes:self.agents.rights.settings(self,group,changes)
        from ..agents.runtime import ACTIVE
        self.router.authorize=lambda:self.agents.rights.check_model(ACTIVE.get() or self)

    @property
    def lifecycle(self):
        if not hasattr(self,'_lifecycle'):
            from .lifecycle import Lifecycle
            self._lifecycle=Lifecycle(self)
        return self._lifecycle


def main(args) -> int:
    from .server_main import run

    return run(args)
