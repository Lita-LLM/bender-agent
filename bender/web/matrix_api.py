"""Matrix: настройка и привязка только через защищённый owner API."""
from ..tools.registry import ToolContext


def routes(router,app,J,body):
    host=app.host

    @router.route('GET','/api/channels/matrix')
    @J
    async def read(req):
        from ..modules.matrix_import import proposals
        return {**host.matrix.public(),'import_proposals':proposals(app)}

    @router.route('POST','/api/modules/bundled/matrix')
    @J
    async def bundled(req):return host.modules.import_bundled('matrix')

    @router.route('POST','/api/channels/matrix/configure')
    @J
    async def configure(req):
        data=body(req)
        return await host.matrix.configure({**data,'agent_id':data.get('agent_id',app.agent_id)},data.get('token'))

    @router.route('POST','/api/channels/matrix/connect')
    @J
    async def connect(req):return await host.matrix.connect()

    @router.route('POST','/api/channels/matrix/pair')
    @J
    async def pair(req):return host.matrix.pair()

    @router.route('POST','/api/channels/matrix/check')
    @J
    async def check(req):
        matrix=host.matrix;stamp=matrix.transport.stamp()
        await ToolContext(host,actor='owner',tool='matrix_check').check('net.read',matrix.settings()['homeserver'],'проверить доступ и комнату Matrix')
        matrix.transport.validate(stamp,enabled=False)
        result=await matrix.transport.call('status',enabled=False);matrix.require_room(result,paired=bool(matrix.owner()))
        return {'status':result,'channel':matrix.public()}

    @router.route('POST','/api/channels/matrix/stop')
    @J
    async def stop(req):
        host.settings.set('matrix',{'enabled':False},by='owner');await host.matrix.stop();return host.matrix.public()

    @router.route('POST','/api/channels/matrix/unpair')
    @J
    async def unpair(req):
        host.settings.set('matrix',{'enabled':False},by='owner');await host.matrix.stop()
        host.store.meta_set('matrix:owner',{});host.matrix.code=None
        return host.matrix.public()
