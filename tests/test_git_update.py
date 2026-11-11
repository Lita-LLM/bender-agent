"""Настоящий git/HTTP/помощник паролей; данные не откатываются вместе с программой."""
import asyncio
import base64
import contextlib
import functools
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import AsyncMock, patch

from bender.core.app import App
from bender.core.errors import HumanError
from bender.core.settings import NeedsOwner
from bender.guard.policy import Action
from bender.updates.git import GitUpdate
from tests.helpers import TempDirCase, async_test


class GitUpdateTest(TempDirCase):
    def git(self, where, *args):
        result = subprocess.run(['git', '-C', str(where), *args], capture_output=True, text=True)
        if result.returncode: raise AssertionError(result.stderr)
        return result.stdout.strip()

    def setup_repo(self):
        self.writer = self.tmp / 'writer'; self.writer.mkdir()
        self.git(self.writer, 'init', '-b', 'main')
        self.git(self.writer, 'config', 'user.name', 'Проверка')
        self.git(self.writer, 'config', 'user.email', 'test@example.invalid')
        (self.writer / 'bender').mkdir()
        (self.writer / 'bender/__init__.py').write_text('')
        self.program = 'import json\nprint(json.dumps({"ok": True}))\n'
        (self.writer / 'bender/__main__.py').write_text(self.program)
        (self.writer / '.gitignore').write_text('__pycache__/\n')
        self.git(self.writer, 'add', '.')
        self.git(self.writer, 'commit', '-m', 'Первая версия')
        self.server = self.tmp / 'server.git'
        self.git(self.tmp, 'clone', '--bare', str(self.writer), str(self.server))
        self.git(self.writer, 'remote', 'add', 'origin', str(self.server))
        self.clone = self.tmp / 'program'
        self.git(self.tmp, 'clone', str(self.server), str(self.clone))
        self.git(self.clone, 'config', 'user.name', 'Проверка')
        self.git(self.clone, 'config', 'user.email', 'test@example.invalid')

    def publish(self, *, broken=False):
        (self.writer / 'bender/__main__.py').write_text('raise RuntimeError("Поломка")\n' if broken else self.program + '# новая версия\n')
        self.git(self.writer, 'add', '.')
        self.git(self.writer, 'commit', '-m', 'Поломка для проверки отката' if broken else 'Исправить программу')
        self.git(self.writer, 'push', 'origin', 'main')
        self.git(self.server, 'update-server-info')
        return self.git(self.writer, 'rev-parse', 'HEAD')

    def application(self):
        app = App(self.tmp / 'data')
        app.settings.set('backups', {'path': str(self.tmp / 'copies')})
        app._updater = GitUpdate(app, self.clone)
        return app

    @async_test
    async def test_new_version_is_announced_to_owner_and_agents(self):
        from bender.mind.context import system_prompt
        from bender.updates.news import announce
        self.setup_repo(); app = self.application()
        app.settings.set('updates', {'news': 'notice'})  # здесь — простое уведомление; пробуждение агента — отдельный тест
        events = []; app.bus.listen(lambda ev: ev.get('type') == 'notify' and events.append(ev))
        try:
            old = self.git(self.clone, 'rev-parse', 'HEAD')
            await announce(app)
            self.assertEqual(events, [], 'первый запуск только запоминает версию')
            self.publish(); self.git(self.clone, 'pull', '--ff-only', 'origin', 'main')
            await announce(app)
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]['kind'], 'update')
            self.assertIn('Бендер обновился', events[0]['text']); self.assertIn('• Исправить программу', events[0]['text'])
            await announce(app)
            self.assertEqual(len(events), 1, 'та же версия — без повторов')
            app.agents.create('Луна', agent_id='luna', awaken=False, start=False)
            for ctx in (app, app.agents.get('luna')):
                prompt = system_prompt(ctx)
                self.assertIn('# Обновление программы', prompt); self.assertIn('Исправить программу', prompt)
            self.git(self.clone, 'reset', '--hard', old)
            await announce(app)
            self.assertIn('вернулся на прежнюю версию', events[-1]['text']); self.assertIn('отменено: Исправить программу', events[-1]['text'])
            news = app.store.meta_get('updates:news'); news['at'] -= 8 * 86400; app.store.meta_set('updates:news', news)
            self.assertNotIn('# Обновление программы', system_prompt(app), 'через неделю новость уходит из промпта')
        finally: await app.close()

    @async_test
    async def test_new_version_wakes_agent_to_tell_owner_itself(self):
        from bender.updates.news import announce
        self.setup_repo(); app = self.application()
        events = []; app.bus.listen(lambda ev: ev.get('type') == 'notify' and events.append(ev))
        calls = []
        async def say(sid, text, **kw):
            calls.append((sid, text, kw)); return {'text': 'Обновился!', 'error': None}
        try:
            await announce(app)
            self.publish(); self.git(self.clone, 'pull', '--ff-only', 'origin', 'main')
            with patch.object(app.mind, 'say', say):
                await announce(app); await asyncio.gather(*app._update_tells)
            self.assertEqual(len(calls), 1); sid, text, kw = calls[0]
            self.assertEqual(sid, app.main_session()); self.assertEqual(kw['channel'], 'update'); self.assertTrue(kw['unattended'])
            self.assertIn('Напиши хозяину', text); self.assertIn('• Исправить программу', text)
            self.assertEqual(events, [], 'рассказывает агент, отдельного уведомления нет')
            async def broken(sid, text, **kw): raise RuntimeError('модель недоступна')
            self.git(self.clone, 'reset', '--hard', 'HEAD~1')
            with patch.object(app.mind, 'say', broken):
                await announce(app); await asyncio.gather(*app._update_tells)
            self.assertEqual(len(events), 1); self.assertIn('вернулся на прежнюю версию', events[0]['text'], 'агент не смог — хозяин всё равно узнаёт')
            # Хозяин в Telegram говорит с Луной в отдельном разговоре — рассказывает она, там же.
            from bender.channels.telegram import OWNER_META
            from bender.channels.telegram_topics import Address
            from bender.mind.context import system_prompt
            app.store.meta_set(OWNER_META, {'user_id': 42, 'chat_id': 42, 'name': 'Хозяин'})
            luna = app.agents.get(app.agents.create('Луна', agent_id='luna', awaken=False, start=False)['id'])
            talk = luna.store.new_session('chat', 'Про дом')
            app.telegram.choose(Address(42), luna, talk)
            calls.clear()
            self.git(self.clone, 'pull', '--ff-only', 'origin', 'main')
            with patch.object(app.mind, 'say', say), patch.object(luna.mind, 'say', say):
                await announce(app); await asyncio.gather(*app._update_tells)
            self.assertEqual([c[0] for c in calls], [talk], 'рассказывает агент, выбранный в Telegram, в открытом там разговоре')
            self.assertIn('Ты сам рассказал', system_prompt(luna))
            self.assertIn('Хозяину об этом уже сообщили', system_prompt(app))
        finally: await app.close()

    @async_test
    async def test_agent_checks_and_asks_owner_to_install_update(self):
        from bender.tools.registry import ToolContext
        self.setup_repo(); app = self.application()
        answers = ['deny', 'once']; cards = []
        def on(ev):
            if ev.get('type') == 'approval':
                cards.append(ev['card']); asyncio.get_running_loop().call_soon(app.guard.respond, ev['card']['id'], answers.pop(0))
        app.bus.listen(on)
        try:
            old = self.git(self.clone, 'rev-parse', 'HEAD')
            ctx = ToolContext(app, actor='agent', session_id=app.main_session())
            result = await app.tools.run('check_and_update', {}, ctx)
            self.assertTrue(result.ok); self.assertIn('Установлена последняя версия', result.content)
            target = self.publish()
            result = await app.tools.run('check_and_update', {}, ctx)
            self.assertIn('Исправить программу', result.content); self.assertEqual(cards, [], 'проверка без карточки')
            result = await app.tools.run('check_and_update', {'install': True}, ctx)
            self.assertFalse(result.ok); self.assertEqual(self.git(self.clone, 'rev-parse', 'HEAD'), old, 'хозяин отказал — ничего не поставлено')
            restarted = []
            async def restart(): restarted.append(True); return 'Бендер перезапускается через Сторожа.'
            with patch.object(app.updater, 'restart', restart):
                result = await app.tools.run('check_and_update', {'install': True}, ctx)
                self.assertTrue(result.ok, result.content); self.assertIn('после конца', result.content)
                await asyncio.wait_for(app._agent_update, 30)
            self.assertEqual(len(cards), 2); self.assertIn('Исправить программу', json.dumps(cards[-1]['detail'], ensure_ascii=False))
            self.assertEqual(self.git(self.clone, 'rev-parse', 'HEAD'), target); self.assertEqual(restarted, [True])
            self.publish(broken=True)
            unattended = ToolContext(app, actor='agent', session_id=app.main_session(), unattended=True)
            self.assertFalse((await app.tools.run('check_and_update', {'install': True}, unattended)).ok, 'без хозяина рядом — отказ')
        finally: await app.close()

    @async_test
    async def test_real_check_install_backup_and_doctor_rollback(self):
        self.setup_repo(); app = self.application()
        marker = app.dirs.work / 'важное.txt'; marker.write_text('данные хозяина')
        try:
            old = self.git(self.clone, 'rev-parse', 'HEAD'); target = self.publish()
            plan = await app.updater.check()
            self.assertTrue(plan['available']); self.assertEqual(self.git(self.clone, 'rev-parse', 'HEAD'), old)
            self.assertIn('Исправить программу', '\n'.join(plan['commits']))
            result = await app.updater.install(plan)
            self.assertTrue(result['updated']); self.assertEqual(self.git(self.clone, 'rev-parse', 'HEAD'), target)
            self.assertTrue(app.backups.snapshots(app.backups.roots()[0]))
            self.publish(broken=True)
            plan = await app.updater.check()
            with self.assertRaises(HumanError) as failed: await app.updater.install(plan)
            self.assertIn('возвращена прежняя', failed.exception.title)
            self.assertEqual(self.git(self.clone, 'rev-parse', 'HEAD'), target)
            self.assertEqual(marker.read_text(), 'данные хозяина')
            self.assertIsNone(app.store.meta_get('updates.in_progress'))
        finally: await app.close()

    @async_test
    async def test_backup_failure_falls_back_then_stops_with_a_way_out(self):
        self.setup_repo(); app = self.application()
        try:
            target = self.publish()
            plan = await app.updater.check()
            broken = HumanError('backup_read', 'Не удалось прочитать файл бекапа', 'Проверь носитель.')
            with patch.object(app.backups, 'now', AsyncMock(side_effect=broken)):
                # основные папки сбоят — снимок уходит в запасную папку обновлений, обновление идёт
                with patch('pathlib.Path.home', return_value=self.tmp / 'home'):
                    result = await app.updater.install(plan)
            self.assertTrue(result['updated']); self.assertIn('запасной папке', result['backup']['note'])
            self.assertEqual(self.git(self.clone, 'rev-parse', 'HEAD'), target)
        finally: await app.close()

    @async_test
    async def test_backup_failure_everywhere_stops_with_a_way_out(self):
        self.setup_repo(); app = self.application()
        try:
            self.publish()
            plan = await app.updater.check()
            broken = HumanError('backup_read', 'Не удалось прочитать файл бекапа', 'Проверь носитель.')
            with patch.object(app.backups, 'now', AsyncMock(side_effect=broken)), patch.object(app.updater, 'reserve_backup', AsyncMock(side_effect=broken)):
                with self.assertRaises(HumanError) as failed: await app.updater.install(plan)
            self.assertEqual(failed.exception.code, 'update_backup')
            self.assertIn('без бекапа', failed.exception.hint)
            self.assertEqual(self.git(self.clone, 'rev-parse', 'HEAD'), plan['head'], 'ничего не установлено')
        finally: await app.close()

    @async_test
    async def test_skip_backup_once_is_consumed(self):
        self.setup_repo(); app = self.application()
        try:
            target = self.publish()
            plan = await app.updater.check()
            app.settings.set('updates', {'skip_backup_once': True})
            with patch.object(app.backups, 'now', AsyncMock(side_effect=AssertionError('бекап не должен запускаться'))):
                result = await app.updater.install(plan)
            self.assertTrue(result['updated']); self.assertTrue(result['backup']['skipped'])
            self.assertFalse(app.settings.get('updates', 'skip_backup_once'), 'выключатель гаснет сам')
            self.assertEqual(self.git(self.clone, 'rev-parse', 'HEAD'), target)
            self.assertIn('Без бекапа', app.store.meta_get('updates:last')['message'])
        finally: await app.close()

    @async_test
    async def test_failed_self_check_names_the_reason(self):
        self.setup_repo(); app = self.application()
        try:
            self.publish(broken=True)
            plan = await app.updater.check()
            with self.assertRaises(HumanError) as failed: await app.updater.install(plan)
            self.assertIn('Что не прошло', failed.exception.hint)
        finally: await app.close()

    @async_test
    async def test_realign_after_server_rewrote_history(self):
        self.setup_repo(); app = self.application()
        marker = app.dirs.work / 'важное.txt'; marker.write_text('данные хозяина')
        try:
            old = self.git(self.clone, 'rev-parse', 'HEAD')
            self.git(self.writer, 'commit', '--amend', '-m', 'Первая версия, история пересобрана', '--date', '2020-01-01T00:00:00')
            self.git(self.writer, 'push', '--force', 'origin', 'main')
            new = self.git(self.writer, 'rev-parse', 'HEAD')
            self.assertNotEqual(old, new)
            with self.assertRaises(HumanError) as failed: await app.updater.check()
            self.assertEqual(failed.exception.code, 'git_diverged'); self.assertIn('Выровнять', failed.exception.hint)
            result = await app.updater.realign()
            self.assertTrue(result['realigned'])
            self.assertEqual(self.git(self.clone, 'rev-parse', 'HEAD'), new)
            self.assertEqual(self.git(self.clone, 'rev-parse', result['saved_branch']), old, 'прежний коммит сохранён')
            self.assertEqual(marker.read_text(), 'данные хозяина')
            self.assertFalse((await app.updater.check())['available'])
            self.assertFalse((await app.updater.realign())['realigned'])
        finally: await app.close()

    @async_test
    async def test_clean_reinstall_wipes_program_edits_but_keeps_data(self):
        self.setup_repo(); app = self.application()
        data = app.dirs.work / 'важное.txt'; data.write_text('данные хозяина')
        try:
            (self.clone / 'bender/__main__.py').write_text('# чужая правка\n')
            (self.clone / 'bender/лишний.py').write_text('x=1\n')
            with self.assertRaises(HumanError): await app.updater.check()
            result = await app.updater.realign(clean=True)
            self.assertTrue(result['realigned'])
            self.assertEqual((self.clone / 'bender/__main__.py').read_text(), self.program)
            self.assertFalse((self.clone / 'bender/лишний.py').exists())
            self.assertEqual(data.read_text(), 'данные хозяина')
            self.assertFalse((await app.updater.check())['available'])
        finally: await app.close()

    @async_test
    async def test_stray_files_outside_code_do_not_block(self):
        self.setup_repo(); app = self.application()
        try:
            (self.clone / 'nohup.out').write_text('лог запуска'); (self.clone / 'заметки.txt').write_text('своё')
            (self.writer / 'НОВОЕ.md').write_text('из обновления'); (self.clone / 'НОВОЕ.md').write_text('своё, не затирать')
            target = self.publish()
            plan = await app.updater.check()
            self.assertTrue(plan['available'])
            with self.assertRaises(HumanError) as failed: await app.updater.install(plan)
            self.assertIn('НОВОЕ.md', failed.exception.title)  # обновление затёрло бы чужой файл — называем его
            self.assertEqual((self.clone / 'НОВОЕ.md').read_text(), 'своё, не затирать')
            (self.clone / 'НОВОЕ.md').unlink()
            self.assertTrue((await app.updater.install(plan))['updated'])
            self.assertEqual(self.git(self.clone, 'rev-parse', 'HEAD'), target)
            self.assertEqual((self.clone / 'nohup.out').read_text(), 'лог запуска')
            (self.clone / 'json.py').write_text('# подменил бы модуль Python службы\n')
            with self.assertRaises(HumanError) as failed: await app.updater.check()
            self.assertIn('json.py', failed.exception.title)
        finally: await app.close()

    @async_test
    async def test_local_changes_and_divergence_refuse(self):
        self.setup_repo(); app = self.application(); self.publish()
        try:
            file = self.clone / 'bender/__main__.py'; file.write_text('# своё\n')
            with self.assertRaises(HumanError) as failed: await app.updater.check()
            self.assertIn('локальные правки', failed.exception.title)
            self.assertEqual(file.read_text(), '# своё\n')
            self.git(self.clone, 'add', '.'); self.git(self.clone, 'commit', '-m', 'Свой коммит')
            head = self.git(self.clone, 'rev-parse', 'HEAD')
            with self.assertRaises(HumanError) as failed: await app.updater.check()
            self.assertIn('разошлась', failed.exception.title)
            self.assertEqual(self.git(self.clone, 'rev-parse', 'HEAD'), head)
        finally: await app.close()

    @async_test
    async def test_private_http_uses_gh_helper_and_service_path_without_printing_token(self):
        self.setup_repo(); self.publish(); app = self.application()
        app.agents.create('Второй',agent_id='second',awaken=False)
        token = 'private-test-SECRET-423846'
        authorization = 'Basic ' + base64.b64encode(('owner:' + token).encode()).decode()
        class Handler(SimpleHTTPRequestHandler):
            def do_GET(self):
                if self.headers.get('Authorization') != authorization:
                    self.send_response(401); self.send_header('WWW-Authenticate', 'Basic realm="git"'); self.end_headers(); return
                super().do_GET()
            def log_message(self, *args): pass
        http = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(self.tmp)))
        thread = threading.Thread(target=http.serve_forever, daemon=True); thread.start()
        self.git(self.clone, 'remote', 'set-url', 'origin', f'http://127.0.0.1:{http.server_port}/server.git')
        # Локальная фикстура не находится за внешним HTTP-прокси облака.
        self.git(self.clone, 'config', f'http.http://127.0.0.1:{http.server_port}/.proxy', '')
        self.git(self.clone, 'config', 'credential.helper', '')
        try:
            with self.assertRaises(HumanError) as failed: await app.updater.check()
            self.assertIn('gh auth login', failed.exception.hint)
            self.assertNotIn(token, str(failed.exception.to_dict()))
            home = self.tmp / 'home'; bindir = home / '.local/bin'; bindir.mkdir(parents=True)
            helper = bindir / 'gh'
            helper_log = home / 'helper-called'
            helper.write_text('#!' + sys.executable + '\nimport sys\nfrom pathlib import Path\nPath(' + repr(str(helper_log)) + ').write_text(sys.argv[-1])\nsys.stdin.read()\nif sys.argv[-1] == "get":\n print("username=owner")\n print(' + repr('password=' + token) + ')\n print()\n')
            helper.chmod(0o700)
            self.git(self.clone, 'config', 'credential.helper', '!gh auth git-credential')
            service = self.tmp / 'service-bin'; service.mkdir()
            if os.name=='nt':
                import shlex
                # Git for Windows запускает credential helper своей sh; Python — явно.
                script=helper.with_suffix('.py');script.write_bytes(helper.read_bytes())
                helper.write_text('#!/bin/sh\nexec '+shlex.quote(Path(sys.executable).as_posix())+' '+shlex.quote(script.as_posix())+' "$@"\n')
                service=Path(shutil.which('git')).parent
            else:
                (service / 'git').symlink_to(shutil.which('git'))
            with patch('pathlib.Path.home', return_value=home), patch.dict(os.environ, {'PATH': str(service)}):
                plan = await app.updater.check()
                self.assertTrue(helper_log.exists())
                result = await app.updater.install(plan)
            self.assertTrue(result['updated'])
            self.assertNotIn(token, json.dumps(result))
        finally:
            http.shutdown(); http.server_close(); thread.join()
            await app.close()

    @async_test
    async def test_night_is_owner_only_notify_once_and_failure_in_morning(self):
        self.setup_repo(); app = self.application(); self.publish(broken=True)
        events = []; app.bus.listen(events.append)
        try:
            with self.assertRaises(NeedsOwner): app.settings.set('updates', {'auto': 'night'}, by='agent')
            app.updater.next_at = 0
            await app.updater.tick(); await app.updater.tick()
            self.assertEqual(len([e for e in events if e.get('update')]), 1)
            app.settings.set('updates', {'auto': 'night'})
            old = self.git(self.clone, 'rev-parse', 'HEAD')
            await app.sleep.run()
            self.assertEqual(self.git(self.clone, 'rev-parse', 'HEAD'), old)
            reports = [e['message']['content'] for e in events if e.get('type') == 'message' and e.get('message', {}).get('kind') == 'dream']
            self.assertTrue(any('возвращена прежняя' in text for text in reports))
            self.assertIsNone(app.store.meta_get('updates:morning'))
            app.settings.set('updates', {'auto': 'off'})
            self.assertIsNone(app.updater.due())
        finally: await app.close()

    @async_test
    async def test_first_night_updates_with_default_backup_and_reports_now(self):
        self.setup_repo(); target = self.publish(); app = self.application()
        app.settings.set('backups', {'path': ''})
        app.settings.set('updates', {'auto': 'night'})
        events = []; app.bus.listen(events.append)
        home = self.tmp / 'home'; home.mkdir()
        try:
            with patch('pathlib.Path.home', return_value=home), patch.object(app.updater, 'restart', AsyncMock(return_value='тест')):
                await app.sleep.run()
            self.assertEqual(self.git(self.clone, 'rev-parse', 'HEAD'), target)
            self.assertTrue(list((home / '.local/share/bender/update-backups').glob('*/bender-backups/snapshots/s-*.json')))
            self.assertEqual(app.settings.get('backups', 'path'), '')
            self.assertIsNone(app.store.meta_get('updates:available'))
            self.assertTrue(any('Программа обновлена' in e.get('message', {}).get('content', '') for e in events))
            app.store.meta_set('updates.in_progress', {'git': True})
            app.mind.recover()
            self.assertTrue(any('Обновление git прервано' in m['content'] for m in app.store.messages(app.main_session())))
        finally: await app.close()

    @async_test
    async def test_owner_api_update_and_restart_modes(self):
        from bender.net.http_server import HttpServer
        from tests.helpers import owner_web as create_web, owner_headers
        from tests.test_diagnostics import _call
        self.setup_repo(); target = self.publish(); app = self.application()
        web = HttpServer(create_web(app, allowed_hosts={'127.0.0.1'})); await web.start('127.0.0.1', 0)
        try:
            with patch.object(app.updater, 'restart', AsyncMock(return_value='Перезапусти Бендера.')):
                code, plan = await asyncio.to_thread(_call, web.port, '/api/update/check', {})
                self.assertEqual(code, 200); self.assertTrue(plan['available'])
                code, result = await asyncio.to_thread(_call, web.port, '/api/update/install', plan)
                self.assertEqual(code, 200); self.assertTrue(result['updated'])
                self.assertEqual(self.git(self.clone, 'rev-parse', 'HEAD'), target)
            home = self.tmp / 'home'; unit = home / '.config/systemd/user/bender.service'; unit.parent.mkdir(parents=True)
            unit.write_text('Environment="BENDER_PROGRAM_DIR=' + str(self.clone) + '"\nEnvironment="BENDER_SERVICE_DATA=' + str(app.dirs.root) + '"\n')
            with patch('pathlib.Path.home', return_value=home), patch.object(app.http, 'get', AsyncMock(side_effect=OSError())), patch('bender.updates.git.command', AsyncMock(return_value=(0, ''))) as restart:
                self.assertIn('службой', await app.updater.restart())
                self.assertEqual(restart.call_args.args[0], ['systemctl', '--user', 'restart', 'bender'])
                unit.write_text('Environment="BENDER_PROGRAM_DIR=' + str(self.clone) + '"\nEnvironment="BENDER_SERVICE_DATA=/другие-данные"\n')
                self.assertEqual(await app.updater.restart(), 'Перезапусти Бендера.')
            called = []; app.watched = True; app.request_restart = lambda: called.append(True)
            self.assertIn('Сторожа', await app.updater.restart())
            await asyncio.sleep(.35); self.assertEqual(called, [True])
        finally:
            await web.stop(); await app.close()

    @async_test
    async def test_agent_cannot_update_even_with_off_or_grant_and_zip_explains(self):
        self.setup_repo(); app = self.application()
        try:
            app.settings.set('guard', {'profile': 'off', 'hardline': False})
            for action in (Action('update.install', 'git', actor='agent', unattended=True), Action('exec', 'bender update --yes'),
                           Action('exec', 'git -C ' + str(Path(__file__).resolve().parents[1]) + ' pull')):
                app.guard.grant('test', [(action.capability, '*')])
                decision = await app.guard.check(action, session_id='test')
                self.assertFalse(decision.allowed)
            (self.clone / '.git').rename(self.clone / '.git-away')
            with self.assertRaises(HumanError) as failed: await app.updater.check()
            self.assertIn('архива', failed.exception.title)
        finally: await app.close()

    def test_cli_check_and_confirmed_update_use_same_code(self):
        from bender.__main__ import build_parser
        from bender.updates.cli import main
        self.setup_repo(); self.publish(); app = self.application()
        old = self.git(self.clone, 'rev-parse', 'HEAD')
        with patch('bender.core.app.App', return_value=app), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(main(build_parser().parse_args(['update', '--check'])), 0)
        self.assertEqual(self.git(self.clone, 'rev-parse', 'HEAD'), old)
        app = self.application()
        out = io.StringIO()
        with patch('bender.core.app.App', return_value=app), contextlib.redirect_stdout(out):
            code = main(build_parser().parse_args(['update', '--yes']))
        self.assertEqual(code, 0)
        self.assertIn('Исправить программу', out.getvalue())
        self.assertIn('Программа обновлена', out.getvalue())
        self.assertEqual(self.git(self.clone, 'rev-parse', 'HEAD'), self.git(self.writer, 'rev-parse', 'HEAD'))


