"""Временный агент поручения; отчёт/шаги/артефакты принадлежат задаче родителя."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from ..core.errors import HumanError
from ..core.fsutil import atomic_write
from ..core.pathutil import canonical,relative_path,within
from ..core.store import dumps
from ..tools.registry import ToolContext


class TaskTeam:
    """Координатор родителя знает дерево/роли; закрытие работника не гасит всю команду."""
    def __init__(self, shared):self.shared=shared
    def __getattr__(self,name):return getattr(self.shared,name)
    async def stop(self):pass


class TaskAgent:
    def __init__(self, team, task, role, sid):
        self.team,self.parent,self.task,self.role,self.sid=team,team.app,task,role,sid
        self.ctx=None;self.agent_id='t_'+task['id'].lower()

    def open(self):
        task,role,sid,team=self.task,self.role,self.sid,self.team
        manager=self.parent.host.agents
        record=manager.create(role.title+' · '+task['id'][-6:],agent_id=self.agent_id,awaken=False,profile='custom',temporary=True)
        self.ctx=manager.get(record['id']);self.ctx._task_agent=self
        self.ctx.team=TaskTeam(team)
        permissions=team.permissions(sid)
        rights={**manager.rights.effective(self.parent),'profile':'custom','groups':['*'],
                'tools':list(permissions['tools']),'capabilities':list(permissions['capabilities'])}
        if not role.read_parent_memory:rights['tools']=[n for n in rights['tools'] if n!='recall']
        current=self.parent.guard.policy.get_settings()
        rights['guard_profile']='off' if self.parent.guard.off() else current.get('profile','careful')
        self.ctx.settings.set('agent_rights',rights)
        for group in ('roles','guard','guard_rules','team','team_roles'):
            self.ctx.settings.replace(group,self.parent.settings.group(group))
        self.ctx.settings.replace('tools',{name:'on' for name in rights['tools']})
        self.ctx.settings.set('memory',{'review':False,'compact':False})
        self.ctx.store.new_session('subagent',role.title+': '+task['goal'][:60],sid=sid,meta={'task':task['id'],'parent':task['parent_session']})
        manager.host.store.x('UPDATE flock_agents SET data=? WHERE id=?',(dumps({'profile':'custom','parent':self.parent.agent_id,'task':task['id']}),self.ctx.agent_id))
        team._set(task['id'],worker_agent=self.ctx.agent_id,worker_rights=rights)
        self.ctx.bus.listen(self.on_event)

    def read_dirs(self):
        role=self.team.roles().get(self.task['role'])
        permissions=self.team.permissions(self.sid)
        allowed=role and role.read_parent_memory and 'fs.read' in permissions['capabilities'] and 'recall' in permissions['tools']
        return [self.parent.dirs.self_dir,self.parent.dirs.diary] if allowed else []

    def memory(self):return self.parent.index if self.read_dirs() else None

    def on_event(self,event):
        if event.get('session')==self.sid and event['type'] in ('message','tool_end','turn_end','event'):
            self.mirror()
            self.parent.bus.publish({'type':'task_step','task_id':self.task['id']})

    def mirror(self):
        if self.ctx is None:return
        for table in ('messages','calls'):
            for row in self.ctx.store.q('SELECT * FROM '+table+' WHERE session_id=?',(self.sid,)):
                values=dict(row);values['agent_id']=self.parent.agent_id
                columns=list(values)
                self.parent.store.x('INSERT OR IGNORE INTO '+table+'('+','.join(columns)+') VALUES('+','.join('?' for _ in columns)+')',tuple(values.values()))

    async def artifacts(self, tainted=False):
        """Кодер по-прежнему возвращает файлы в work родителя, через его Предохранитель."""
        if self.ctx._processes is not None:await self.ctx._processes.close()
        permissions=self.team.permissions(self.sid)
        if 'fs.write' not in permissions['capabilities']:return []
        files=[];total=0;plans=[]
        maximum=self.parent.settings.get('team','artifact_mb',64)*1024*1024
        count=self.parent.settings.get('team','artifact_files',100)
        ctx=ToolContext(self.parent,session_id=self.sid,actor='subagent:'+self.task['role'],tainted=tainted,unattended=bool(self.task['unattended']))
        saved=self.parent.dirs.local/'task-outputs'/self.task['id']
        for path in sorted(self.ctx.dirs.work.rglob('*')):
            if path.is_symlink():raise HumanError('task_artifact','Ссылки из временной папки не возвращаются','Пусть задача создаёт обычные файлы.')
            if not path.is_file():continue
            rel=relative_path(path.relative_to(self.ctx.dirs.work).as_posix());target=self.parent.dirs.work/rel
            if not within(path,self.ctx.dirs.work) or not within(target,self.parent.dirs.work):raise HumanError('task_artifact','Файл вне рабочей папки','Результат оставлен в журнале задачи.')
            total+=path.stat().st_size
            if total>maximum or len(plans)>=count:raise HumanError('task_artifact','Результаты задачи превысили файловый предел','Увеличь предел артефактов команды в настройках.')
            plans.append((path,rel,target))
        for path,rel,target in plans:
            await ctx.check('fs.write',str(target),'вернуть файл задачи '+str(rel))
            data=path.read_bytes();target.parent.mkdir(parents=True,exist_ok=True);atomic_write(target,data)
            archive=saved/rel;archive.parent.mkdir(parents=True,exist_ok=True);atomic_write(archive,data)
            files.append({'path':rel.as_posix(),'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()})
        return files

    async def close(self):
        manager=self.parent.host.agents
        ctx=self.ctx or manager.loaded.get(self.agent_id)
        if ctx is not None:await ctx.close()
        manager.loaded.pop(self.agent_id,None)
        manager.host.store.x('UPDATE flock_agents SET archived=1 WHERE id=?',(self.agent_id,))
        root=canonical(manager.host.dirs.root/'agents'/self.agent_id)
        if not within(root,self.parent.host.dirs.root/'agents'):raise HumanError('task_cleanup','Не удалось проверить папку временного агента','Проверь задачу в пульте.')
        if root.exists():shutil.rmtree(root)
        # Удаляются данные заведомо временного контекста, не схема и не чужие строки.
        prefix='agent_'+self.agent_id+'__'
        tables=manager.host.store.q("SELECT name,sql FROM sqlite_master WHERE type='table' AND substr(name,1,?)=?",(len(prefix),prefix))
        virtual=[r['name'] for r in tables if 'CREATE VIRTUAL TABLE' in (r['sql'] or '').upper()]
        for name in virtual:manager.host.store.x('DELETE FROM "'+name+'"')
        for row in tables:
            name=row['name']
            if name in virtual or any(name.startswith(v+'_') for v in virtual):continue
            if name==prefix+'meta':manager.host.store.x('DELETE FROM "'+name+'" WHERE key NOT LIKE \'schema:%\'')
            else:manager.host.store.x('DELETE FROM "'+name+'"')


async def keep(team, task_id):
    task=team.get(task_id)
    if not task or task['status'] not in ('done','error','cancelled'):raise HumanError('task_keep','Дождись завершения задачи','После завершения можно оставить агента.')
    manager=team.app.host.agents
    if task.get('kept_agent'):return manager.record(task['kept_agent'])
    if not task.get('worker_rights'):raise HumanError('task_keep','Это задача прежней версии без временного агента','Создай агента из её роли на экране стаи.')
    role=task['budget']['authority'].get('role',{});name=role.get('title') or task['role']
    record=manager.create(name+' · '+task['id'][-6:],awaken=False,profile='custom')
    manager.host.store.x('UPDATE flock_agents SET role=? WHERE id=?',(name,record['id']))
    ctx=manager.get(record['id']);ctx.settings.set('agent_rights',task['worker_rights'])
    roles=team.app.models.roles();roles['main']=list(roles.get(task['model_role']) or roles.get('main') or [])
    ctx.settings.replace('roles',roles)
    if role.get('prompt'):ctx.selfdir.write('AGENTS.md',role['prompt'])
    sid=ctx.main_session()
    rows=team.app.store.q('SELECT * FROM messages WHERE session_id=? ORDER BY id',(task['session_id'],))
    for row in rows:
        value=dict(row);value.update(session_id=sid,agent_id=ctx.agent_id)
        meta=json.loads(value.get('meta') or '{}');meta.pop('call',None);meta['source_task']=task_id;value['meta']=dumps(meta)
        ctx.store.x('INSERT INTO messages('+','.join(value)+') VALUES('+','.join('?' for _ in value)+')',tuple(value.values()))
    for item in task.get('outputs') or []:
        rel=relative_path(item['path']);source=team.app.dirs.local/'task-outputs'/task['id']/rel;target=ctx.dirs.work/rel
        if not within(source,team.app.dirs.local/'task-outputs'/task['id']) or not within(target,ctx.dirs.work):raise HumanError('task_keep','Небезопасный путь результата','Проверь сохранённые файлы задачи.')
        data=source.read_bytes()
        if hashlib.sha256(data).hexdigest()!=item['sha256']:raise HumanError('task_keep','Сохранённый файл задачи изменился','Создай нового агента без переноса этого файла.')
        target.parent.mkdir(parents=True,exist_ok=True);atomic_write(target,data)
    team._set(task_id,kept_agent=ctx.agent_id)
    ctx.journal.write('agents','Хозяин оставил временного агента задачи',actor='owner',data={'task':task_id})
    return manager.record(ctx.agent_id)
