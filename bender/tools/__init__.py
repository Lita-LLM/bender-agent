"""Руки: базовый набор тулов."""


def register_all(reg) -> None:
    from . import agents, channel, manner, files, look, heart, lifecycle, mail, mcp_content, memory, modules, processes, selftools, sessions, shell, skills, team, userbot, voice, weather, web, workshop, browser, face, screen

    for mod in (files, look, shell, web, browser, face, screen, weather, memory, sessions, heart, team, mail, selftools, skills, processes, modules, workshop, agents, channel, lifecycle, voice, mcp_content, manner, userbot):
        mod.register(reg)
