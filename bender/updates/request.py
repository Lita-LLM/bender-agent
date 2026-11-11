"""Агент просит обновить программу. Проверка — сразу; установка — после карточки хозяина
и только когда закончатся все ходы стаи (в том числе ход самого агента), затем перезапуск.
После запуска новость о версии (news.announce) будит агента, и он сам рассказывает, что нового."""
import asyncio
import time

from ..core.errors import HumanError

WAIT_LIMIT = 30 * 60  # дольше ходы не ждём: обновление отменяется, хозяину — уведомление


def busy(host):
    for ctx in host.agents.loaded.values():
        if any(turn.task and not turn.task.done() for turn in ctx.mind.turns.values()): return True
    return bool(getattr(host.team, 'running', None))


async def check(ctx):
    host = ctx.app.host
    await ctx.check('update.check', str(host.updater.root), 'проверить, вышло ли обновление программы')
    plan = await host.updater.check()
    host.store.meta_set('updates:checked', time.time())
    host.store.meta_set('updates:available', plan if plan.get('available') else None)
    return plan


async def install(ctx, plan):
    host = ctx.app.host
    task = getattr(host, '_agent_update', None)
    if task and not task.done(): raise HumanError('update_pending', 'Обновление уже ждёт конца ходов', 'Закончи ход: установка начнётся сама.')
    commits = plan.get('commits') or []
    await ctx.check('update.install', str(host.updater.root), 'установить обновление программы: ' + str(len(commits)) + ' нов. коммит(ов), затем перезапуск',
                    detail={'commits': commits[:20], 'target': plan.get('target')})
    host._agent_update = asyncio.ensure_future(_perform(host, plan, ctx.app.agent_id))
    return host._agent_update


async def _perform(host, plan, agent_id):
    try:
        deadline = time.monotonic() + WAIT_LIMIT
        await asyncio.sleep(.5)
        while busy(host):
            if time.monotonic() > deadline: raise HumanError('update_busy', 'Обновление не установлено: ходы стаи не закончились за 30 минут', 'Попроси агента ещё раз или обнови программу в пульте.')
            await asyncio.sleep(1)
        result = await host.updater.install(plan)
        host.store.meta_set('updates:available', None)
        host.journal.write('update', 'Обновление по просьбе агента ' + agent_id, actor='owner', data={'agent': agent_id, 'to': plan.get('target')})
        if result.get('updated'):
            message = await host.updater.restart()
            if 'Сторожа' not in str(message):
                host.bus.publish({'type': 'notify', 'kind': 'update', 'agent_id': '*', 'text': 'Обновление установлено. ' + str(message)})
    except asyncio.CancelledError: raise
    except Exception as exc:  # noqa: BLE001 — итог видит хозяин, а не только бортжурнал
        text = (exc.title + '. ' + exc.hint) if isinstance(exc, HumanError) else 'Обновление не удалось: ' + str(exc)[:300]
        host.journal.write('update', text, actor='system', data={'agent': agent_id})
        host.bus.publish({'type': 'notify', 'kind': 'update', 'agent_id': agent_id, 'text': text})
