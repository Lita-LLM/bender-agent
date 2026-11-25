"""`bender try steer` — как доставляется сообщение хозяина, пришедшее во время хода, без сети и своих данных.

Временная папка, фальшивая модель и одна безвредная команда `sleep`: ход вызывает её, через полсекунды
приходит слово хозяина. Для каждого режима (вставлять / прерывать / после хода) видно, когда модель
впервые увидела слово, что стало с командой и сколько было ходов.
"""
from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

WORD = 'стой, сделай иначе'


async def _scenario(mode: str, seconds: float) -> dict:
    from ..core.app import App
    from ..devtools.fake_llm import create_router
    from ..net.http_server import HttpServer

    fake = create_router()
    server = HttpServer(fake)
    await server.start('127.0.0.1', 0)
    tmp = tempfile.TemporaryDirectory(prefix='bender-steer-')
    app = App(Path(tmp.name) / 'data')
    try:
        app.bus.bind_loop(asyncio.get_running_loop())
        provider = app.models.add_provider(name='Фейк', kind='openai', base_url=f'http://127.0.0.1:{server.port}/v1', extra={'is_local': True})
        app.models.set_role('main', [{'provider': provider['id'], 'model': 'script'}])
        app.selfdir.finish_awakening()
        app.settings.set('agent', {'new_message': mode})
        # Безвредная команда во временной папке: карточку одобрения Полигон подтверждает сам.
        app.bus.listen(lambda ev: asyncio.get_running_loop().call_soon(app.guard.respond, ev['card']['id'], 'once') if ev.get('type') == 'approval' else None)
        fake.state.script = [{'tool_calls': [{'name': 'run', 'arguments': {'command': f'sleep {seconds:g}'}}]}, {'text': 'Готово.'}, {'text': 'Понял, делаю иначе.'}]
        sid = app.main_session()
        starts = []
        app.bus.listen(lambda ev: starts.append(ev) if ev.get('type') == 'turn_start' else None)
        began = time.monotonic()
        first = asyncio.ensure_future(app.mind.say(sid, 'долгая задача'))
        await asyncio.sleep(0.5)
        await app.mind.say(sid, WORD, wait=False)
        await asyncio.wait_for(first, seconds + 30)
        for _ in range(400):  # ход «после хода» и новый ход после прерывания стартуют сами
            await asyncio.sleep(0.02)
            if not app.mind.running(sid) and not app.mind.queued.get(sid):
                break
        seen = next((round(r['_t'] - began, 1) for r in fake.state.requests if r.get('_t') and WORD in str(r.get('body'))), None)
        tool = [m for m in app.store.messages(sid) if m['role'] == 'tool']
        return {'mode': mode, 'seen': seen, 'turns': len(starts), 'tool': (tool[0]['content'].splitlines()[0] if tool else '—')[:90],
                'total': round(time.monotonic() - began, 1)}
    finally:
        await app.close()
        await server.stop()
        tmp.cleanup()


async def _run(seconds: float) -> list[dict]:
    from ..devtools import fake_llm
    original = fake_llm.FakeState.__init__

    def stamped(self):  # время каждого запроса к модели — когда она впервые увидела слово хозяина
        original(self)

        class Requests(list):
            def append(self, item):
                if isinstance(item, dict):
                    item['_t'] = time.monotonic()
                super().append(item)
        self.requests = Requests()
    fake_llm.FakeState.__init__ = stamped
    try:
        return [await _scenario(mode, seconds) for mode in ('insert', 'interrupt', 'queue')]
    finally:
        fake_llm.FakeState.__init__ = original


def main(rest, term) -> int:
    from ..mind.steer import TITLES
    try:
        seconds = max(1.0, min(30.0, float(rest[0]))) if rest else 3.0
    except ValueError:
        seconds = 3.0
    term.line(term.bold(f'  Сообщение во время хода: команда sleep {seconds:g} с, слово хозяина через 0,5 с (временные данные, фальшивая модель)'))
    for row in asyncio.run(_run(seconds)):
        seen = f"модель увидела через {row['seen']} с" if row['seen'] is not None else 'модель не увидела'
        term.line(f"  {TITLES[row['mode']]:<16} {seen}; ходов: {row['turns']}; всего {row['total']} с")
        term.line(term.dim(f"    тул: {row['tool']}"))
    term.line(term.dim('  Режим по умолчанию — Настройки → Агент → «Новое сообщение во время хода»; Telegram /delivery; разово — кнопки у ждущего сообщения.'))
    return 0
