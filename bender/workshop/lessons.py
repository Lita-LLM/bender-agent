"""Урок — проверяемое предложение памяти, а не изменение полномочий."""
import re

from ..memory import selfdir
from ..mind.review import _clean, entries, similar


def enabled(agent_ctx):
    return not agent_ctx.safe and agent_ctx.settings.get("workshop", "enabled", True) and agent_ctx.settings.get("workshop", "lessons", True)


def correction(text):
    return bool(re.search(r"(?iu)\b(?:не\s+так|неверно|неправильно|переделай|исправь\s+ошибку)\b", text))


async def save(agent_ctx, values, ctx, *, source):
    if not enabled(agent_ctx) or ctx.tainted:
        return []
    saved = []
    for text in _clean(values, 3):
        text = agent_ctx.guard.scrub(text)
        current = agent_ctx.selfdir.read("MEMORY.md")
        if any(similar(text, old) for old in entries(current)) or re.search(r"(?i)игнориру[йя].{0,30}(правил|предохранител)|ignore.{0,30}instructions", text):
            continue
        updated = selfdir.add_entry(current, "Урок: " + text + " <!-- source: lesson -->", directive=False)
        if len(updated) > agent_ctx.settings.get("memory", "core_limit"):
            continue
        await ctx.check("memory.core", "MEMORY.md", "сохранить урок из чистого разбора: " + text[:120])
        agent_ctx.selfdir.write("MEMORY.md", updated)
        saved.append(text)
    if saved:
        agent_ctx.journal.write("lesson", "Уроки: " + "; ".join(saved)[:300], actor=ctx.actor, data={"source": source, "lessons": saved})
        agent_ctx.bus.publish({"type": "memory", "file": "MEMORY.md", "action": "lesson"})
    return saved
