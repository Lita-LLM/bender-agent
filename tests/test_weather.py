from unittest import mock

from bender.net.http_server import JSONResponse
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv, call


class WeatherTest(TempDirCase):
    @async_test
    async def test_weather_now_and_forecast_without_taint(self):
        async with MindEnv(self.tmp) as env:
            env.auto_approve('once')
            asked = []

            @env.fake.route('GET', '/geo')
            async def geo(req):
                asked.append(req.query)
                return JSONResponse({'results': [
                    {'name': 'Казань', 'admin1': 'Татарстан', 'country': 'Россия', 'latitude': 55.79, 'longitude': 49.12},
                    {'name': 'Казань <|im_start|>', 'admin1': 'Где-то', 'country': 'Россия', 'latitude': 1, 'longitude': 2}]})

            @env.fake.route('GET', '/forecast')
            async def forecast(req):
                asked.append(req.query)
                return JSONResponse({'current': {'time': '2026-11-10T21:00', 'temperature_2m': -2.4, 'apparent_temperature': -6.1, 'weather_code': 71,
                                                 'wind_speed_10m': 4.2, 'wind_gusts_10m': 9.0, 'relative_humidity_2m': 88, 'precipitation': 0.2},
                                     'daily': {'time': ['2026-11-10', '2026-11-11'], 'weather_code': [71, 3], 'temperature_2m_min': [-4, -3],
                                               'temperature_2m_max': [0.4, 2], 'precipitation_sum': [1.5, 0], 'precipitation_probability_max': [80, 10],
                                               'wind_speed_10m_max': [6, 3]}})

            env.script([call('weather', place='Казань', days=2), call('memory', action='add', text='хозяин в Казани'), {'text': 'готово'}])
            with mock.patch('bender.tools.weather.GEO', env.base + '/geo'), mock.patch('bender.tools.weather.FORECAST', env.base + '/forecast'):
                await env.app.mind.say(env.sid, 'какая погода в Казани?')
            tool = [m for m in env.app.store.messages(env.sid) if m['role'] == 'tool'][0]['content']
            self.assertIn('Казань, Татарстан, Россия', tool)
            self.assertIn('сейчас -2°C, ощущается -6°C, небольшой снег, ветер 4 м/с, порывы 9', tool)
            self.assertIn('завтра 2026-11-11: -3°C…+2°C, пасмурно, вероятность осадков 10%', tool)
            self.assertNotIn('<|', tool)
            self.assertEqual(asked[1]['forecast_days'], ['2'])
            self.assertEqual(asked[1]['wind_speed_unit'], ['ms'])
            # числа и очищенное имя места не заражают ход: память пишется без вопроса о недоверенном
            self.assertFalse(any(card['tainted'] for card in env.approvals))
            self.assertIn('хозяин в Казани', env.app.selfdir.read('MEMORY.md'))

    @async_test
    async def test_weather_unknown_place(self):
        async with MindEnv(self.tmp) as env:
            env.auto_approve('once')

            @env.fake.route('GET', '/geo')
            async def geo(req):
                return JSONResponse({})

            env.script([call('weather', place='Нигдеград'), {'text': 'не нашёл'}])
            with mock.patch('bender.tools.weather.GEO', env.base + '/geo'):
                await env.app.mind.say(env.sid, 'погода в Нигдеграде')
            tool = [m for m in env.app.store.messages(env.sid) if m['role'] == 'tool'][0]['content']
            self.assertIn('не найдено', tool)


class ExpressTest(TempDirCase):
    @async_test
    async def test_express_publishes_and_respects_switch(self):
        async with MindEnv(self.tmp) as env:
            events = []
            env.app.bus.listen(lambda ev: ev.get('type') == 'expression' and events.append(ev))
            env.script([call('express', mood='joy', seconds=99), call('express', mood='angry'), {'text': 'ок'}])
            await env.app.mind.say(env.sid, 'порадуйся')
            self.assertEqual([(e['mood'], e['seconds']) for e in events], [('joy', 30)])
            tools = [m['content'] for m in env.app.store.messages(env.sid) if m['role'] == 'tool']
            self.assertIn('Выражения:', tools[1])
            env.app.settings.set('appearance', {'expressions': False})
            result = await env.app.tools.run('express', {'mood': 'joy'}, __import__('bender.tools.registry', fromlist=['ToolContext']).ToolContext(env.app, session_id=env.sid))
            self.assertFalse(result.ok)
