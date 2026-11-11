"""Реестр стаи: контексты загружаются лениво, main остаётся на прежнем месте."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from contextvars import ContextVar
import json
import time
from ..core.errors import HumanError
from ..core.ids import new_id
from ..core.pathutil import within, canonical
from ..core.store import dumps
from .store import valid_id

SCHEMA = ["""CREATE TABLE IF NOT EXISTS flock_agents (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, role TEXT NOT NULL DEFAULT '', archived INTEGER NOT NULL DEFAULT 0,
 temporary INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, data TEXT NOT NULL DEFAULT '{}')"""]
BACKGROUND = ContextVar('bender_background_lane', default=None)
DESCRIPTION_LIMIT = 200  # короткая визитка агента: чем занимается, в чём силён — не резюме


class BackgroundLease:
    def __init__(self,semaphore):
        self.semaphore,self.owner,self.held=semaphore,asyncio.current_task(),False
        self.pauses=0;self.restore=False;self.closed=False


class Agents:
    def overloaded(self):
        """Цикл Бендера откликается медленно (сторож цикла видит задержку больше секунды) и хозяин не отключал притормаживание."""
        watch=getattr(self.host,'loop_watch',None)
        return bool(watch and watch.busy() and self.host.settings.get('flock','shed_load',True))

    async def calm(self, limit=30.0):
        """Плановое дело ждёт, пока цикл свободен, но не дольше limit секунд: разговор с хозяином не ждёт никогда."""
        end=time.monotonic()+limit
        while self.overloaded() and time.monotonic()<end:
            await asyncio.sleep(2)

    @asynccontextmanager
    async def background(self):
        existing=BACKGROUND.get()
        if existing is not None and existing.owner is asyncio.current_task():
            yield
            return
        await self.calm()
        lease=BackgroundLease(self.background_limit)
        await lease.semaphore.acquire();lease.held=True
        token=BACKGROUND.set(lease)
        try:yield
        finally:
            lease.closed=True
            if lease.held:lease.semaphore.release();lease.held=False
            BACKGROUND.reset(token)

    @asynccontextmanager
    async def pause_background(self, source=None):
        lease=source or BACKGROUND.get();paused=False;cancelled=False
        if lease is not None and not lease.closed and (source is not None or lease.owner is asyncio.current_task()):
            lease.pauses+=1;paused=True
            if lease.held:lease.semaphore.release();lease.held=False;lease.restore=True
        try:yield
        except asyncio.CancelledError:
            cancelled=True
            raise
        finally:
            if paused:
                lease.pauses-=1
                if not lease.pauses and lease.restore:
                    lease.restore=False
                    if not cancelled and not lease.closed and not getattr(self.host,'_closed',False):
                        await lease.semaphore.acquire()
                        if lease.closed or getattr(self.host,'_closed',False):lease.semaphore.release()
                        else:lease.held=True

    def __init__(self, host):
        self.host = host
        self.loaded = {'main':host}
        self._costs = {}
        from .rights import Rights
        self.rights = Rights(self)
        host.store.migrate('flock', 1, SCHEMA)
        if not host.store.one("SELECT 1 FROM flock_agents WHERE id='main'"):
            host.store.x("INSERT INTO flock_agents(id,name,created) VALUES('main','Главный',?)", (time.time(),))
        tags=[]
        for table in ('sessions','messages','calls','journal','mem_docs','mem_chunks','facts','jobs','job_runs','tasks'):
            fields = host.store.q('PRAGMA table_info('+table+')')
            if fields and 'agent_id' not in {r['name'] for r in fields}:tags.append("ALTER TABLE "+table+" ADD COLUMN agent_id TEXT NOT NULL DEFAULT 'main'")
        host.store.migrate('flock_tags',1,tags)
        host._shared_journal.agent_column = True
        from .correspondence import Correspondence
        self.correspondence = Correspondence(self)
        from .assignments import Assignments
        self.assignments = Assignments(self)
        self.background_size = host.settings.get("flock","background_parallel",0) or (2 if self._small() else 4)
        self.background_limit = asyncio.Semaphore(self.background_size)

    @staticmethod
    def _small():
        import os, platform
        return platform.machine().lower() in ('aarch64','arm64','armv7l') or (os.cpu_count() or 2) <= 4

    def record(self, agent):
        valid_id(agent)
        row=self.host.store.one('SELECT * FROM flock_agents WHERE id=?',(agent,))
        if not row:raise HumanError('agent_missing','Такого агента нет','Выбери агента в стае.',status=404)
        result=dict(row);result['data']=json.loads(result['data']);return result

    def get(self, agent='main', *, archived=False):
        record=self.record(agent)
        if record['temporary'] and record['archived'] and agent not in self.loaded:raise HumanError('agent_temporary_done','Временный агент уже завершён','Хозяин может оставить его на экране задачи.')
        if agent != "main" and not archived and not self.host.settings.get("flock","enabled",True):raise HumanError("flock_off","Стая выключена","Включи её в Настройках; main доступен.")
        if record['archived'] and not archived:raise HumanError('agent_archived','Агент в архиве','Хозяин может разбудить его в Стае.')
        if agent not in self.loaded:
            from ..core.app import AgentContext
            self.loaded[agent]=AgentContext(self.host.dirs.root, safe=self.host.safe, host=self.host, agent_id=agent)
            ctx=self.loaded[agent]
            ctx.settings.authorize=lambda group,changes:self.rights.settings(ctx,group,changes)
        return self.loaded[agent]

    def resolve(self, value):
        found=[a['id'] for a in self.public() if value in (a['id'],a['name'])]
        if len(found)!=1:raise HumanError('agent_missing','Не найден единственный агент с таким именем','Укажи id из списка стаи.')
        return found[0]

    COST_TTL=4.0  # секунд: сумма за день считается полным обходом таблицы вызовов, а пульт спрашивает её на каждое событие

    def cost(self, agent, *, fresh=False):
        import time
        valid_id(agent)
        cached=self._costs.get(agent)
        if cached and not fresh and time.monotonic()-cached[0]<self.COST_TTL:return cached[1]
        table='calls' if agent=='main' else 'agent_'+agent+'__calls'
        start=time.localtime();start=time.mktime((start.tm_year,start.tm_mon,start.tm_mday,0,0,0,0,0,-1))
        if not self.host.store.one("SELECT 1 FROM sqlite_master WHERE name=?",(table,)):value=0.0
        else:value=float(self.host.store.one('SELECT COALESCE(SUM(cost),0) AS cost FROM "'+table+'" WHERE created_at>=?',(start,))['cost'])
        self._costs[agent]=(time.monotonic(),value)
        return value

    def total_cost(self, *, fresh=False):
        return sum(self.cost(r['id'],fresh=fresh) for r in self.host.store.q('SELECT id FROM flock_agents WHERE temporary=0'))

    def economy(self, ctx):
        settings=self.host.settings.group('budget');limit=float(settings.get('daily_usd') or 0)
        return ctx.body.economy() or bool(limit and settings.get('economy_auto',True) and self.total_cost()>=limit)

    def public(self, *, include_temporary=False):
        out=[]
        for row in self.host.store.q('SELECT * FROM flock_agents ORDER BY created,id'):
            record=dict(row);data=json.loads(record.pop('data'))
            if record['temporary'] and not include_temporary:continue
            ctx=self.loaded.get(record['id'])
            record['name']=(ctx.selfdir.identity().get('name') or record['name']) if ctx else record['name']
            record['loaded']=ctx is not None
            record['cost_today']=round(self.cost(record['id']),6)
            record['profile']=self.rights.effective(ctx)['profile'] if ctx else data.get('profile','full' if record['id']=='main' else 'helper')
            record['description']=str(data.get('description') or '')
            record['described_by']=str(data.get('described_by') or '')
            out.append(record)
        return out

    def card(self, record):
        """Что видят соседи по стае: имя, роль и, если хозяин разрешил, описание. Чужую память и разговоры не показываем."""
        out={k:record[k] for k in ('id','name','role')}
        if self.host.settings.get('flock','descriptions',True):
            out.update(description=record.get('description',''),described_by=record.get('described_by',''))
        return out

    def description_text(self, agent, text, *, by='owner'):
        """Проверка без записи: одна строка до 200 знаков и право писать. Возвращает очищенный текст."""
        valid_id(agent)
        text=' '.join(str(text or '').split())
        if len(text)>DESCRIPTION_LIMIT:raise HumanError('agent_description','Описание длиннее 200 знаков','Сократи до одной строки: что ты делаешь и в чём силён.')
        if by!='owner' and self.record(agent)['data'].get('described_by')=='owner':raise HumanError('agent_description_owner','Описание задал хозяин','Попроси его переписать описание в Стае; сам агент его не меняет.')
        return text

    def describe(self, agent, text, *, by='owner'):
        """Короткое описание агента. Пишет хозяин (by='owner') или сам агент (by='agent').
        Агент не переписывает описание, которое задал хозяин; пустой текст — очистить."""
        text=self.description_text(agent,text,by=by)
        record=self.record(agent)
        data=dict(record['data'])
        if text:data.update(description=text,described_by=by)
        else:data.pop('description',None);data.pop('described_by',None)
        self.host.store.x('UPDATE flock_agents SET data=? WHERE id=?',(dumps(data),agent))
        self.host.journal.write('agents','Описание агента '+record['name']+(': '+text if text else ' очищено'),actor=by if by=='owner' else 'agent:'+agent,data={'agent_id':agent})
        self.host.bus.publish({'type':'agents','agent_id':'*'})
        return self.record(agent)

    def create(self, name, *, agent_id='', awaken=True, profile='helper', temporary=False, start=True):
        if profile not in ('full','helper','observer','reader','custom'):raise HumanError('agent_profile','Неизвестный шаблон прав','Выбери шаблон в пульте.')
        name=str(name).strip()[:120]
        if not name:raise HumanError('agent_name','Укажи имя агента','')
        agent_id=valid_id(agent_id or 'a_'+new_id()[-14:].lower())
        if self.host.store.one("SELECT 1 FROM sqlite_master WHERE name=?",('agent_'+agent_id+'__meta',)):raise HumanError('agent_used','Этот id уже использовался','Выбери новый id; прежние данные оставлены для восстановления.')
        if self.host.store.one('SELECT 1 FROM flock_agents WHERE id=?',(agent_id,)):raise HumanError('agent_exists','Такой агент уже есть','Выбери другое имя/id.')
        self.host.store.x('INSERT INTO flock_agents(id,name,temporary,created,data) VALUES(?,?,?,?,?)',(agent_id,name,int(temporary),time.time(),dumps({'profile':profile})))
        try:
            ctx=self.get(agent_id)
            ctx.selfdir.set_identity({'name':name})
            if not awaken:ctx.selfdir.finish_awakening()
            self.host.journal.write('agents','Создан агент: '+name,actor='owner',data={'agent_id':agent_id,'profile':profile})
            self.host.bus.publish({'type':'agents','agent_id':'*'})
            if start and not temporary and getattr(self.host,'flock_started',False) and not self.host.safe:ctx.heart.start()
            return self.record(agent_id)
        except BaseException:
            self.host.store.x('UPDATE flock_agents SET archived=1 WHERE id=?',(agent_id,));raise

    async def update(self, agent, *, name=None, archived=None, description=None):
        if getattr(self.loaded.get(agent),'_importing',False):raise HumanError('import_busy','Агент сейчас переезжает','Дождись завершения переноса.')
        if self.record(agent)['temporary']:raise HumanError('agent_temporary','Временным агентом управляет задача','Отмени задачу или дождись её и нажми «Оставить агента».')
        record=self.record(agent)
        if agent=='main' and archived:raise HumanError('main_archive','Главного агента нельзя архивировать','Он нужен прежнему слоту. Можно выключить его фон.')
        if name is not None:
            name=str(name).strip()[:120]
            if not name:raise HumanError('agent_name','Имя не может быть пустым','')
            self.host.store.x('UPDATE flock_agents SET name=? WHERE id=?',(name,agent))
            ctx=self.get(agent,archived=True)
            ctx.selfdir.set_identity({'name':name})
        if description is not None:self.describe(agent,description,by='owner')
        if archived is not None:
            self.host.store.x('UPDATE flock_agents SET archived=? WHERE id=?',(int(archived),agent))
            if archived and agent in self.loaded:
                ctx=self.loaded.pop(agent);await ctx.close()
            elif not archived:self.get(agent).heart.start() if getattr(self.host,'flock_started',False) and not self.host.safe else None
        self.host.bus.publish({'type':'agents','agent_id':'*'})
        return self.record(agent)

    def create_role(self, parent, role_name, *, name='', awaken=True):
        role=parent.team.roles().get(role_name)
        if not role or not role.enabled:raise HumanError('agent_role','Такой шаблон команды недоступен','Выбери роль на экране задач.')
        from ..team.roles import required
        rights=dict(self.rights.effective(parent));caps=set(rights['capabilities'])&set(role.capabilities)
        tools=[n for n in role.tools if parent.tools.enabled(n) and set(required(n) or ()).issubset(caps)]
        rights.update(profile='custom',capabilities=sorted(caps),groups=['*'],tools=tools)
        record=self.create(name or role.title,awaken=awaken,profile='custom')
        ctx=self.get(record['id']);ctx.settings.set('agent_rights',rights)
        roles=parent.models.roles();roles['main']=list(roles.get(role.model) or roles.get('main') or [])
        ctx.settings.replace('roles',roles);ctx.selfdir.write('AGENTS.md',role.prompt)
        self.host.store.x('UPDATE flock_agents SET role=? WHERE id=?',(role.title,ctx.agent_id))
        return self.record(ctx.agent_id)

    def start(self):
        self.host.flock_started=True
        if not self.host.settings.get('flock','enabled',True):return
        for row in self.host.store.q('SELECT id FROM flock_agents WHERE archived=0 AND temporary=0'):
            ctx=self.get(row['id'])
            if ctx is not self.host:ctx.heart.start();ctx.mind.recover();ctx.team.recover()

    def start_userbots(self):
        """Аккаунты Telegram агентов, включённые хозяином; контекст агента не поднимается ради выключенного."""
        contexts=[self.host]
        if self.host.settings.get('flock','enabled',True):
            contexts+=[self.get(row['id']) for row in self.host.store.q('SELECT id FROM flock_agents WHERE archived=0 AND temporary=0') if row['id']!='main']
        for ctx in contexts:
            if ctx.settings.get('userbot','enabled',False):ctx.userbot.start()

    def private_path(self, ctx, path, capability='', *, ancestors=True):
        path=canonical(path)
        # Собственные навыки/память разрешены; остальные агенты и общие файлы состояния закрыты.
        # ancestors=False: папку-родителя можно посмотреть (имена), внутрь чужого — нельзя;
        # тулы обхода (find_files, grep) сами пропускают закрытые подпапки.
        for record in self.public(include_temporary=True):
            if record['id']==ctx.agent_id:continue
            task=getattr(ctx,'_task_agent',None)
            if task and capability=='fs.read' and record['id']==task.parent.agent_id and any(within(path,p) for p in task.read_dirs()):continue
            base=self.host.dirs.root if record['id']=='main' else self.host.dirs.root/'agents'/record['id']
            targets=[base/name for name in ('self','diary','skills','work','local','imports','checkpoints','workshop')] if record['id']=='main' else [base]
            if any(within(path,t) or ancestors and within(t,path) for t in targets):return True
        private=getattr(ctx.dirs,'agent_root',None)
        return bool(private and any(within(path,private/name) for name in ('local','imports','workshop','checkpoints')))

    def capability(self, ctx, capability):
        return self.rights.capability(ctx,capability)

    def tool_allowed(self, ctx, name):
        return self.rights.tool_allowed(ctx,name)

    def export(self, agent):
        import base64, io, zipfile
        from .settings import LOCAL
        ctx=self.get(agent,archived=True); record=self.record(agent)
        result={'format':'bender-agent-1','name':record['name'],'role':record['role'],'tables':{}}
        prefix=getattr(ctx.store.db,'prefix','')
        for table in self.host.store.q("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
            physical=table['name']
            if prefix:
                if not physical.startswith(prefix):continue
                name=physical[len(prefix):]
            else:
                if physical.startswith(('agent_','flock_')):continue
                name=physical
            if name.startswith(('mem_fts','mem_tri')) or name in ('journal',):continue
            rows=[]
            for row in ctx.store.q('SELECT * FROM "'+name+'"'):
                data=dict(row)
                if name=='settings' and (data['grp'] not in LOCAL|{'telegram'} or data['grp']=='userbot'):continue
                if name=='settings' and data['grp']=='telegram':data['value']=dumps({'live':ctx.settings.get('telegram','live')})
                if name=='meta' and str(data['key']).startswith(('telegram:','userbot:','updates:','models:','schema:')):continue
                rows.append({k: {'$bytes':base64.b64encode(v).decode()} if isinstance(v,bytes) else v for k,v in data.items()})
            result['tables'][name]=rows
        out=io.BytesIO()
        with zipfile.ZipFile(out,'w',zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('agent.json',dumps(result))
            for folder in ('self','diary','skills','work'):
                base=getattr(ctx.dirs,{'self':'self_dir'}.get(folder,folder))
                for path in sorted(base.rglob('*')):
                    if path.is_symlink():raise HumanError('agent_export','Ссылки в архив агента не входят','Убери ссылку или скопируй нужные файлы отдельно.')
                    if path.is_file():archive.write(path,folder+'/'+path.relative_to(base).as_posix())
        return out.getvalue()

    def import_archive(self, raw, *, name=''):
        import base64, io, zipfile
        from ..core.pathutil import relative_path
        from ..core.fsutil import atomic_write
        try:
            archive=zipfile.ZipFile(io.BytesIO(raw))
            if sum(x.file_size for x in archive.infolist()) > 200*1024*1024:raise ValueError('Архив больше 200 МБ')
            data=json.loads(archive.read('agent.json'))
            if data.get('format')!='bender-agent-1':raise ValueError('Неизвестный формат')
            paths=[]
            for item in archive.infolist():
                if item.filename=='agent.json':continue
                p=relative_path(item.filename)
                if p.is_absolute() or '..' in p.parts or '\\' in item.filename or ':' in item.filename or not p.parts or p.parts[0] not in ('self','diary','skills','work') or (item.external_attr>>16)&0o170000==0o120000:raise ValueError('Небезопасное имя файла')
                paths.append((item,p))
        except (ValueError,KeyError,zipfile.BadZipFile) as exc:
            raise HumanError('agent_import','Не удалось прочитать архив агента','Нужен экспорт Бендера без ссылок и выходов из папки.',detail=str(exc)) from None
        record=self.create(name or data.get('name') or 'Импортированный',awaken=False,profile='helper')
        ctx=self.get(record['id'])
        try:
            for item,p in paths:
                if item.is_dir():continue
                base=getattr(ctx.dirs,{'self':'self_dir'}.get(p.parts[0],p.parts[0]))
                target=base.joinpath(*p.parts[1:])
                if not within(target,base):raise ValueError('Путь вне папки')
                target.parent.mkdir(parents=True,exist_ok=True);atomic_write(target,archive.read(item))
            known={r['name'][len(ctx.store.db.prefix):] for r in self.host.store.q("SELECT name FROM sqlite_master WHERE type='table'") if r['name'].startswith(ctx.store.db.prefix)}
            for table,rows in data.get('tables',{}).items():
                if table not in known or table.startswith('mem_') or table in ('journal','settings'):continue
                columns={r['name'] for r in ctx.store.q('PRAGMA table_info("'+table+'")')}
                for row in rows:
                    value={k:(base64.b64decode(v['$bytes']) if isinstance(v,dict) and set(v)=={'$bytes'} else v) for k,v in row.items() if k in columns}
                    if 'agent_id' in columns:value['agent_id']=ctx.agent_id
                    if table=='jobs' and 'enabled' in value:value['enabled']=0
                    if table=='sessions' and 'meta' in value:
                        value['meta']=dumps({**json.loads(value['meta']), 'home_channel':'pult'})
                    if value:
                        keys=list(value);ctx.store.x('INSERT OR REPLACE INTO "'+table+'"('+','.join('"'+k+'"' for k in keys)+') VALUES('+','.join('?' for _ in keys)+')',tuple(value.values()))
            # Неодобренный архив не включает скрипты, каналы, старые grants и процессы.
            for row in data.get('tables',{}).get('settings',[]):
                group=row.get('grp')
                if group not in ('agent','memory','appearance','voice','heart','sleep','roles','team_roles'):continue
                changes=json.loads(row.get('value') or '{}')
                if group in ('voice','heart','sleep'):changes['enabled']=False
                from ..core.settings import SCHEMA
                schema=SCHEMA[group]
                known={f.key for f in schema.fields}
                if schema.fields:
                    ctx.settings.set(group,{k:v for k,v in changes.items() if k in known})
                    ctx.settings._write(group,{**ctx.settings._stored(group),**{k:v for k,v in changes.items() if k not in known}})
                elif group=='roles':
                    candidates={role:[e for e in chain if e.get('provider') in ctx.models.providers()] for role,chain in changes.items() if isinstance(chain,list)}
                    ctx.settings.set(group,candidates)
                else:ctx.settings.replace(group,changes)
            ctx.selfdir.set_identity({'name':name or record['name']})
            ctx.index.rebuild()
            saved=ctx.dirs.imports/'original-agent.zip';saved.parent.mkdir(parents=True,exist_ok=True);atomic_write(saved,raw)
            return self.record(ctx.agent_id)
        except BaseException:
            self.host.store.x('UPDATE flock_agents SET archived=1 WHERE id=?',(ctx.agent_id,));raise
        finally:archive.close()

    async def delete(self, agent, *, confirmed=False):
        if getattr(self.loaded.get(agent),'_importing',False):raise HumanError('import_busy','Агент сейчас переезжает','Дождись завершения переноса.')
        if self.record(agent)['temporary']:raise HumanError('agent_temporary','Временный агент связан с задачей','Управляй им на экране задачи.')
        if agent=='main':raise HumanError('main_delete','Главного агента нельзя удалить','Он нужен прежнему слоту и общему ядру.')
        if not confirmed:raise HumanError('agent_confirm','Подтверди удаление агента','Сначала сохраним архив для восстановления.')
        from ..core.fsutil import atomic_write
        import shutil
        raw=self.export(agent)
        path=self.host.dirs.local/'agent-backups'/(agent+'-'+new_id()+'.zip');path.parent.mkdir(parents=True,exist_ok=True);atomic_write(path,raw)
        await self.update(agent,archived=True)
        # Данные таблиц остаются как архив; старый слот их не видит. Файлы восстанавливаются из ZIP.
        private=self.host.dirs.root/'agents'/valid_id(agent)
        if not within(canonical(private),canonical(self.host.dirs.root/'agents')):raise HumanError('agent_path','Путь агента изменился','Архив сохранён, файлы не удалены.')
        if private.exists():shutil.rmtree(private)
        for part in ('session','api'):self.host.secrets.delete('userbot:'+agent+':'+part)
        self.host.store.x('DELETE FROM flock_agents WHERE id=?',(agent,))
        self.host.journal.write('agents','Агент удалён с архивом',actor='owner',data={'agent_id':agent,'backup':str(path)})
        return {'backup':str(path)}
