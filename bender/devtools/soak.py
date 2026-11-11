"""Сутки расписания за час: только временные данные и локальные фальшивые службы."""
from __future__ import annotations

import asyncio
from contextlib import ExitStack
import gc
import json
import os
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

from ..core.errors import HumanError

MCP = '''import json,sys
for line in sys.stdin:
 r=json.loads(line)
 if 'id' not in r:continue
 m=r['method'];v={'jsonrpc':'2.0','id':r['id']}
 if m=='initialize':v['result']={'protocolVersion':'2025-11-25','capabilities':{'tools':{}},'serverInfo':{'name':'soak','version':'1'}}
 elif m=='tools/list':v['result']={'tools':[{'name':'ping','inputSchema':{'type':'object','properties':{}}}]}
 elif m=='tools/call':v['result']={'content':[{'type':'text','text':'pong'}]}
 else:v['error']={'code':-32601,'message':'Нет метода'}
 print(json.dumps(v),flush=True)
'''


class Clock:
    def __init__(self):
        self.now = time.time()
    def time(self):return self.now
    def __getattr__(self,name):return getattr(time,name)


def sample(app, elapsed, cycle):
    proc = Path('/proc/self')
    rss, threads = None, None
    if (proc/'status').exists():
        for line in (proc/'status').read_text().splitlines():
            if line.startswith('VmRSS:'):rss = round(int(line.split()[1])/1024,2)
            if line.startswith('Threads:'):threads = int(line.split()[1])
    descriptors = list((proc/'fd').iterdir()) if (proc/'fd').exists() else []
    sockets = 0
    for path in descriptors:
        try:sockets += os.readlink(path).startswith('socket:')
        except OSError:pass
    files = list(app.dirs.root.rglob('*'))
    return {'cycle':cycle,'seconds':round(elapsed,2),'rss_mb':rss,'fds':len(descriptors) if descriptors else None,'sockets':sockets,
            'threads':threads,'asyncio':sum(not task.done() for task in asyncio.all_tasks()),'listeners':len(app.bus._listeners),
            'turns':len(app.mind.turns),'http_idle':sum(map(len,app.http._pool.values())),
            'database':app.dirs.db.stat().st_size,'wal':Path(str(app.dirs.db)+'-wal').stat().st_size if Path(str(app.dirs.db)+'-wal').exists() else 0,
            'files':sum(path.is_file() for path in files), 'messages':app.store.one('SELECT COUNT(*) FROM messages')[0],
            'tasks':app.store.one('SELECT COUNT(*) FROM tasks')[0]}


def conclusion(samples):
    first,last = samples[0],samples[-1]
    limits={'rss_mb':8,'fds':4,'sockets':4,'threads':2,'asyncio':3,'listeners':0,'turns':0,'http_idle':6}
    growth={key:round(last[key]-first[key],3) for key in limits if first[key] is not None and last[key] is not None}
    suspicious=[key for key,value in growth.items() if value>limits[key]]
    return {'ok':not suspicious,'growth':growth,'suspicious':suspicious,
            'note':'База, сообщения, задачи и файлы растут от известных событий; RSS включает фальшивые службы. Проверка не доказывает отсутствие любой утечки.'}


