"""Тулы: реестр, схемы аргументов, выполнение с понятными ошибками.

Тул — асинхронная функция `handler(ctx, **аргументы) → ToolResult`. Всё, что тул делает
с последствиями (файлы, команды, сеть, сообщения), он сначала проверяет через
`await ctx.check(Action(...))` — Предохранитель решает, можно ли. Реестр только находит тул,
проверяет аргументы, ловит ошибки, чистит вывод от секретов и обрезает слишком длинное.

Схема аргументов — либо явно (params=), либо из аннотаций функции и строк docstring вида
`имя — описание`.
"""

from __future__ import annotations

import inspect
import json
import re
import time
import types
import typing
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from ..core.errors import HumanError
from ..guard import Halted
from ..guard.policy import Action, Decision
from ..router.types import ToolSpec

MAX_OUTPUT = 30_000  # меньше — никогда не режется; больше — по бюджету истории (mind.context.tool_output_limit)


@dataclass
class ToolResult:
    content: str
    ok: bool = True
    summary: str = ""
    tainted: bool = False  # вывод пришёл из недоверенного источника (сеть, чужие люди, внешние тулы)
    data: dict = field(default_factory=dict)
    model_parts: list = field(default_factory=list)  # вложения отдельно от текстового результата и его метаданных


class Denied(HumanError):
    def __init__(self, decision: Decision):
        super().__init__("denied", "Предохранитель не разрешил", decision.reason, status=403, data={"rule": decision.rule})
        self.decision = decision


LAYERS = {"agent-rights": "потолок прав агента (Стая → Права)", "agent-private": "граница чужих агентов", "team-role": "права роли команды",
          "hardline": "жёсткий список", "protected": "защищённый путь", "sealed": "контур доверия", "unattended": "некого спросить",
          "agent-control": "служебный порт пульта", "path_alias": "неоднозначный путь"}


def denial_layer(app, decision: Decision) -> str:
    """Слой отказа и действующий Предохранитель: по этой строке хозяин воспроизводит отказ без догадок."""
    rule = decision.rule or "?"
    layer = LAYERS.get(rule) or ("правило хозяина" if rule.startswith("owner-rule:") else "профиль Предохранителя" if rule.startswith("profile:") else
                                 "хозяин" if rule.startswith("owner") else rule)
    try:
        st = app.guard.state()
        policy = f"Предохранитель {'включён' if st['enabled'] else 'выключен'}, профиль {st['profile']} ({st['source']})"
    except Exception:  # noqa: BLE001 — строка диагностики не должна ломать сам отказ
        policy = "состояние Предохранителя не прочитано"
    return f"[слой: {layer} — {rule}; {policy}]"


@dataclass
class ToolContext:
    app: Any
    session_id: str | None = None
    actor: str = "agent"
    tainted: bool = False
    unattended: bool = False
    tool: str = ""
    turn: dict = field(default_factory=dict)  # состояние хода (план и т. п.)
    progress: Callable[[str], None] | None = None

    async def check(self, capability: str, target: str = "", summary: str = "", **kw) -> Decision:
        a = Action(capability=capability, target=target, actor=self.actor, tool=self.tool, summary=summary,
                   tainted=self.tainted, unattended=self.unattended, **kw)
        d = await self.app.guard.check(a, session_id=self.session_id)
        if not d.allowed:
            raise Denied(d)
        return d


Handler = Callable[..., Awaitable[ToolResult]]


@dataclass
class Tool:
    name: str
    title: str
    description: str
    parameters: dict
    handler: Handler
    group: str = "base"
    default_on: bool = True
    available: Callable[[Any], bool] | None = None  # (app) → есть ли смысл показывать модели (например, почта подключена)
    external: bool = False  # внешняя JSON Schema: не отбрасывать поля и не менять типы аргументов

    def spec(self) -> ToolSpec:
        return ToolSpec(self.name, self.description, self.parameters)


_PY2JSON = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}


def schema_from_function(fn: Callable) -> dict:
    sig = inspect.signature(fn)
    hints = typing.get_type_hints(fn)
    descs = {}
    for line in (inspect.getdoc(fn) or "").splitlines():
        m = re.match(r"\s*(\w+)\s+—\s+(.+)", line)
        if m:
            descs[m.group(1)] = m.group(2).strip()
    props, required = {}, []
    for name, p in list(sig.parameters.items())[1:]:  # первый — ctx
        if p.kind in (p.VAR_KEYWORD, p.VAR_POSITIONAL):
            continue
        t = hints.get(name, str)
        origin = typing.get_origin(t)
        args = [a for a in typing.get_args(t) if a is not type(None)]
        if origin in (typing.Union, getattr(types, "UnionType", typing.Union)) and args:  # Optional[X] и X | None
            t = args[0]
            origin = typing.get_origin(t)
        base = origin or t
        js = {"type": _PY2JSON.get(base, "string")}
        if base is list:
            inner = typing.get_args(t)
            js["items"] = {"type": _PY2JSON.get(inner[0], "string")} if inner else {}
        if name in descs:
            js["description"] = descs[name]
        props[name] = js
        if p.default is inspect.Parameter.empty:
            required.append(name)
    out = {"type": "object", "properties": props}
    if required:
        out["required"] = required
    return out


