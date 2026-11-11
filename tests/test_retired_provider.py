"""Прежний CLI не запускается; настройки и незнакомые поля сохраняются."""
import sys
from pathlib import Path
from bender.core.app import App
from bender.core.errors import HumanError
from bender.router import codex_probe
from bender.router.config import PRESETS
from tests.helpers import TempDirCase, async_test


class RetiredProviderTest(TempDirCase):
    @async_test
    async def test_old_provider_disabled_once_and_fallback_survives(self):
        app = App(self.tmp / 'data')
        p = app.models.add_provider(preset='fake')
        app.settings.replace('providers', {p['id']: p, 'old': {'id': 'old', 'kind': 'gemini_cli', 'enabled': True, 'future': {'keep': 17}}})
        app.models.set_role('main', [{'provider': p['id'], 'model': 'echo'}])
        app.models.set_role('fast', [{'provider': 'old', 'model': 'default'}, {'provider': p['id'], 'model': 'echo'}])
        await app.close()
        app = App(self.tmp / 'data')
        try:
            old = app.models.providers()['old']
            self.assertFalse(old['enabled'])
            self.assertEqual(old['future'], {'keep': 17})
            self.assertEqual(app.router.chain('fast')[0].model, 'echo')
            with self.assertRaises(HumanError): app.router.explicit('old', 'default')
            app.models.set_role('fast', [{'provider': 'old', 'model': 'default'}])
            self.assertEqual(app.router.chain('fast')[0].model, 'echo')
            self.assertIn('Google API', app.store.meta_get('models:removed_notice'))
            app.store.meta_set('models:removed_notice', None)
            with self.assertRaises(HumanError): app.models.update_provider('old', {'enabled': True})
            with self.assertRaises(HumanError): app.models.add_provider(preset='gemini_cli')
            self.assertNotIn('gemini_cli', PRESETS)
            self.assertEqual(PRESETS['gemini']['kind'], 'openai')
        finally: await app.close()
        app = App(self.tmp / 'data')
        try: self.assertIsNone(app.store.meta_get('models:removed_notice'))
        finally: await app.close()
        app = App(self.tmp / 'data')
        try: app.store.meta_set('models:removed_notice', 'Провайдер Gemini CLI убран: Gemini CLI больше не существует. Подключи модели через OmniRoute или Google API по ключу')
        finally: await app.close()
        app = App(self.tmp / 'data')  # незакрытое прежнее уведомление заменяется честным текстом
        try:
            notice = app.store.meta_get('models:removed_notice')
            self.assertNotIn('больше не существует', notice); self.assertIn('Antigravity', notice)
        finally: await app.close()

    @async_test
    async def test_codex_probe_stays_blocked(self):
        result = await codex_probe.check({'command': [sys.executable, str(Path(__file__).parent / 'fixtures/fake_codex.py')]})
        self.assertEqual(result['version'], '0.163.0')
        self.assertFalse(result['safe_tools'])
