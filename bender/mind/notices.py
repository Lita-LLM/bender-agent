"""Служебная строка хозяину: журнал разговора и его последний канал."""
from ..guard.policy import Action


async def home(agent_ctx, text, kind):
    from .loop import home_channel
    from .passport import clean
    sid = agent_ctx.main_session()
    text = clean(agent_ctx, text)
    decision = await agent_ctx.guard.check(Action('msg.send', 'owner', actor='system', to_owner=True, summary=kind), session_id=sid)
    if not decision.allowed: return
    msg = {'role': 'event', 'content': text, 'kind': kind}
    msg['id'] = agent_ctx.store.add_message(sid, msg, {'actor': 'system'})
    agent_ctx.bus.publish({'type': 'message', 'session': sid, 'message': msg})
    agent_ctx.bus.publish({'type': 'notify', 'kind': kind, 'channel': home_channel(agent_ctx, sid), 'text': text})
