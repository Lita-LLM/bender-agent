"""Одинаковое действие «убрать навык» для каналов хозяина."""
from ..core.errors import HumanError
from .experience import update


async def archive(agent_ctx,name,ctx,expected=None):
    item=agent_ctx.skills.package(name)
    if expected and item['hash']!=expected:
        raise HumanError('skill_changed','Навык изменился после уведомления','Просмотри текущую версию в пульте → Навыки.')
    await ctx.check('skill.learn',name,'убрать навык из активных')
    update(agent_ctx.skills,name,archived=True)
    agent_ctx.journal.write('skill','Навык убран: '+name,actor='owner')
    agent_ctx.bus.publish({'type':'skills','name':name})
    return 'Навык убран: '+name