async def run(*, seconds=3600, cycles=60, progress=None):
    from ..core.app import App
    from ..devtools.fake_llm import create_router
    from ..devtools.fake_mail import FakeMail
    from ..net.http_server import HttpServer, Router, JSONResponse
    from ..skills.experience import learn
    from ..tools.registry import ToolContext
    from ..heart import __name__ as heart_name
    import importlib
    import sys

    if seconds < 0 or not 1 <= cycles <= 1440:
        raise HumanError('soak_args','Неверная длительность обкатки','Нужно неотрицательное время и от 1 до 1440 циклов.')
    started=time.monotonic();metrics=[];counts={};clock=Clock()
    with tempfile.TemporaryDirectory(prefix='bender-soak-') as folder:
        base=Path(folder);app=App(base/'data');fake=create_router();server=HttpServer(fake);mail=FakeMail();hook_server=None
        app.bus.bind_loop(asyncio.get_running_loop())
        try:
            await server.start('127.0.0.1',0)
            imap,smtp=await mail.start()
            provider=app.models.add_provider(name='Обкатка',kind='openai',base_url=f'http://127.0.0.1:{server.port}/v1',extra={'is_local':True})
            app.models.set_role('main',[{'provider':provider['id'],'model':'script'}])
            app.settings.set('guard',{'profile':'trust','process_count':1024})
            app.settings.set('memory',{'review':False,'compact':False})
            app.settings.set('heart',{'mode':'off'})
            app.settings.set('updates', {'auto': 'off'})  # только фальшивые службы, без git хозяина
            app.settings.set('sleep',{'enabled':False,'promote':False,'rewrite_core':False})
            app.settings.set('backups',{'path':str(base/'backups'),'keep_last':3,'daily':0,'weekly':0,'monthly':0})
            app.settings.set('mail',{'enabled':True,'address':'owner@example.com','imap_host':'127.0.0.1','imap_port':imap,'imap_security':'none',
                                     'smtp_host':'127.0.0.1','smtp_port':smtp,'smtp_security':'none','on_new':'important'})
            app.secrets.set('mail:password','secret')
            app.selfdir.finish_awakening();sid=app.main_session()
            await app.mail.poll()
            hook=app.hooks.create('Обкатка',per_hour=3600)
            hooks=Router()
            @hooks.route('POST','/hook')
            async def receive(req):
                status,value=await app.hooks.receive(hook['id'],req)
                return JSONResponse(value,status=status)
            hook_server=HttpServer(hooks);await hook_server.start('127.0.0.1',0)
            script=app.dirs.work/'soak_mcp.py';script.write_text(MCP)
            await app.mcp.save({'id':'soak','command':[sys.executable,str(script)],'enabled':True,'readonly':True})
            ctx=ToolContext(app,actor='owner',session_id=sid)
            state=await app.mcp.discover('soak',ctx);app.mcp.approve('soak',state['servers'][0]['review'])
            timer=app.heart.add('Обкатка',kind='remind',text='Проверка расписания',every=str(86400/cycles)+'s')
            def count_timer(event):
                if event.get('type')=='job' and event.get('status')=='ok' and (event.get('job') or {}).get('id')==timer.id:
                    counts['timer']=counts.get('timer',0)+1
            app.bus.listen(count_timer)
            clock.now=time.time()+.01
            clock_start=clock.now
            async def step(number):
                app.heart.start()
                for _ in range(3):
                    fake.state.script=[{'text':'Проверка завершена.'}]
                    result=await app.mind.say(sid,'Обкатка '+str(number),actor='owner')
                    if result.get('error'):raise HumanError('soak_turn','Не прошёл ход обкатки','Смотри отчёт.',detail=str(result['error']))
                    await app.mind.idle()
                    counts['owner']=counts.get('owner',0)+1
                await app.tools.run('write_file',{'path':'soak.txt','content':str(number)},ctx)
                fake.state.script=[{'text':'Пересказ завершён.'}]
                task=app.team.spawn('scribe','Перескажи: обкатка',parent_session=sid)
                result=(await app.team.wait([task['id']],timeout=15))[0]
                if result['status']!='done':raise HumanError('soak_task','Субагент не завершился','Смотри отчёт.')
                counts['subagent']=counts.get('subagent',0)+1
                fake.state.script=[{'text':json.dumps({'spam':False,'important':True,'needs_reply':False,'category':'другое','summary':'Тестовое письмо.'})}]
                mail.box.add(subject='Обкатка '+str(number))
                await app.mail.poll()
                while app.mail._bg:await asyncio.gather(*list(app.mail._bg))
                counts['mail']=counts.get('mail',0)+1
                async with await app.http.post(f'http://127.0.0.1:{hook_server.port}/hook',headers={'Authorization':'Bearer '+app.hooks.secret(hook['id'])},json={'text':'событие'}) as response:
                    await response.read()
                    if response.status!=202:raise HumanError('soak_hook','Вебхук не принят','Смотри отчёт.')
                counts['hook']=counts.get('hook',0)+1
                await learn(app.skills,{'name':'soak','description':'Проверка обкатки','body':'Проверь источник и сохрани результат.'},ctx)
                counts['skill']=counts.get('skill',0)+1
                result=await app.mcp.call('soak','ping',{},ctx)
                if not result.ok or 'pong' not in result.content:raise HumanError('soak_mcp','MCP не ответил','Смотри отчёт.')
                counts['mcp']=counts.get('mcp',0)+1
                if number%15==0:
                    fake.state.script=[{'text':'{"summary":"Обкатка","lessons":[],"skills":[]}'}]
                    night=await app.sleep.run(force=True)
                    if night.get('errors'):raise HumanError('soak_sleep','Ошибка сна в обкатке','Смотри отчёт.',detail=str(night['errors']))
                    counts['sleep']=counts.get('sleep',0)+1
                await app.backups.now();counts['backup']=counts.get('backup',0)+1
                await app.mind.idle()
                await app.heart.stop()
                if number and counts.get('timer',0)<number:
                    raise HumanError('soak_timer','Таймер не сработал в ускоренном Сердце','Смотри отчёт.')
                fake.state.requests.clear();mail.box.commands.clear()
                gc.collect()
            with ExitStack() as stack:
                stack.enter_context(patch.object(importlib.import_module(heart_name),'time',clock))
                await asyncio.wait_for(step(0),45)
                metrics.append(sample(app,0,0))
                deadline=time.monotonic()
                for number in range(1,cycles+1):
                    target=deadline+seconds*number/cycles
                    await asyncio.sleep(max(0,target-time.monotonic()))
                    clock.now=clock_start+86400*number/cycles
                    await asyncio.wait_for(step(number),45)
                    if number%5==0 or number==cycles:
                        metrics.append(sample(app,time.monotonic()-deadline,number))
                        if progress:progress(metrics[-1])
            verification=await app.backups.verify(full=True)
            result={'seconds':round(time.monotonic()-started,2),'requested_seconds':seconds,'simulated_hours':24,'cycles':cycles,'warmup':1,
                    'counts':counts,'samples':metrics,'journal_ok':app.journal.verify()['ok'],'database_ok':app.store.one('PRAGMA quick_check')[0]=='ok','backup_ok':verification['ok'],**conclusion(metrics)}
            result['ok']=result['ok'] and result['journal_ok'] and result['database_ok'] and result['backup_ok']
            return result
        finally:
            await app.close()
            await server.stop();await mail.stop()
            if hook_server:await hook_server.stop()


def main(rest,term,*,data=None):
    try:
        minutes=float(rest[0]) if rest and not rest[0].startswith('--') else 60
        if not 0<=minutes<=1440:raise ValueError()
        path=Path(rest[rest.index('--report')+1]).expanduser() if '--report' in rest else None
    except (ValueError,IndexError):
        raise HumanError('soak_args','Нужна длительность в минутах','try soak 60 [--report файл.json]') from None
    def progress(value):term.line(json.dumps(value,ensure_ascii=False))
    result=asyncio.run(run(seconds=minutes*60,progress=progress))
    if path:
        from ..core.paths import default_data_dir
        from ..diagnostics.report import save
        save(data or default_data_dir(),path,json.dumps(result,ensure_ascii=False,indent=2).encode())
    term.line(json.dumps(result,ensure_ascii=False,indent=2))
    return 0 if result['ok'] else 1
