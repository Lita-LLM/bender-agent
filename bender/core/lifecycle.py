"""Общий перезапуск/выключение: решение хозяина, ожидание ходов и чистый выход."""
import asyncio
import time

from .errors import HumanError
from .run_lock import launch_kind


class Lifecycle:
    def __init__(self,host):
        self.host=host;self.task=None;self.pending=None

    def state(self):
        busy=[]
        for ctx in self.host.agents.loaded.values():
            for sid,turn in ctx.mind.turns.items():
                if turn.task and not turn.task.done():busy.append({'agent':ctx.agent_id,'session':sid,'title':(ctx.store.session(sid) or {}).get('title',sid)})
        return {'busy':busy,'pending':self.pending,'kind':launch_kind(),'watched':bool(getattr(self.host,'watched',False)),
                'restart_fields':sorted(getattr(self.host,'_restart_fields',set()))}

    async def request(self,ctx,action,*,policy='wait',mode='same',notice=None,reason=''):
        if action not in ('restart','shutdown') or policy not in ('wait','interrupt') or mode not in ('same','normal','safe'):
            raise HumanError('control','Неизвестный режим управления','Выбери перезапуск/выключение и дождаться/прервать.')
        callback=getattr(self.host,'request_'+action,None)
        if not callback or action=='restart' and not getattr(self.host,'watched',False):
            hint='Останови Ctrl+C и запусти bender снова.' if action=='restart' else 'Останови процесс Ctrl+C в его терминале.'
            raise HumanError('no_watchdog','Перезапуск без Сторожа недоступен' if action=='restart' else 'Эта копия не управляет работающим процессом',hint,status=409)
        reason=ctx.app.guard.scrub(str(reason))[:500]
        await ctx.check('system.'+action,self.host.dirs.root.as_posix(),('Перезапустить' if action=='restart' else 'Выключить')+' Бендера'+(': '+reason if reason else ''),detail={'policy':policy,'mode':mode})
        if self.task and not self.task.done():
            raise HumanError('control_pending','Остановка уже запрошена','Дождись завершения выбранного действия.',status=409)
        self.host._draining=True
        self.pending={'action':action,'policy':policy,'mode':mode}
        self.host.bus.publish({'type':'lifecycle','agent_id':'*',**self.pending})
        self.task=asyncio.create_task(self._perform(callback,action,policy,mode,notice))
        kind=launch_kind()
        start='Запустить снова: systemctl --user start bender.' if kind=='systemd' else 'Запустить снова: bender.'
        return {'ok':True,'pending':self.pending,'message':('Бендер перезапускается через Сторожа.' if action=='restart' else 'Бендер выключается. '+start)}

    async def _perform(self,callback,action,policy,mode,notice):
        try:
            await asyncio.sleep(.5)  # HTTP/Telegram успевают отдать подтверждение хозяину.
            contexts=list(self.host.agents.loaded.values())
            tasks=[turn.task for ctx in contexts for turn in ctx.mind.turns.values() if turn.task and not turn.task.done()]
            if policy=='interrupt':
                for ctx in contexts:ctx.mind.stop()
            if tasks:await asyncio.gather(*tasks,return_exceptions=True)
            if action=='restart':
                if mode!='same':
                    from .watchdog import load_boot,save_boot
                    boot=load_boot(self.host.dirs.root)
                    if mode=='normal':boot.update(safe=None,crashes=[])
                    else:boot['safe']={'since':time.time(),'reason':'включён хозяином'}
                    save_boot(self.host.dirs.root,boot)
                if notice:self.host.store.meta_set('lifecycle:restart_notice',{**notice,'at':time.time()})
            self.host.journal.write('system',('Перезапуск' if action=='restart' else 'Выключение')+' по просьбе хозяина',actor='owner',data={'policy':policy,'mode':mode})
            callback()
        except asyncio.CancelledError:raise
        except Exception as exc:
            from .errors import unexpected
            error=unexpected(exc,'управление запуском')
            self.host.journal.write('system',error.title,data={'code':error.code})
            self.host.bus.publish({'type':'notify','agent_id':'*','text':error.title+'. '+error.hint})
            self.host._draining=False;self.pending=None

    async def close(self):
        if self.task and not self.task.done() and self.task is not asyncio.current_task():
            self.task.cancel();await asyncio.gather(self.task,return_exceptions=True)