class GitFailureReasonTest(TempDirCase):
    """Сбой git показывает шаг и вид причины, а не одну общую фразу; URL с токеном наружу не уходит."""

    def repo(self):
        path = Path(self.tmp) / 'repo'
        path.mkdir()
        subprocess.run(['git', 'init', '-q', str(path)], check=True)
        return path

    @async_test
    async def test_missing_remote_ref_is_named_with_step(self):
        from bender.updates.git import command
        repo = self.repo()
        remote = Path(self.tmp) / 'remote'
        subprocess.run(['git', 'init', '-q', '--bare', str(remote)], check=True)
        with self.assertRaises(HumanError) as caught:
            await command(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(repo), 'fetch', '--quiet', '--', str(remote),
                           '+refs/heads/nope:refs/remotes/origin/nope'], repo)
        self.assertIn('шаг «fetch»', caught.exception.title)
        self.assertIn('Ветка не найдена', caught.exception.title)

    def test_step_name_skips_config_options(self):
        from bender.updates.git import git_step
        self.assertEqual(git_step(['git', '-c', 'core.hooksPath=/dev/null', '-C', '/x', 'rev-parse', 'HEAD']), 'rev-parse')
        self.assertEqual(git_step(['git', 'status']), 'git')

    def test_failure_table_maps_network_and_auth(self):
        from bender.updates.git import FAILURES
        def reason(text):
            low = text.lower()
            for needles, title, _hint in FAILURES:
                if any(s in low for s in needles): return title
            return None
        self.assertEqual(reason("fatal: unable to access 'https://github.com/x/y/': Could not resolve host: github.com"), 'Нет связи с GitHub')
        self.assertEqual(reason("remote: Invalid username or password. fatal: Authentication failed"), 'GitHub не пустил к приватному репозиторию')
        self.assertEqual(reason('fatal: unable to access: The requested URL returned error: 403'), 'GitHub не пустил к приватному репозиторию')
        self.assertEqual(reason('error: index.lock exists'), 'Нет прав на папку программы')
        self.assertIsNone(reason('something unexpected'))
