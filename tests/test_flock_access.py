"""Стая и общие ресурсы: папку программы можно посмотреть, чужая память закрыта;
MCP-сервер stdio один на всю стаю, его журнал и запуск видны агенту без секретов."""
import json
import sys

from bender.core.errors import HumanError
from bender.mcp.manager import hide_args, tool_name
from bender.tools.registry import ToolContext
from tests.helpers import TempDirCase, async_test
from tests.test_mcp_client import FAKE
from tests.test_mind import MindEnv


class FlockAccessTest(TempDirCase):
    @async_test
    async def test_parent_of_data_is_listable_but_private_insides_stay_closed(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            app.agents.create('Второй', agent_id='second', awaken=False, profile='full')
            other = app.agents.get('second')
            (other.dirs.self_dir / 'MEMORY.md').write_text('тайна второго агента')
            (app.dirs.work / 'notes.txt').write_text('тайна главного')
            parent = app.dirs.root.parent
            other.settings.set('guard', {'profile': 'off'})  # тумблер выключен, граница стаи остаётся
            ctx = ToolContext(other, session_id='s')
            listing = await other.tools.run('list_dir', {'path': str(parent)}, ctx)
            self.assertTrue(listing.ok, listing.content)
            self.assertIn(app.dirs.root.name + '/', listing.content)
            found = await other.tools.run('find_files', {'pattern': '*.md', 'path': str(parent)}, ctx)
            self.assertIn('agents/second/self/MEMORY.md', found.content, 'свою память видит')
            self.assertNotIn(app.dirs.root.name + '/self/', found.content, 'чужую — нет')
            grep = await other.tools.run('grep', {'pattern': 'тайна', 'path': str(parent)}, ctx)
            self.assertTrue(grep.ok, grep.content)
            self.assertNotIn('тайна главного', grep.content)
            read = await other.tools.run('read_file', {'path': str(app.dirs.work / 'notes.txt')}, ctx)
            self.assertFalse(read.ok)
            self.assertIn('другого агента', read.content)
            # импорт из родительской папки по-прежнему закрыт (там внутри данные Бендера)
            self.assertTrue(app.agents.private_path(other, parent, 'fs.read'))

    def test_secret_flags_are_hidden_in_command(self):
        self.assertEqual(hide_args(['tg', '--api-hash', 'abc', '--session=/x/s', '--chat', 'me', '-v']),
                         ['tg', '--api-hash', '[скрыто]', '--session=[скрыто]', '--chat', 'me', '-v'])

    @async_test
    async def test_one_stdio_process_for_flock_log_and_diagnose(self):
        state = self.tmp / 'server' / 'state.json'
        state.parent.mkdir()
        state.write_text(json.dumps({'track_pids': True}))
        async with MindEnv(self.tmp / 'mind') as env:
            app = env.app
            app.agents.create('Помощник', agent_id='helper', awaken=False, profile='full')
            helper = app.agents.get('helper')
            app.settings.set('guard', {'profile': 'off'})
            helper.settings.set('guard', {'profile': 'off'})
            await app.mcp.save({'id': 'tg', 'transport': 'stdio', 'enabled': True, 'cwd': str(state.parent),
                                'command': [sys.executable, str(FAKE), str(state), '--api-hash', 'ОЧЕНЬ-ТАЙНО'],
                                'env': {'MCP_TEST_CONFIGURED': 'секрет-окружения'}})
            panel = await app.mcp.discover('tg', ToolContext(app, actor='owner'))
            app.mcp.approve('tg', panel['servers'][0]['review'])
            first = await app.mcp.call('tg', 'echo', {'text': 'раз'}, ToolContext(app, session_id='a'))
            second = await helper.mcp.call('tg', 'echo', {'text': 'два'}, ToolContext(helper, session_id='b'))
            self.assertTrue(first.ok and second.ok, (first.content, second.content))
            self.assertEqual(len(json.loads(state.read_text())['pids']), 1, 'второй агент не запускает вторую копию')
            pool = app.mcp._host_state()['pool']['tg'][1].transport
            self.assertIs(helper.mcp.clients['tg'][1].transport, pool)
            self.assertNotIn('RLIMIT_CPU', app.mcp._protection(app.mcp.get('tg'), ToolContext(app))['limits'])
            # диагностика: журнал без значений секретов, команда без значения секретного флага
            result = await helper.tools.run('mcp_diagnose', {'server': 'tg'}, ToolContext(helper, session_id='b'))
            self.assertTrue(result.ok, result.content)
            self.assertIn('fake mcp started', result.content)
            self.assertNotIn('секрет-окружения', result.content)
            self.assertNotIn('ОЧЕНЬ-ТАЙНО', result.content)
            self.assertIn('MCP_TEST_CONFIGURED', result.content)
            self.assertTrue(result.tainted)
            listing = await helper.tools.run('mcp_diagnose', {}, ToolContext(helper, session_id='b'))
            self.assertIn('tg (stdio, общий для стаи): подключён', listing.content)
            # сервер упал — код выхода и ошибка видны
            pool.process.kill()
            await pool.process.wait()
            failed = await helper.tools.run(tool_name('tg', 'echo'), {'text': 'три'}, ToolContext(helper, session_id='b'))
            self.assertFalse(failed.ok)
            info = helper.mcp.diagnostics(helper.mcp.get('tg'))
            self.assertIsNotNone(info['exit'])
            self.assertTrue(info['error'])
            # не выданный агенту сервер не виден в диагностике
            await app.mcp.save({'id': 'tg', 'agents': ['main']})
            hidden = await helper.tools.run('mcp_diagnose', {}, ToolContext(helper, session_id='b'))
            self.assertNotIn('tg', hidden.content)

    @async_test
    async def test_choosing_agents_keeps_approval_and_agent_seen_change_shows_review(self):
        state = self.tmp / 'server' / 'state.json'
        state.parent.mkdir()
        state.write_text('{}')
        async with MindEnv(self.tmp / 'mind') as env:
            app = env.app
            app.agents.create('Помощник', agent_id='helper', awaken=False, profile='full')
            helper = app.agents.get('helper')
            await app.mcp.save({'id': 'tg', 'transport': 'stdio', 'enabled': True, 'cwd': str(state.parent),
                                'command': [sys.executable, str(FAKE), str(state)]})
            panel = await app.mcp.discover('tg', ToolContext(app, actor='owner'))
            app.mcp.approve('tg', panel['servers'][0]['review'])
            # старый сервер с открытым значением в env (до сейфа) — выбор агентов его не переписывает
            servers = app.mcp.servers()
            servers[0]['env'] = {'LEGACY': 'plain'}
            app.settings.set('mcp', {'servers': servers}, by='owner')
            await app.mcp.discover('tg', ToolContext(app, actor='owner'))
            app.mcp.approve('tg', app.mcp.public()['servers'][0]['review'])
            panel = await app.mcp.save({'id': 'tg', 'agents': ['helper']})
            self.assertEqual(app.mcp.get('tg')['env'], {'LEGACY': 'plain'})
            self.assertTrue(all(t['approved'] for t in panel['servers'][0]['tools']))
            # сервер поменял описание — агент это заметил, экран показывает кнопку одобрения
            state.write_text(json.dumps({'description': 'Новое описание'}))
            helper.settings.set('guard', {'profile': 'off'})
            with self.assertRaises(HumanError):
                await helper.mcp.call('tg', 'echo', {'text': 'x'}, ToolContext(helper, session_id='b'))
            server = app.mcp.public()['servers'][0]
            self.assertIsNotNone(server['review'])
            self.assertFalse(server['tools'][0]['approved'])
