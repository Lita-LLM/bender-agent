from bender.core.errors import HumanError
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv


class HandOverTest(TempDirCase):
    @async_test
    async def test_match_agent_by_declined_name(self):
        from bender.tools.channel import match_agent
        async with MindEnv(self.tmp) as env:
            host = env.app
            host.agents.create('Шерлок', agent_id='sherlock', awaken=False)
            self.assertEqual(match_agent(host, 'Шерлока'), 'sherlock')  # на слух имя склоняют
            self.assertEqual(match_agent(host, 'sherlock'), 'sherlock')
            self.assertEqual(match_agent(host, 'main'), 'main')
            with self.assertRaises(HumanError) as caught:
                match_agent(host, 'Ватсон')
            self.assertIn('Шерлок (sherlock)', caught.exception.hint)

    @async_test
    async def test_telegram_chat_goes_to_other_agent_with_note(self):
        async with MindEnv(self.tmp) as env:
            host = env.app
            host.agents.create('Шерлок', agent_id='sherlock', awaken=False)
            result = await host.tools.run('hand_over', {'agent': 'Шерлок', 'note': 'хозяин ищет ключи'},
                                          ToolContext(host, session_id=env.sid, turn={'_telegram_route': {'chat': 42}}))
            self.assertTrue(result.ok, result.content)
            self.assertEqual(host.store.meta_get('telegram:selection')['42']['agent'], 'sherlock')
            sherlock = host.agents.get('sherlock')
            env.script([{'text': 'Ищу.'}])
            await sherlock.mind.say(sherlock.main_session(), 'где они?', channel='telegram')
            first = [m for m in sherlock.store.messages(sherlock.main_session()) if m['role'] == 'user'][0]['content']
            self.assertTrue(first.startswith('[Разговор передал тебе '), first)
            self.assertIn('хозяин ищет ключи] где они?', first)
            # записка одноразовая
            env.script([{'text': 'Да.'}])
            await sherlock.mind.say(sherlock.main_session(), 'ещё', channel='telegram')
            last = [m for m in sherlock.store.messages(sherlock.main_session()) if m['role'] == 'user'][-1]['content']
            self.assertEqual(last, 'ещё')

    @async_test
    async def test_refused_outside_owner_channels(self):
        async with MindEnv(self.tmp) as env:
            host = env.app
            host.agents.create('Шерлок', agent_id='sherlock', awaken=False)
            pult = await host.tools.run('hand_over', {'agent': 'Шерлок'}, ToolContext(host, session_id=env.sid))
            self.assertFalse(pult.ok)
            self.assertIn('пульте хозяин выбирает агента сам', pult.content)
            night = await host.tools.run('hand_over', {'agent': 'Шерлок'}, ToolContext(host, session_id=env.sid, unattended=True, turn={'_telegram_route': {'chat': 42}}))
            self.assertFalse(night.ok)
            self.assertIsNone((host.store.meta_get('telegram:selection') or {}).get('42'))


