"""Запуск одобренного Matrix MCP как фиксированного транспорта канала, без импорта пакета."""
import asyncio
import os
import shlex
import sys
from pathlib import Path
from urllib.parse import urlsplit

from ..core.errors import HumanError
from ..guard import sandbox
from ..mcp.client import Client,Stdio,RpcError
from ..mcp.manager import digest
from ..tools.registry import ToolContext

METHODS={'status','join','sync','send','react','download','upload'}
SECRET='matrix:token'
LIMIT=32*1024*1024


def failed(hint):return HumanError('matrix','Matrix недоступен',hint)


def homeserver(value):
    if not isinstance(value,str) or not value or any(ord(c)<33 for c in value) or '\\' in value:raise failed('Укажи HTTPS-адрес домашнего сервера Matrix.')
    try:
        parts=urlsplit(value);_=parts.port
        if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or (parts.scheme!='https' and not (parts.scheme=='http' and parts.hostname in ('localhost','127.0.0.1','::1'))):raise ValueError()
    except ValueError:raise failed('Нужен HTTPS без логина/пароля, query/fragment; HTTP допустим только на localhost.') from None
    return value.rstrip('/')


class Transport:
    def __init__(self,app):self.app=app.host;self.clients=set()

    def setup(self):
        app=self.app;cfg=app.settings.group('matrix')
        if app.safe:raise failed('В безопасном режиме Matrix выключен.')
        homeserver(cfg.get('homeserver'))
        room=cfg.get('room_id','')
        if not isinstance(room,str) or not room.startswith('!') or len(room)>4096 or any(ord(c)<33 for c in room):raise failed('Укажи полный id комнаты: !…:сервер.')
        package=app.modules.package('matrix');trust=app.modules.trust('matrix');sid=trust.get('server')
        if not app.modules.active(package) or package['metadata'].get('mcp',{}).get('entry')!='server.py' or package['metadata'].get('workshop'):raise failed('Установи/просмотри/включи транспортный модуль Matrix в Модулях.')
        metadata=package['metadata']
        if not {'net.read','net.send','msg.send'}.issubset(metadata.get('capabilities',[])) or not any(row.get('name')=='matrix' and row.get('adapter')=='matrix-v1' for row in metadata.get('channels',[])):raise failed('Пакет не объявил интерфейс и сетевые способности транспорта Matrix.')
        server=app.mcp.get(sid)
        if not app.mcp.active(server):raise failed('Подключение модуля Matrix выключено; проверь MCP/Модули.')
        definitions={name:app.mcp.definition(sid,name) for name in METHODS}
        if any(value is None for value in definitions.values()):raise failed('Обнаружь и одобри определения Matrix в Модулях.')
        if not app.secrets.get(SECRET):raise failed('Сохрани access_token Matrix на экране Связи.')
        app.agents.get(cfg.get('agent_id','main'))
        return cfg,server,trust,definitions

    def stamp(self):
        cfg,server,trust,definitions=self.setup()
        return digest({'settings':cfg,'server':self.app.mcp.signature(server),'module':trust,'definitions':definitions,
                       'token':(self.app.secrets.get(SECRET),self.app.secrets.allow(SECRET))})

    def validate(self,expected,*,enabled=True):
        if self.stamp()!=expected or (enabled and not self.app.settings.get('matrix','enabled',False)):raise failed('Настройки, ключ или одобрение Matrix изменились; действие остановлено.')

    async def call(self,name,args=None,*,enabled=True):
        # Этот метод не является тулом/API. Вызывающие методы канала уже проверили
        # net.read/net.send/msg.send/fs для конкретного хозяина/комнаты/действия.
        if name not in METHODS:raise failed('Неизвестная операция транспорта.')
        cfg,server,trust,definitions=self.setup();stamp=self.stamp();self.validate(stamp,enabled=enabled)
        ctx=ToolContext(self.app,actor='owner',session_id='matrix-transport',tool='matrix_transport')
        info=sandbox.for_app(self.app);info['write_work']=False;info['agent_isolation']=False
        await ctx.check('mcp.connect','matrix/'+stamp,'запустить одобренный транспорт Matrix',detail={'sandbox':info['title'],'operation':name})
        self.validate(stamp,enabled=enabled)
        snapshot=Path(trust['snapshot']);command=[sys.executable,str(snapshot/'server.py')]
        prepared=sandbox.prepare(shlex.join(command),info=info,work_dir=self.app.dirs.work,allow_net=True,read_dirs=(snapshot,),temp_root=self.app.dirs.local,raw_argv=command)
        allowed={'PATH','HOME','USER','LANG','LC_ALL','SYSTEMROOT','SystemRoot','PATHEXT','COMSPEC','HTTP_PROXY','HTTPS_PROXY','ALL_PROXY','NO_PROXY','http_proxy','https_proxy','all_proxy','no_proxy','SSL_CERT_FILE','SSL_CERT_DIR'}
        env={key:value for key,value in os.environ.items() if key in allowed};env.update(prepared.env);env['PYTHONDONTWRITEBYTECODE']='1'
        transport=Stdio(prepared.argv or command,cwd=self.app.dirs.work,env=env,cleanup=prepared.close,resource=prepared,limit=LIMIT)
        client=Client(transport,timeout=90);self.clients.add(client)
        try:
            await client.connect()
            found={item.get('name'):item for item in await client.tools()}
            self.validate(stamp,enabled=enabled)
            if found.get(name)!=definitions[name]:raise failed('Определение операции Matrix изменилось; просмотри модуль заново.')
            token,_=self.app.guard.expand_secrets('{{secret:'+SECRET+'}}',host=urlsplit(cfg['homeserver']).hostname,tool='matrix_transport')
            owner=self.app.store.meta_get('matrix:owner',{})
            await client.request('channel/configure',{'homeserver':cfg['homeserver'],'token':token,'room':cfg['room_id'],'owner':owner.get('user_id') if owner.get('binding')==self.app.matrix.identity() else None})
            self.validate(stamp,enabled=enabled)
            result=await client.request('tools/call',{'name':name,'arguments':args or {}})
            self.validate(stamp,enabled=enabled)
            value=result.get('structuredContent')
            if not isinstance(value,dict):raise failed('Транспорт Matrix вернул неверный результат.')
            return value
        except RpcError as error:raise failed(self.app.guard.scrub(error.detail or error.hint)) from None
        finally:
            # Остановка может попасть уже в закрытие stdin/процесса. Сначала
            # завершаем уборку, затем передаём отмену вызывающему каналу.
            closing=asyncio.create_task(client.close());cancelled=False
            try:
                while True:
                    try:await asyncio.shield(closing);break
                    except asyncio.CancelledError:
                        if closing.done():raise
                        cancelled=True
            finally:self.clients.discard(client)
            if cancelled:raise asyncio.CancelledError

    async def close(self):
        if self.clients:await asyncio.gather(*(client.close() for client in list(self.clients)),return_exceptions=True)
