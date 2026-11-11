"""Агент просит перезапуск; выключение и служба остаются действиями хозяина."""
from .registry import ToolResult


def register(reg):
    @reg.tool('restart','Попросить перезапуск',group='self')
    async def restart(ctx,reason:str,wait:bool=True):
        """Попросить хозяина перезапустить Бендера через Сторожа, всегда отдельной карточкой.

        reason — причина перезапуска, которую увидит хозяин
        wait — дождаться текущих ходов; false — прервать после подтверждения
        """
        result=await ctx.app.host.lifecycle.request(ctx,'restart',policy='wait' if wait else 'interrupt',reason=reason)
        return ToolResult(result['message'],data=result)

    @reg.tool('compact','Сжать разговор',group='self')
    async def compact(ctx,instructions:str=''):
        """Сжать текущий разговор сейчас: старая часть станет пересказом, место в контексте освободится.
        Нужно, когда разговор длинный, тема сменилась или хозяин попросил «сожми». Последние реплики остаются как есть.

        instructions — как пересказать, своими словами (например, «подробно, сохрани всё про деплой»); сохраняется для следующих сжатий
        """
        from ..core.errors import HumanError
        from ..mind import compact as compactor, context
        agent=ctx.app;sid=ctx.session_id
        session=agent.store.session(sid) if sid else None
        if not session or session.get('kind') in compactor.SKIP_KINDS:raise HumanError('compact','Этот разговор не сжимается','Сжимается главный или обычный разговор с хозяином.')
        if not compactor.enabled(agent):raise HumanError('compact_off','Сжатие выключено хозяином','Настройки → Память → Сжатие разговоров.')
        budget=context.history_budget(agent,context.estimate_tokens(context.system_prompt(agent)))
        result=await compactor.maybe_compact(agent,sid,budget,force=True,during_turn=True,guide=instructions)
        if not result:raise HumanError('compact','Не сжалось',compactor.state(agent,sid).get('error') or 'Сжимать нечего: после прошлого пересказа слишком мало сообщений.')
        return ToolResult('Разговор сжат: старая часть заменена пересказом.',summary='разговор сжат',data={'summary_chars':len(str(result.get('summary') or ''))})

    @reg.tool('check_and_update','Проверить обновление',group='self')
    async def check_and_update(ctx,install:bool=False):
        """Проверить, вышло ли обновление программы Бендер, и показать новые коммиты.
        install=true — проверить и сразу попросить хозяина карточкой установить: после одобрения и конца всех ходов стаи будет бекап, установка и перезапуск.
        Хозяин просит обновиться — вызывай сразу с install=true и не переспрашивай словами: карточка и есть вопрос
        (кнопки в пульте и Telegram; на колонке — вслух, если хозяин включил подтверждения голосом).
        После перезапуска ты получишь событие об обновлении и расскажешь хозяину, что нового.

        install — после проверки попросить установку (карточка хозяину, каждый раз)
        """
        from ..updates import request
        plan=await request.check(ctx)
        commits=plan.get('commits') or []
        if not plan.get('available'):
            return ToolResult('Установлена последняя версия.'+(' Локальная ветка впереди сервера.' if plan.get('ahead') else ''),summary='обновлений нет',data={'available':False})
        listing='\n'.join('• '+c for c in commits[:30])+(f'\n… и ещё {len(commits)-30}' if len(commits)>30 else '')
        if not install:
            return ToolResult('Доступно обновление ('+str(len(commits))+'):\n'+listing+'\nУстановить — check_and_update с install=true: хозяин подтвердит карточкой, словами не спрашивай.',summary='есть обновление',data={'available':True,'commits':commits})
        await request.install(ctx,plan)
        return ToolResult('Хозяин одобрил. Обновление ('+str(len(commits))+') установится после конца всех ходов стаи, затем Бендер перезапустится. '
                          'Закончи ход коротко; после запуска придёт событие, и ты расскажешь хозяину, что нового.\n'+listing,summary='обновление после хода',data={'available':True,'commits':commits,'scheduled':True})
