"""bender style: посмотреть и переключить манеру речи агента из терминала."""

from __future__ import annotations

import asyncio
import sys

from ..core.errors import HumanError
from ..core.paths import default_data_dir


async def run(args, app_factory):
    from . import manners
    from ..tools.registry import ToolContext

    app = app_factory(args.data or default_data_dir())
    try:
        ctx = app.agents.get(app.agents.resolve(args.agent)) if args.agent else app
        if args.action == "list":
            cur = manners.active(ctx)
            print("Сейчас: " + (f"«{cur['name']}»" if cur else "свой голос (SOUL.md)"))
            for m in manners.library(ctx.settings):
                print(("→ " if cur and cur["id"] == m["id"] else "  ") + f"{m['id']:<12} {m['name']}" + (f" — {m['description']}" if m["description"] else ""))
            return 0
        if args.action == "show":
            print(manners.section(manners.resolve(ctx.settings, args.name or "")))
            return 0
        item = manners.resolve(ctx.settings, args.name or "") if args.action == "use" else None
        await ToolContext(ctx, actor="owner", session_id="cli").check("manner", item["id"] if item else "off", "выбрать манеру речи из терминала")
        manners.choose(ctx, item["id"] if item else "", actor="owner")
        print("Манера: " + (f"«{item['name']}»" if item else "свой голос") + ". Подействует со следующего шага агента.")
        return 0
    finally:
        await app.close()


def main(args, *, app_factory) -> int:
    try:
        return asyncio.run(run(args, app_factory))
    except HumanError as error:
        print(error.title + (". " + error.hint if error.hint else ""), file=sys.stderr)
        return 2
