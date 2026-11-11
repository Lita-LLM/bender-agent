"""Настраиваемые шаблоны команды и явная карта способностей тулов.

Неизвестный тул не выдаётся автоматически. MCP — только явным именем и с
проверками его подключения/вызова; внутренние действия ещё проверяет ToolContext.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, replace

from ..core.errors import HumanError
from ..guard.policy import CAPS

TOOL_CAPS = {
    'sessions':('fs.read',),'session_read':('fs.read',),
    'telegram_profile': ('telegram.profile',), 'telegram_chats': (),
    'send_file': ('channel.send_file',),
    'send_message': ('msg.send',), 'react': ('msg.send',), 'hand_over': ('msg.send',),
    'speak':('audio.speak','msg.send'),'voice_connect':('settings',),
    'import_openclaw': ('self.rules','settings'),
    'agents': (), 'describe_me': ('agent.describe',), 'request_link': ('agent.link_request',), 'message_agent': ('agent.send',), 'assign': ('agent.send',), 'assignments': (), 'assignment_update': (),
    "tg_chats": ("net.read",), "tg_read": ("net.read",), "tg_file": ("net.read", "fs.write"), "tg_send": ("msg.send",), "tg_message": ("msg.send",), "tg_chat": ("msg.send",), "tg_account": ("msg.send",), "tg_call": ("msg.send",),
    "read_file": ("fs.read",), "look": ("fs.read",), "list_dir": ("fs.read",), "find_files": ("fs.read",), "grep": ("fs.read",),
    "write_file": ("fs.write",), "edit_file": ("fs.read", "fs.write"), "recall": ("fs.read",), "note": ("fs.write",),
    "fact": ("memory.core",), "memory": ("memory.core",), "update_self": ("memory.core", "self.rules"),
    "run": ("exec",), "process_start": ("exec",), "process_status": ("exec",), "process_stop": ("exec",),
    "web_fetch": ("net.read",), "browser_open": ("net.read",), "browser_look": ("net.read",), "browser_click": ("net.send",), "browser_type": ("net.send",), "browser_close": (), "express": (), "screen_windows": ("screen.look",), "screen_look": ("screen.look",), "screen_click": ("screen.act",), "screen_type": ("screen.act",), "web_search": ("net.read",), "weather": ("net.read",), "settings": ("settings",),
    "remind": ("schedule",), "schedule": ("schedule",), "intent": ("schedule",),
    "mail_list": ("net.read",), "mail_read": ("net.read",), "mail_ask": ("net.read", "team.spawn"), "mail_send": ("msg.send",),
    "skill_read": ("fs.read",), "skill_run": ("skill.run", "exec"), "module_info": ("mcp.call",), "module_send": ("mcp.call", "msg.send"),
    "spawn_task": ("team.spawn",), "wait_tasks": ("team.spawn",), "tasks": ("team.spawn",),
    "status": (), "plan": (), "manner": ("manner",), "ask_owner": (), 'restart':('system.restart',), 'compact':(), 'check_and_update':('update.check',),
    "run_exams": ("exam.run",),
    "learn_skill": ("skill.learn",),
    "propose_tool": ("workshop.propose", "exam.run"),
}
MODEL_ROLES = ("main", "fast", "smart", "coder")


def bad(hint):
    return HumanError("team_role", "Не удалось сохранить роль команды", hint)


def required(name):
    return ("mcp.connect", "mcp.call") if name.startswith("mcp__") or name in ('mcp_content','mcp_tasks','mcp_diagnose') else TOOL_CAPS.get(name)


def tools_for(caps):
    return tuple(name for name, needs in TOOL_CAPS.items() if set(needs).issubset(caps))


def validate_roles(values):
    if not isinstance(values, dict) or len(values) > 64:
        raise bad("Нужен словарь не более чем из 64 ролей.")
    for name, value in values.items():
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_-]{0,39}", name) or not isinstance(value, dict):
            raise bad("Имя роли: латинские буквы, цифры, _ и -, до 40 знаков; описание — объект.")
        for key, limit in (("title", 120), ("prompt", 16000)):
            if key in value and (not isinstance(value[key], str) or len(value[key]) > limit):
                raise bad(f"{key}: нужна строка до {limit} знаков.")
        caps = value.get("capabilities", [])
        if not isinstance(caps, list) or any(not isinstance(c, str) or c not in CAPS for c in caps) or len(set(caps)) != len(caps):
            raise bad("Способности — список известных имён без повторов.")
        if "model" in value and value["model"] not in MODEL_ROLES:
            raise bad("Роль модели: main, fast, smart или coder.")
        if "enabled" in value and not isinstance(value["enabled"], bool):
            raise bad("Выключатель роли — true/false.")
        if 'read_parent_memory' in value and not isinstance(value['read_parent_memory'],bool):raise bad('Чтение памяти родителя — true/false.')
        schema = value.get("schema")
        if schema is not None and (not isinstance(schema, dict) or len(json.dumps(schema)) > 32000):
            raise bad("Схема ответа — объект JSON Schema до 32 КБ.")
        tools = value.get("tools")
        if "tools" in value and (not isinstance(tools, list) or len(tools) > 128 or any(not isinstance(t, str) or required(t) is None for t in tools)):
            raise bad("Неизвестный тул. MCP указывается полным именем mcp__сервер__тул.")


def configured(agent_ctx, defaults):
    result = {}
    stored = agent_ctx.settings.group("team_roles")
    for name in dict.fromkeys([*defaults, *stored]):
        base = defaults.get(name)
        value = stored.get(name, {})
        if base:
            caps = tuple(dict.fromkeys(c for t in base.tools for c in (required(t) or ())))
            base = replace(base, capabilities=caps, builtin=True)
        else:
            from . import Role
            base = Role(name, (), "fast", "Выполни поручение и сообщи итог.")
        caps = tuple(value.get("capabilities", base.capabilities))
        tools = tuple(value.get("tools", base.tools if name in defaults else tools_for(caps)))
        tools = tuple(t for t in tools if required(t) is not None and set(required(t)).issubset(caps))
        result[name] = replace(base, title=value.get("title", base.title), prompt=value.get("prompt", base.prompt), model=value.get("model", base.model),
                               capabilities=caps, tools=tools, schema=value.get("schema", base.schema), enabled=value.get("enabled", True),
                               read_parent_memory=value.get('read_parent_memory','recall' in tools))
    return result


def public(role):
    return asdict(role)
