"""Предохранитель: решение, одобрение хозяином, правила, стоп-кран, секреты, снимки, бортжурнал."""

from __future__ import annotations

import asyncio
import fnmatch
import re
import time
from urllib.parse import urlsplit

from ..core.errors import HumanError
from ..core.ids import new_id
from .checkpoints import Checkpoints
from .journal import Journal
from .policy import CAPS, EXFIL_TRUST, Action, Decision, Policy, hardline_hit, remember_scope

SECRET_REF = re.compile(r"\{\{\s*secret:([\w.:\-]+)\s*\}\}")


class Halted(HumanError):
    def __init__(self):
        super().__init__("halted", "Стоп-кран: агент остановлен хозяином", "Сними стоп в пульте или командой /продолжить.", status=423)


class Guard:
    def file_send_decision(self, action):
        from ..core.pathutil import within
        from .control import sealed
        path=self.policy.resolve(action.target)
        own=within(path,self.app.dirs.work)
        if (not own and within(path,self.app.host.dirs.root)) or self.policy.protected(path) or sealed(path,'fs.read',self.app.host.dirs.root):
            return Decision('deny','Служебные данные, сейф и ключи нельзя отправлять файлами.','channel-file-private',False)
        if own:return Decision('allow','Файл из своей рабочей папки — хозяину.','channel-file-work')
        return Decision('ask','Файл вне рабочей папки: отправку подтверждает хозяин.','channel-file-outside',False)

    def __init__(self, agent_ctx):
        self.app = agent_ctx
        self.policy = Policy(work_dir=agent_ctx.dirs.work, data_dir=agent_ctx.dirs.root,
                             get_settings=lambda: agent_ctx.host.agents.rights.guard_settings(agent_ctx) if hasattr(agent_ctx.host,'agents') else agent_ctx.settings.group("guard"), get_rules=self.rules)
        self.checkpoints = Checkpoints(agent_ctx.dirs.checkpoints)
        self.pending: dict[str, dict] = {} if agent_ctx.host is agent_ctx else agent_ctx.host.guard.pending
        self._turn_grants: dict[str, set] = {}  # сессия → полномочия задачи по расписанию на один ход
        self._session_grants: dict[str, set] = {}  # сессия → способности, разрешённые «в этом разговоре» (до /new или перезапуска)
        self.halted = False

    # правила хозяина

    def rules(self) -> list[dict]:
        r = self.app.settings.group("guard_rules").get("rules")
        return list(r) if isinstance(r, list) else []

    def add_rule(self, capability: str, target: str, verdict: str, note: str = "", actor: str = "", only_clean: bool = True, scope: str = "") -> dict:
        if capability not in CAPS and capability != "*":
            raise HumanError("bad_rule", f"Нет такой способности «{capability}»", ", ".join(CAPS))
        for same in self.rules():  # повторное «Всегда» не плодит одинаковые правила
            if (same.get("capability"), same.get("target") or "*", same.get("verdict"), same.get("actor") or "") == (capability, target or "*", verdict, actor or "") \
                    and same.get("enabled", True) and bool(same.get("only_clean")) == bool(only_clean):
                return same
        rule = {"id": new_id(), "capability": capability, "target": target or "*", "verdict": verdict, "note": note,
                "actor": actor, "only_clean": only_clean, "enabled": True, "created_at": time.time(), **({"scope": scope} if scope else {})}
        rules = self.rules() + [rule]
        self.app.settings.replace("guard_rules", {"rules": rules})
        self.app.journal.write("guard", f"Новое правило: {CAPS.get(capability, capability)} «{rule['target']}» — {'разрешено' if verdict == 'allow' else 'запрещено'}", actor="owner", data=rule)
        return rule

    def remove_rule(self, rid: str) -> None:
        rules = [r for r in self.rules() if r.get("id") != rid]
        self.app.settings.replace("guard_rules", {"rules": rules})
        self.app.journal.write("guard", "Правило удалено", actor="owner", data={"id": rid})

    # полномочия задач по расписанию: разрешено заранее на время одного хода

    def grant(self, session_id: str, grants: list[tuple[str, str]]) -> None:
        self._turn_grants.setdefault(session_id, set()).update((c, t or "*") for c, t in grants)

    def revoke(self, session_id: str, grants: list[tuple[str, str]]) -> None:
        cur = self._turn_grants.get(session_id)
        if cur is not None:
            cur.difference_update((c, t or "*") for c, t in grants)
            if not cur:
                self._turn_grants.pop(session_id, None)

    # стоп-кран

    def halt(self, by: str = "owner") -> None:
        self.halted = True
        for pid in list(self.pending):
            self.respond(pid, "deny", by="halt")
        self.app.journal.write("guard", "Стоп-кран: всё остановлено", actor=by)
        self.app.bus.publish({"type": "halt", "on": True})

    def resume(self, by: str = "owner") -> None:
        self.halted = False
        self.app.journal.write("guard", "Стоп снят", actor=by)
        self.app.bus.publish({"type": "halt", "on": False})

    def off(self) -> bool:
        s = self.policy.get_settings()
        return not s.get("enabled", True) or s.get("profile") == "off"

    def _relaxed(self, a: Action, d: Decision) -> Decision:
        """Выключенный Предохранитель не спрашивает и в особых местах (файл хозяину, переписка агентов); запреты остаются."""
        if d.verdict == "ask" and self.off():
            return Decision("allow", "Предохранитель выключен хозяином — только запись в бортжурнал", "off")
        return d

    def _hard_deny(self, a: Action) -> bool:
        d = self.policy.decide(a)
        return d.verdict == "deny" and (d.rule in ("hardline", "protected", "sealed", "path_alias") or d.rule.startswith("owner-rule:"))

    def session_granted(self, capability: str, session_id: str | None) -> bool:
        if capability in self._session_grants.get(session_id or "", ()):
            return True
        task = getattr(self.app, "_task_agent", None)  # помощник команды пользуется разрешениями разговора, который его позвал
        if task is not None:
            parent = task.task.get("parent_session")
            return task.parent.guard.session_granted(capability, parent) if parent else False
        return False

    # главное: проверить действие

    def _record_decision(self,a,d,session_id):
        turn=getattr(getattr(self.app,"mind",None),"turns",{}).get(session_id)
        self.app.journal.write("decision",f"{_verdict_word(d.verdict)}: {a.summary or CAPS.get(a.capability,a.capability)}",actor=a.actor,
                               data={"capability":a.capability,"target":a.target[:500],"verdict":d.verdict,"reason":d.reason,"rule":d.rule,
                                     "tainted":a.tainted,"unattended":a.unattended,"tool":a.tool,"session":session_id,"channel":getattr(turn,"channel",None),
                                     "agent":getattr(self.app,"agent_id","main"),"policy":self.state()})

    def state(self) -> dict:
        """Действующий профиль и его источник — для отказов и паспорта; без секретов."""
        host=self.app.host
        if hasattr(host,'agents'):return host.agents.rights.guard_state(self.app)
        s=self.policy.get_settings();enabled=bool(s.get('enabled',True)) and s.get('profile')!='off'
        return {'enabled':enabled,'profile':s.get('profile','careful') if enabled else 'off','source':'из настроек агента'}

    async def check(self, a: Action, *, session_id: str | None = None) -> Decision:
        if (self.halted or self.app.host.guard.halted) and a.actor != "owner":
            raise Halted()
        from .control import sealed, updates_command

        if a.actor != 'owner' and a.capability in ('net.read','net.send'):
            try:
                target = urlsplit(a.target)
                port = target.port or (443 if target.scheme=='https' else 80)
                ports = {getattr(self.app.host,'web_port',None), self.app.host.store.meta_get('web:active_port'), self.app.host.settings.get('web','port',8770)}
                if port in ports:
                    d=Decision('deny','Служебный порт пульта недоступен сетевым тулам. Используй свои тулы памяти и настройки.','agent-control',False)
                    self._record_decision(a,d,session_id)
                    return d
            except ValueError:
                return Decision('deny','Некорректный адрес сети.','bad-url',False)
        try:
            blocked=(a.capability.startswith("fs.") and sealed(self.policy.resolve(a.target),a.capability,self.app.dirs.root)) or updates_command(a) or a.capability.startswith('update.')
        except HumanError:
            blocked=True
        if blocked and a.capability=='update.install' and a.actor!='owner' and not updates_command(a):
            # Агент только просит: каждый раз отдельная карточка, её не заменяют ни профиль, ни «в этом разговоре», ни выключенный Предохранитель.
            return await self.require_owner(a,'Агент просит установить обновление программы: бекап, новые коммиты и перезапуск всей стаи.',session_id=session_id)
        if blocked:
            d=self.policy.decide(a)
            self._record_decision(a,d,session_id)
            return d
        if a.capability.startswith('fs.') and hasattr(self.app.host, 'agents') and self.app.host.agents.private_path(self.app, self.policy.resolve(a.target),a.capability,ancestors=not(a.capability=='fs.read' and a.detail.get('listing'))):
            d = Decision('deny', 'Память, рабочая папка и служебные данные другого агента закрыты.', 'agent-private', False)
            self._record_decision(a,d,session_id)
            return d
        if a.capability=='voice.install':
            rule=self.policy._rule_for(a)
            allowed=a.actor=='owner' and not(rule and rule.get('verdict')=='deny')
            d=Decision('allow' if allowed else 'deny','Необязательную голосовую службу устанавливает и запускает только хозяин.','voice-install-owner',False)
            self._record_decision(a,d,session_id)
            return d
        if a.capability=='telegram.topic':
            d=self.app.host.telegram.topics.repair_decision(self.app,a)
            self._record_decision(a,d,session_id)
            return d
        if a.capability=='telegram.leave':
            d=self.app.host.telegram.groups.leave_decision(self.app,a)
            self._record_decision(a,d,session_id)
            return d
        if a.actor != 'owner' and hasattr(self.app.host, 'agents'):
            passive_reply=(a.capability=='agent.send' and a.tool=='agent_reply') or (a.capability=='msg.send' and a.tool in ('telegram_reply','matrix_reply','userbot_reply') and a.actor=='system')
            reason='' if passive_reply else self.app.host.agents.rights.denied(self.app,a)
            if reason:
                d=Decision('deny',reason,'agent-rights',False)
                self._record_decision(a,d,session_id)
                return d
        permissions = self.app.team.permissions(session_id)
        if permissions is not None and a.capability not in permissions["capabilities"]:
            d = Decision("deny", "Способность не выдана роли или родителем.", "team-role", False)
            self._record_decision(a,d,session_id)
            return d
        if a.capability in ('system.restart','system.shutdown','system.service'):
            rule=self.policy._rule_for(a)
            if rule and rule.get('verdict')=='deny':
                d=Decision('deny','Правило хозяина запрещает управление запуском. Настройки → Предохранитель → Правила хозяина.','system-rule',False)
            elif a.actor=='owner':d=Decision('allow','Хозяин явно управляет запуском Бендера.','system-owner',False)
            elif a.capability!='system.restart':d=Decision('deny','Выключение и автозапуск доступны только хозяину.','system-owner',False)
            else:return await self.require_owner(a,'Перезапуск всегда подтверждает хозяин; это разрешение только на один раз.',session_id=session_id)
            self._record_decision(a,d,session_id)
            return d
        if a.capability=='msg.send' and a.target.startswith('telegram:group:'):
            groups=self.app.host.telegram.groups
            d=groups.decide(self.app,a,session_id)  # публичный ответ группе после чужого текста — хозяин и при выключенном (веха 9)
            if d.verdict=='ask':
                d=await self._ask(a,d,session_id)
                current=groups.decide(self.app,a,session_id)
                if current.verdict=='deny':d=current
                if a.actor!='owner' and not passive_reply:
                    reason=self.app.host.agents.rights.denied(self.app,a)
                    if reason:d=Decision('deny',reason,'agent-rights',False)
            self._record_decision(a,d,session_id)
            return d
        if a.target.startswith('userbot:') and a.capability in ('net.read','net.send','msg.send'):
            bot=self.app.userbot
            d=bot.decide(a,session_id)  # аккаунт Telegram агента: список чатов хозяина сильнее профиля Предохранителя
            if d.verdict=='ask':
                if a.unattended:d=Decision('deny',d.reason+' Но спросить некого.','unattended',False)
                else:
                    d=await self._ask(a,d,session_id)
                    current=bot.decide(a,session_id)
                    if current.verdict=='deny':d=current
            self._record_decision(a,d,session_id)
            return d
        if a.capability=='telegram.profile':
            rule=self.policy._rule_for(a)
            if rule and rule.get('verdict')=='deny':
                d=Decision('deny','Правило хозяина запрещает изменение публичного профиля.','telegram-profile-rule',False)
                self._record_decision(a,d,session_id)
                return d
            if a.actor=='owner':
                d=Decision('allow','Хозяин явно подтвердил показанный публичный профиль.','telegram-profile-owner',False)
                self._record_decision(a,d,session_id)
                return d
            return await self.require_owner(a,'Публичное лицо бота меняет только хозяин после просмотра',session_id=session_id)
        if a.capability=='channel.send_file':
            d=self._relaxed(a,self.file_send_decision(a))
            if d.verdict=='ask':
                d=await self._ask(a,d,session_id)
                current=self.file_send_decision(a)
                if current.verdict=='deny':d=current
                if a.actor!='owner':
                    reason=self.app.host.agents.rights.denied(self.app,a)
                    if reason:d=Decision('deny',reason,'agent-rights',False)
            self._record_decision(a,d,session_id)
            return d
        if a.capability=='agent.send':
            correspondence=self.app.host.agents.correspondence
            d=self._relaxed(a,correspondence.decide(self.app,a))
            if d.verdict=='ask':
                d=await self._ask(a,d,session_id)
                current=correspondence.decide(self.app,a)
                if current.verdict=='deny':d=current
                if a.tool!='agent_reply' and a.actor!='owner':
                    reason=self.app.host.agents.rights.denied(self.app,a)
                    if reason:d=Decision('deny',reason,'agent-rights',False)
            self._record_decision(a,d,session_id)
            return d
        grants = self._turn_grants.get(session_id or "", set())
        if (a.capability, a.target) in grants or (a.capability, "*") in grants or self.session_granted(a.capability, session_id) or \
                any(c == a.capability and t not in ("*", a.target) and fnmatch.fnmatch(a.target, t) for c, t in grants):
            if a.capability in ("exec", "mcp.connect", "skill.run") and self.app.settings.get("guard", "hardline", True) and hardline_hit(a.target if a.capability == "exec" else a.detail.get("command", "")):
                pass  # жёсткий список не отменяется ни одобрением, ни полномочиями
            elif self._hard_deny(a):
                pass  # защищённые пути и запреты хозяина сильнее разрешения разговора
            else:
                d=Decision("allow", "Разрешено хозяином в этом разговоре.", "grant")
                if a.capability not in ("fs.read","net.read"):self._record_decision(a,d,session_id)
                return d
        d = self.policy.decide(a)
        if d.verdict == "ask":
            d = await self._ask(a, d, session_id, sweepable=True)
            if a.actor!='owner' and hasattr(self.app.host,'agents'):
                reason=self.app.host.agents.rights.denied(self.app,a)
                if reason:d=Decision('deny',reason,'agent-rights',False)
            current = self.app.team.permissions(session_id)
            if current is not None and a.capability not in current["capabilities"]:
                d = Decision("deny", "Права роли изменились во время ожидания.", "team-role", False)
        if d.verdict != "allow" or d.rule in ("off",) or a.capability not in ("fs.read", "net.read"):
            self._record_decision(a,d,session_id)
        return d

    async def require_owner(self, a: Action, reason: str, *, session_id: str | None = None) -> Decision:
        """Всегда спросить хозяина, что бы ни говорил профиль (ослабление защиты, правила, необратимое).
        Без хозяина рядом — отказ."""
        permissions = self.app.team.permissions(session_id)
        if permissions is not None and a.capability not in permissions["capabilities"]:
            d = Decision("deny", "Способность не выдана роли или родителем.", "team-role", False)
        elif a.unattended:
            d = Decision("deny", reason + " — но спросить некого.", "unattended", False)
        else:
            d = await self._ask(a, Decision("ask", reason, "needs-owner", False), session_id)
            if a.actor!='owner' and hasattr(self.app.host,'agents'):
                denied=self.app.host.agents.rights.denied(self.app,a)
                if denied:d=Decision('deny',denied,'agent-rights',False)
            current = self.app.team.permissions(session_id)
            if current is not None and a.capability not in current["capabilities"]:
                d = Decision("deny", "Права роли изменились во время ожидания.", "team-role", False)
        self._record_decision(a,d,session_id)
        return d

    async def _ask(self, a: Action, d: Decision, session_id: str | None, *, sweepable: bool = False) -> Decision:
        pid = new_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        timeout = float(self.app.settings.get("guard", "approval_timeout", 300))
        pattern, scope, words = remember_scope(self.policy, a)
        if d.rule == "taint" and a.capability in EXFIL_TRUST:
            words += " (после чужого текста всё равно спрошу)"
        card = {"id": pid, "capability": a.capability, "what": CAPS.get(a.capability, a.capability), "target": a.target[:2000],
                "agent_id": self.app.agent_id, "summary": a.summary, "reason": d.reason, "tool": a.tool, "actor": a.actor, "tainted": a.tainted,
                "can_remember": d.remember_ok, "remember": words, "can_session": sweepable and bool(session_id),
                "session": session_id, "ts": time.time(), "timeout": timeout, "detail": a.detail}
        self.pending[pid] = {"card": card, "future": fut, "action": a, "guard": self, "sweepable": sweepable}
        self.app.bus.publish({"type": "approval", "agent_id":"*", "card": card})
        try:
            choice = await asyncio.wait_for(asyncio.shield(fut), timeout)
        except asyncio.TimeoutError:
            choice = "timeout"
        finally:
            self.pending.pop(pid, None)
            self.app.bus.publish({"type": "approval_done", "agent_id":"*", "id": pid})
        if choice in ("once", "task", "always"):
            if choice == "task" and session_id and sweepable:
                self._session_grants.setdefault(session_id, set()).add(a.capability)
                self.app.journal.write("guard", f"Разрешено в этом разговоре: {CAPS.get(a.capability, a.capability)}", actor="owner",
                                       data={"capability": a.capability, "session": session_id})
            if choice == "always" and d.remember_ok:
                self.add_rule(a.capability, pattern, "allow", note=f"всегда {words} — из карточки: {a.summary[:80]}", only_clean=False, scope=scope)
            if choice in ("task", "always") and sweepable:
                self._sweep()
            return Decision("allow", "Хозяин разрешил.", f"owner:{choice}")
        if choice == "timeout":
            return Decision("deny", f"Хозяин не ответил за {int(timeout)} с — считаю отказом.", "timeout")
        if choice == "halt":
            return Decision("deny", "Стоп-кран.", "halt")
        return Decision("deny", "Хозяин запретил.", "owner:deny")

    def respond(self, pid: str, choice: str, by: str = "owner") -> bool:
        item = self.pending.get(pid)
        if not item:
            return False
        if choice not in ("once", "task", "always", "deny"):
            raise HumanError("bad_choice", "Непонятный ответ на карточку", "once, task, always или deny")
        if not item["future"].done():
            item["future"].set_result("halt" if by == "halt" else choice)
        return True

    def _sweep(self) -> None:
        """Ответ «в этом разговоре» или «всегда» закрывает и уже висящие карточки, которые он покрывает,
        — параллельные ходы и помощники не переспрашивают то же самое."""
        for item in list(self.pending.values()):
            guard, fut, a = item.get("guard"), item["future"], item["action"]
            if not item.get("sweepable") or fut.done() or guard is None or guard.app.host is not self.app.host:
                continue
            card = item["card"]
            if guard.session_granted(a.capability, card.get("session")) or guard.policy.decide(a).allowed:
                fut.set_result("once")

    def end_turn(self, session_id: str | None) -> None:
        self._turn_grants.pop(session_id or "", None)

    # секреты

    def expand_secrets(self, text: str, *, host: str = "", tool: str = "", session_id: str | None = None) -> tuple[str, list[str]]:
        """Подставить {{secret:имя}}. Секрет уходит, только если его разрешено использовать здесь
        (хост в списке allow.hosts или тул в allow.tools); иначе — HumanError."""
        permissions = self.app.team.permissions(session_id)
        if SECRET_REF.search(text) and permissions is not None and "secret.use" not in permissions["capabilities"]:
            raise HumanError("team_secret", "Роли не разрешено использовать секреты", "Нужна способность secret.use в пределах прав родителя.")
        used = []

        def sub(m):
            name = m.group(1)
            val = self.app.secrets.get(name)
            if val is None:
                raise HumanError("no_secret", f"Нет секрета «{name}»", "Добавь его в «Сейф» в пульте.")
            allow = self.app.secrets.allow(name) or {}
            hosts = allow.get("hosts") or []
            tools = allow.get("tools") or []
            ok = (host and any(host == h or host.endswith("." + h.lstrip("*.")) for h in hosts)) or (tool and tool in tools)
            if not ok and (hosts or tools):
                raise HumanError("secret_not_here", f"Секрет «{name}» не разрешён для {host or tool}", "Разреши место в «Сейфе».")
            if not ok and not (hosts or tools):
                raise HumanError("secret_not_here", f"Для секрета «{name}» не указано, где его можно использовать", "Открой «Сейф» и укажи хосты или тулы.")
            used.append(name)
            return val

        return SECRET_REF.sub(sub, text), used

    def scrub(self, text: str) -> str:
        """Вычистить значения секретов из вывода, который увидит модель."""
        if not text:
            return text
        for v in self.app.secrets.values():
            if v and v in text:
                text = text.replace(v, "[секрет скрыт]")
        return text


def _verdict_word(v: str) -> str:
    return {"allow": "Разрешено", "ask": "Спросил", "deny": "Запрещено"}.get(v, v)


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


__all__ = ["Guard", "Action", "Decision", "Halted", "host_of"]
