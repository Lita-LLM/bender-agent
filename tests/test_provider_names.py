"""Имена своих провайдеров: по адресу, а не одно на всех «Свой провайдер»."""
import asyncio
from pathlib import Path

from bender.core.app import App
from bender.core.secrets import Secrets
from bender.core.settings import Settings
from bender.core.store import Store
from bender.net.http_server import HttpServer
from bender.router.config import ModelsConfig, auto_name
from tests.helpers import TempDirCase, async_test, owner_web
from tests.test_agent_cards import _request


class AutoNameTest(TempDirCase):
    def test_known_addresses_get_the_preset_name_and_others_their_own_address(self):
        self.assertEqual(auto_name('https://openrouter.ai/api/v1'), 'OpenRouter')
        self.assertEqual(auto_name('http://127.0.0.1:20128/v1'), 'OmniRoute')  # по порту: на одном адресе живут разные программы
        self.assertEqual(auto_name('http://127.0.0.1:11434/v1'), 'Ollama')
        self.assertEqual(auto_name('http://127.0.0.1:9000/v1'), 'Локальный 127.0.0.1:9000')
        self.assertEqual(auto_name('http://localhost/v1'), 'Локальный localhost')
        self.assertEqual(auto_name('https://llm.example.org/v1'), 'llm.example.org')
        self.assertEqual(auto_name('https://api.example.org:8443/v1'), 'example.org:8443')
        self.assertEqual(auto_name('http://192.168.1.5:8000/v1'), '192.168.1.5:8000')
        self.assertEqual(auto_name('https://llm.example.org/v1', 'anthropic'), 'llm.example.org (Anthropic)')
        self.assertEqual(auto_name(''), 'Провайдер')

    def test_audio_only_presets_do_not_name_a_text_provider(self):
        # у Groq два пресета (текст и Уши): обычный адрес Groq зовётся «Groq», а не «Groq Whisper — Уши»
        self.assertEqual(auto_name('https://api.groq.com/openai/v1'), 'Groq')

    def test_taken_name_gets_a_number(self):
        self.assertEqual(auto_name('http://127.0.0.1:9000/v1', taken=['Локальный 127.0.0.1:9000']), 'Локальный 127.0.0.1:9000 2')
        self.assertEqual(auto_name('http://127.0.0.1:9000/v1', taken=['локальный 127.0.0.1:9000', 'Локальный 127.0.0.1:9000 2']), 'Локальный 127.0.0.1:9000 3')


class ProviderNamesTest(TempDirCase):
    def setUp(self):
        super().setUp()
        self.store = Store(self.tmp / 'b.db')
        self.addCleanup(self.store.close)
        self.secrets = Secrets(self.tmp / 'secrets.json')

    def models(self):
        return ModelsConfig(Settings(self.store), self.secrets)

    def test_custom_providers_without_a_name_are_told_apart(self):
        models = self.models()
        first = models.add_provider(kind='openai', base_url='http://127.0.0.1:9000/v1')
        second = models.add_provider(kind='openai', base_url='http://127.0.0.1:9001/v1')
        third = models.add_provider(kind='openai', base_url='http://127.0.0.1:9000/v1')
        generic = models.add_provider(name='Свой провайдер', kind='openai', base_url='https://llm.example.org/v1')
        self.assertEqual([p['name'] for p in (first, second, third, generic)],
                         ['Локальный 127.0.0.1:9000', 'Локальный 127.0.0.1:9001', 'Локальный 127.0.0.1:9000 2', 'llm.example.org'])

    def test_name_given_by_the_owner_and_preset_names_are_kept(self):
        models = self.models()
        own = models.add_provider(name='Домашний сервер', kind='openai', base_url='http://127.0.0.1:9000/v1')
        preset = models.add_provider(preset='ollama')
        moved = models.add_provider(preset='ollama', base_url='http://127.0.0.1:11500/v1')  # пресет на другом порту остаётся «Ollama»
        self.assertEqual((own['name'], preset['name'], moved['name']), ('Домашний сервер', 'Ollama', 'Ollama'))

    def test_providers_already_called_the_default_get_names_by_address_on_start(self):
        models = self.models()
        old = {}
        for n, url in enumerate(('http://127.0.0.1:20128/v1', 'http://127.0.0.1:9000/v1', 'http://127.0.0.1:9000/v1')):
            record = models.add_provider(name='Домашний', kind='openai', base_url=url)
            old[record['id']] = record
        providers = models.providers()
        for pid in old:  # как их создавал прежний пульт: имя «Свой провайдер» у всех
            providers[pid] = {**providers[pid], 'name': 'Свой провайдер'}
        keep = models.add_provider(name='Мой выбор', kind='openai', base_url='http://127.0.0.1:7000/v1')
        with models.settings.transaction():
            models.settings.replace('providers', {**providers, keep['id']: models.providers()[keep['id']]})
        names = [p['name'] for pid, p in self.models().providers().items() if pid in old]
        self.assertEqual(sorted(names), sorted(['Локальный 127.0.0.1:9000', 'Локальный 127.0.0.1:9000 2', 'OmniRoute']))
        self.assertEqual(self.models().providers()[keep['id']]['name'], 'Мой выбор')
        self.assertEqual(sorted(p['name'] for p in self.models().providers().values()), sorted(names + ['Мой выбор']))  # повторный старт ничего не меняет


class PultApiTest(TempDirCase):
    @async_test
    async def test_pult_sends_no_name_and_the_provider_is_named_by_its_address(self):
        app = App(self.tmp / 'data')
        web = HttpServer(owner_web(app, allowed_hosts={'127.0.0.1'}))
        await web.start('127.0.0.1', 0)
        try:
            body = {'base_url': 'http://127.0.0.1:9000/v1', 'kind': 'openai', 'api_key': 'ключ'}
            code, first = await asyncio.to_thread(_request, web.port, 'POST', '/api/providers', body)
            self.assertEqual(code, 200, first)
            code, second = await asyncio.to_thread(_request, web.port, 'POST', '/api/providers', {**body, 'base_url': 'https://llm.example.org/v1'})
            code, third = await asyncio.to_thread(_request, web.port, 'POST', '/api/providers', body)
            self.assertEqual([r['provider']['name'] for r in (first, second, third)],
                             ['Локальный 127.0.0.1:9000', 'llm.example.org', 'Локальный 127.0.0.1:9000 2'])
        finally:
            await web.stop()
            await app.close()

    def test_pult_does_not_hardcode_the_same_name_for_every_custom_provider(self):
        source = (Path(__file__).resolve().parent.parent / 'bender' / 'web' / 'static' / 'screens' / 'models.js').read_text(encoding='utf-8')
        self.assertNotIn('Свой провайдер', source)
