"""Общий транспорт событий/роутер, закреплённые за контекстом агента."""
from contextvars import ContextVar

ACTIVE = ContextVar('bender_agent_context', default=None)


class Router:
    def __init__(self, router, ctx): self.shared, self.ctx = router, ctx
    def __getattr__(self, name):
        value = getattr(self.shared, name)
        if not callable(value): return value
        def call(*args, **kwargs):
            token = ACTIVE.set(self.ctx)
            try: return value(*args, **kwargs)
            finally: ACTIVE.reset(token)
        return call
    async def call(self, *args, **kwargs):
        token = ACTIVE.set(self.ctx)
        try: return await self.shared.call(*args, **kwargs)
        finally: ACTIVE.reset(token)
    async def embed(self, *args, **kwargs):
        token = ACTIVE.set(self.ctx)
        try: return await self.shared.embed(*args, **kwargs)
        finally: ACTIVE.reset(token)


class Bus:
    def __init__(self, shared, agent_id): self.shared, self.agent_id, self.listeners = shared, agent_id, []
    def publish(self, event): self.shared.publish({'agent_id': self.agent_id, **event})
    publish_threadsafe = publish
    def listen(self, callback):
        def on(event):
            if event.get('agent_id', 'main') in (self.agent_id, '*'): callback(event)
        self.listeners.append(on); self.shared.listen(on)
    def __getattr__(self, name): return getattr(self.shared, name)
    def dispose(self):
        for fn in self.listeners:
            if fn in self.shared._listeners: self.shared._listeners.remove(fn)
        self.listeners.clear()


class Journal:
    def __init__(self, shared, agent_id): self.shared, self.agent_id = shared, agent_id
    def write(self, kind, summary, *, actor='bender', data=None):
        return self.shared.write(kind, summary, actor=actor, data={**(data or {}), 'agent_id':self.agent_id})
    def recent(self, limit=200, **kwargs):
        return self.shared.recent(limit=limit, agent_id=self.agent_id, **kwargs)
    def __getattr__(self, name): return getattr(self.shared, name)