class Registry:
    def __init__(self, app):
        self.app = app
        self.tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> None:
        self.tools[tool.name] = tool

    def tool(self, name: str, title: str, *, group: str = "base", params: dict | None = None, default_on: bool = True, available=None):
        def deco(fn: Handler) -> Handler:
            desc = (inspect.getdoc(fn) or "").split("\n\n")[0].strip()
            self.add(Tool(name, title, desc, params or schema_from_function(fn), fn, group, default_on, available))
            return fn

        return deco

    def enabled(self, name: str, *, owner=False) -> bool:
        if not owner and hasattr(self.app.host, "agents") and not self.app.host.agents.tool_allowed(self.app, name): return False
        t = self.tools.get(name)
        if not t:
            return False
        if t.available is not None:
            try:
                if not t.available(self.app):
                    return False
            except Exception:  # noqa: BLE001
                return False
        state = self.app.settings.group("tools").get(name)
        if state is None:
            return t.default_on
        return state == "on" or state is True

    def specs(self, names: list[str] | None = None) -> list[ToolSpec]:
        out = []
        for n, t in self.tools.items():
            if names is not None and n not in names:
                continue
            if self.enabled(n):
                out.append(t.spec())
        return out

    def describe(self) -> list[dict]:
        return [{"name": t.name, "title": t.title, "description": t.description, "group": t.group, "enabled": self.enabled(t.name),
                 "parameters": t.parameters} for t in self.tools.values()]

    async def run(self, name: str, args: dict, ctx: ToolContext) -> ToolResult:
        permissions = self.app.team.permissions(ctx.session_id)
        if permissions is not None and name not in permissions["tools"]:
            return ToolResult(f"Ошибка: тул «{name}» не выдан этой роли или родителем.", ok=False, summary="тул не выдан роли")
        t = self.tools.get(name)
        started = time.monotonic()
        if t is None:
            return ToolResult(f"Ошибка: нет тула «{name}». Доступные: {', '.join(n for n in self.tools if self.enabled(n))}.", ok=False, summary=f"нет тула {name}")
        if not self.enabled(name,owner=ctx.actor=='owner'):
            hint='Настройки → Тулы → '+name
            if hasattr(self.app.host,'agents') and not self.app.host.agents.tool_allowed(self.app,name):
                hint='Стая → Права → Способности / Группы тулов / Отдельные тулы (или шаблон Полный)'
            return ToolResult(f"Ошибка: тул «{name}» выключен хозяином. Хозяин может изменить: {hint}.", ok=False, summary=f"{t.title}: выключен")
        props = (t.parameters or {}).get("properties") or {}
        if not isinstance(args, dict):
            return ToolResult("Ошибка: аргументы тула должны быть объектом JSON.", ok=False, tainted=t.external)
        missing = [k for k in (t.parameters or {}).get("required") or [] if (k not in args if t.external else args.get(k) in (None, ""))]
        if missing:
            return ToolResult(f"Ошибка: не хватает аргументов: {', '.join(missing)}.", ok=False, summary=f"{t.title}: не хватает аргументов")
        clean = {}
        for k, v in args.items():
            if t.external:
                clean[k] = v
                continue
            if k not in props:
                continue  # лишние аргументы модели молча отбрасываем
            want = props[k].get("type")
            try:
                if want == "integer" and not isinstance(v, bool):
                    v = int(v)
                elif want == "number":
                    v = float(v)
                elif want == "boolean" and isinstance(v, str):
                    v = v.strip().lower() in ("true", "1", "да", "yes")
                elif want == "string" and not isinstance(v, str):
                    v = str(v)
                elif want in ("object", "array") and isinstance(v, str):
                    v = json.loads(v) if v.strip() else None  # модель прислала JSON строкой
                    if v is not None and not isinstance(v, dict if want == "object" else list):
                        raise ValueError(want)
            except (TypeError, ValueError):
                return ToolResult(f"Ошибка: аргумент {k} должен быть {want}.", ok=False, summary=f"{t.title}: неверный аргумент")
            clean[k] = v
        ctx.tool = name
        try:
            res = await t.handler(ctx, **clean)
        except Halted:
            raise
        except Denied as e:
            res = ToolResult(f"Отказано Предохранителем: {e.decision.reason}\n{denial_layer(ctx.app, e.decision)}", ok=False, summary=f"{t.title}: отказано",
                             data={"denied": {"rule": e.decision.rule or "", "reason": (e.decision.reason or "")[:300]}})
        except HumanError as e:
            res = ToolResult(f"Ошибка: {e.title}. {e.hint}".strip(), ok=False, summary=f"{t.title}: {e.title}")
        except Exception as e:  # noqa: BLE001
            res = ToolResult(f"Внутренняя ошибка тула {name}: {type(e).__name__}: {e}", ok=False, summary=f"{t.title}: внутренняя ошибка")
        content = self.app.guard.scrub(res.content or "")
        from ..mind.context import tool_output_limit
        limit = tool_output_limit(self.app) if len(content) > MAX_OUTPUT else MAX_OUTPUT
        if len(content) > limit:
            content = content[:limit] + f"\n…[обрезано: всего {len(content)} символов, показано {limit}; читай частями]"
        res.content = content
        res.tainted = res.tainted or t.external
        res.data.setdefault("seconds", round(time.monotonic() - started, 3))
        return res
