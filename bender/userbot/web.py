"""Аккаунт Telegram агента: установка Telethon, вход, привязка хозяина и список чатов — только через API пульта (хозяин)."""
from ..tools.registry import ToolContext


def routes(router, app, J, body):
    host = app.host

    async def owner(what):
        await ToolContext(host, actor='owner', tool='userbot_setup').check('settings', 'userbot', what)

    @router.route('GET', '/api/channels/userbot')
    @J
    async def read(req):
        return app.userbot.public()

    @router.route('POST', '/api/channels/userbot/install')
    @J
    async def install(req):
        await host.userbot_runtime.install()
        return app.userbot.public()

    @router.route('POST', '/api/channels/userbot/remove')
    @J
    async def remove(req):
        await owner('удалить Telethon и остановить аккаунты Telegram')
        await host.userbot_runtime.remove()
        return app.userbot.public()

    @router.route('POST', '/api/channels/userbot/api')
    @J
    async def api(req):
        data = body(req)
        await owner('сохранить ключи приложения Telegram')
        return app.userbot.save_api(data.get('api_id'), data.get('api_hash'))

    @router.route('POST', '/api/channels/userbot/login')
    @J
    async def login(req):
        data = body(req)
        if not data.get('risk'):
            from .runtime import failed
            raise failed('Подтверди, что знаешь о риске: Telegram может ограничить или заблокировать аккаунт за автоматизацию.', 'Нужно подтверждение')
        return await app.userbot.login_start(data.get('phone'))

    @router.route('POST', '/api/channels/userbot/code')
    @J
    async def code(req):
        data = body(req)
        return await app.userbot.login_code(code=data.get('code'), password=data.get('password'))

    @router.route('POST', '/api/channels/userbot/cancel')
    @J
    async def cancel(req):
        await app.userbot.stop()
        return app.userbot.public()

    @router.route('POST', '/api/channels/userbot/logout')
    @J
    async def logout(req):
        await owner('выйти из аккаунта Telegram агента')
        return await app.userbot.logout()

    @router.route('POST', '/api/channels/userbot/pair')
    @J
    async def pair(req):
        return app.userbot.pair()

    @router.route('POST', '/api/channels/userbot/unpair')
    @J
    async def unpair(req):
        return app.userbot.unpair()

    @router.route('GET', '/api/channels/userbot/chats')
    @J
    async def chats(req):
        bot = app.userbot
        data = await bot.request(ToolContext(bot.ctx, actor='owner', tool='userbot_setup'), 'net.read', 'userbot:account', 'показать хозяину чаты аккаунта', 'dialogs',
                                 {'query': req.q('q', '') or '', 'limit': 60})
        return {'chats': [{**c, **{k: v for k, v in bot.policy(c['id']).items() if k in ('see', 'wake', 'reply')}} for c in data.get('chats') or []]}

    @router.route('POST', '/api/channels/userbot/chat')
    @J
    async def chat(req):
        data = body(req)
        await owner('изменить, кого видит и кому отвечает аккаунт Telegram')
        return app.userbot.set_chat(data.get('id'), {k: data[k] for k in ('see', 'wake', 'reply', 'title', 'remove') if k in data})

    @router.route('POST', '/api/channels/userbot/settings')
    @J
    async def settings(req):
        data = body(req)
        await owner('изменить настройки аккаунта Telegram агента')
        app.settings.set('userbot', {k: data[k] for k in ('trust', 'service', 'enabled') if k in data}, by='owner')
        return app.userbot.public()