class StationHandoverTest(TempDirCase):
    def test_station_switches_on_next_phrase(self):
        from bender.speakers.channel import Speakers, Station

        class Agents:
            def get(self, agent_id):
                return type('Ctx', (), {'agent_id': agent_id, 'main_session': lambda self: agent_id + '-main'})()

        owner = Speakers.__new__(Speakers)
        owner.app = type('App', (), {'agents': Agents()})()
        owner.settings = lambda: {'session': 'main'}
        station = Station(owner, {'id': 'kitchen', 'name': 'Кухня', 'host': 'h', 'port': 1, 'agent_id': 'main'})
        station.handover = 'sherlock'
        self.assertEqual(station.agent_id, 'main')  # текущий ход ещё ждёт прежнего агента
        self.assertEqual(station.public()['answering'], 'sherlock')
        ctx, sid = owner.selected(station)
        self.assertEqual((ctx.agent_id, sid, station.agent_id), ('sherlock', 'sherlock-main', 'sherlock'))
        station.handover = 'main'
        owner.selected(station)
        self.assertIsNone(station.agent)  # вернули агента из настроек

    def test_choice_survives_temporary_outage_and_main_is_the_last_resort(self):
        from bender.speakers.channel import Speakers, Station

        class Agents:
            down = {}

            def get(self, agent_id):
                if agent_id in self.down:
                    raise HumanError(self.down[agent_id], 'Агент недоступен', '')
                return type('Ctx', (), {'agent_id': agent_id, 'main_session': lambda self: agent_id + '-main'})()

        agents = Agents()
        owner = Speakers.__new__(Speakers)
        owner.app = type('App', (), {'agents': agents})()
        owner.settings = lambda: {'session': 'main', 'agent_id': 'main'}
        owner.answering = lambda: {}
        owner.remember = lambda station: None
        station = Station(owner, {'id': 'kitchen', 'name': 'Кухня', 'host': 'h', 'port': 1, 'agent_id': 'watson'})
        station.agent = 'sherlock'
        agents.down = {'sherlock': 'flock_off'}  # стая выключена на время: выбор хозяина не стираем
        ctx, _ = owner.selected(station)
        self.assertEqual((ctx.agent_id, station.agent, station.fallback), ('watson', 'sherlock', 'watson'))  # сперва агент колонки, не главный
        agents.down = {'sherlock': 'flock_off', 'watson': 'flock_off'}
        ctx, _ = owner.selected(station)
        self.assertEqual((ctx.agent_id, station.agent), ('main', 'sherlock'))  # главный — когда выбрать некого
        agents.down = {}
        ctx, _ = owner.selected(station)
        self.assertEqual((ctx.agent_id, station.fallback), ('sherlock', None))  # вернулся — снова отвечает выбранный
        agents.down = {'sherlock': 'agent_archived'}  # в архив насовсем: выбор забывается
        ctx, _ = owner.selected(station)
        self.assertEqual((ctx.agent_id, station.agent), ('watson', None))


class StationPreviewTest(TempDirCase):
    """Превью колонки: «стоп» посреди ответа и свет кольца."""

    def make(self, **settings):
        import asyncio
        from bender.speakers.channel import Speakers, Station
        stopped, sent = [], []

        class Mind:
            def stop(self, sid):
                stopped.append(sid)

        class Agents:
            def get(self, agent_id):
                return type('Ctx', (), {'agent_id': agent_id, 'mind': Mind()})()

        owner = Speakers.__new__(Speakers)
        owner.app = type('App', (), {'agents': Agents()})()
        owner.settings = lambda: {'session': 'main', 'agent_id': 'main', 'light': True, 'stop_cancels': True, **settings}
        owner.answering = lambda: {}
        station = Station(owner, {'id': 'kitchen', 'name': 'Кухня', 'host': 'h', 'port': 1, 'agent_id': 'main'})

        class Link:
            async def send(self, kind, payload=b''):
                sent.append((kind, payload))
        station.link = Link()
        return station, stopped, sent

    @async_test
    async def test_stop_word_cancels_talk_and_agent_turn(self):
        import asyncio
        station, stopped, _ = self.make()
        station.sid = 'main-main'
        station.talk = asyncio.ensure_future(asyncio.sleep(30))
        await station.on_frame(90, {})  # VA_REQUEST без флага начала: колонка закончила слушать/стоп
        await asyncio.sleep(0)
        self.assertTrue(station.talk.cancelled())
        self.assertEqual(stopped, ['main-main'])

    @async_test
    async def test_light_follows_state_only_when_enabled_and_known(self):
        from bender.speakers import esphome as api
        station, _, sent = self.make()
        await station.glow('think')
        self.assertEqual(sent, [])  # ключа света нет — тишина
        await station.on_frame(api.LIST_LIGHT, {2: [77]})
        await station.glow('think')
        await station.glow('off')
        self.assertEqual([k for k, _ in sent], [api.LIGHT_COMMAND] * 2)
        on = api.decode(sent[0][1][5:])  # после fixed32-ключа
        self.assertEqual(round(on[9][0] and 1, 1), 1)
        station.owner.settings = lambda: {'light': False}
        await station.glow('speak')
        self.assertEqual(len(sent), 2)
