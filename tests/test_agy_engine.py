"""Движок Antigravity (превью) на фальшивом agy: риск принимается явно, встроенные инструменты agy выключены, тулов Бендера нет."""
import json
import sys

from bender.core.errors import HumanError
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv

FAKE = '''#!{py}
import json, sys
args = sys.argv[1:]
if "--help" in args:
    print("agy -p --output-format stream-json {flags}")
elif "--version" in args:
    print("agy 0.0.1")
else:
    prompt = sys.stdin.read()
    open({log!r}, "a").write(json.dumps({{"argv": args, "prompt": prompt}}) + "\\n")
    print(json.dumps({{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "Ответ agy."}}]}}}}))
    print(json.dumps({{"type": "result", "result": "Ответ agy."}}))
'''


class AgyEngineTest(TempDirCase):
    def fake(self, flags):
        script = self.tmp / 'agy'
        self.log = self.tmp / 'agy.log'
        script.write_text(FAKE.format(py=sys.executable, flags=flags, log=str(self.log)))
        script.chmod(0o755)
        return [str(script)]

    @async_test
    async def test_risk_must_be_accepted(self):
        async with MindEnv(self.tmp) as env:
            with self.assertRaises(HumanError) as caught:
                env.app.models.add_provider(preset='antigravity', extra={'command': ['agy']})
            self.assertEqual(caught.exception.code, 'agy_risk')

    @async_test
    async def test_text_only_with_tools_disabled(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            p = app.models.add_provider(preset='antigravity', extra={'risk_accepted': True, 'command': self.fake('--no-tools')})
            app.models.set_role('fast', [{'provider': p['id'], 'model': 'default'}])
            result = await app.router.call([{'role': 'user', 'content': 'Привет'}], role='fast')
            self.assertEqual(result.text, 'Ответ agy.')
            run = json.loads(self.log.read_text().splitlines()[0])
            self.assertIn('--no-tools', run['argv']);self.assertIn('Привет', run['prompt'])

    @async_test
    async def test_refuses_without_a_way_to_disable_builtin_tools_and_with_tools(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            p = app.models.add_provider(preset='antigravity', extra={'risk_accepted': True, 'command': self.fake('--model')})
            app.models.set_role('fast', [{'provider': p['id'], 'model': 'default'}])
            with self.assertRaises(Exception) as caught:
                await app.router.call([{'role': 'user', 'content': 'Привет'}], role='fast')
            self.assertIn('не принял запрос', str(caught.exception))
            self.assertFalse(self.log.exists())  # agy даже не запускали для хода
            q = app.models.add_provider(preset='antigravity', extra={'risk_accepted': True, 'command': self.fake('--no-tools')})
            app.models.set_role('fast', [{'provider': q['id'], 'model': 'default'}])
            with self.assertRaises(Exception) as caught:
                await app.router.call([{'role': 'user', 'content': 'Привет'}], tools=[{'type': 'function', 'function': {'name': 'x', 'parameters': {'type': 'object', 'properties': {}}}}], role='fast')
            self.assertIn('не принял запрос', str(caught.exception))
            self.assertFalse(self.log.exists(), 'с тулами agy тоже не запускали')
