"""Обычное обновление клона по решению хозяина; git использует его вход и окружение."""
from __future__ import annotations

DAY = 86400


def error(title, hint='Повтори проверку в «Версиях и откате».'):
    from ..core.errors import HumanError
    return HumanError('git_update', title, hint, status=409)


def environment():
    import os
    from pathlib import Path
    env = dict(os.environ)
    # systemd может не наследовать PATH интерактивной оболочки с gh.
    paths = env.get('PATH', '').split(os.pathsep)
    for folder in (str(Path.home() / '.local/bin'), '/usr/local/bin', '/opt/homebrew/bin'):
        if folder not in paths: paths.append(folder)
    env.update(PATH=os.pathsep.join(paths), GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='never')
    return env


PIPE_GRACE = 3  # сколько дочитывать вывод после выхода процесса


async def command(argv, cwd, *, allow_failure=False, timeout=120):
    """Ждём выхода самого процесса, а не закрытия его труб. Потомок в своей сессии (мастер ssh
    с ControlPersist, помощник входа) может держать вывод открытым часами — git при этом уже закончил."""
    import asyncio
    import os
    import signal
    out, err = bytearray(), bytearray()

    async def drain(stream, sink):
        while True:
            chunk = await stream.read(65536)
            if not chunk: return
            sink.extend(chunk)

    try:
        proc = await asyncio.create_subprocess_exec(*argv, cwd=str(cwd), env=environment(), stdin=asyncio.subprocess.DEVNULL,
                                                     stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                                                     **({'start_new_session': True} if os.name == 'posix' else {}))
        readers = [asyncio.ensure_future(drain(proc.stdout, out)), asyncio.ensure_future(drain(proc.stderr, err))]
        try:
            await asyncio.wait_for(proc.wait(), timeout)
            await asyncio.wait(readers, timeout=PIPE_GRACE)
        finally:
            if proc.returncode is None:
                try:
                    if os.name == 'posix': os.killpg(proc.pid, signal.SIGKILL)
                    else: proc.kill()
                except ProcessLookupError: pass
                try: await asyncio.wait_for(proc.wait(), 5)
                except asyncio.TimeoutError: pass
            for reader in readers: reader.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            transport = getattr(proc, '_transport', None)
            if transport is not None: transport.close()  # трубы, которые держит выживший потомок
    except (OSError, asyncio.TimeoutError):
        raise error('Не удалось выполнить проверку программы', 'Проверь наличие git/Python и доступ к серверу; повтори позже.') from None
    out, err = bytes(out), bytes(err)
    text = out.decode('utf-8', 'replace')
    if proc.returncode and not allow_failure:
        # Вывод git/помощника может содержать URL с токеном: наружу только фиксированные причины и имя шага.
        failure = err.decode('utf-8', 'replace').lower()
        step = git_step(argv)
        for needles, title, hint in FAILURES:
            if any(s in failure for s in needles):
                raise error(title + ' (шаг «' + step + '»)', hint)
        raise error('Git не смог проверить или обновить ветку (шаг «' + step + '»)', 'Проверь интернет, права на папку и выбранную ветку. Вход в приватный GitHub: gh auth login и gh auth setup-git.')
    return proc.returncode, text


# Причины сбоя git: (фрагменты stderr в нижнем регистре, заголовок, подсказка). Порядок важен: вход проверяется первым.
FAILURES = (
    (('401', '403', 'repository not found', 'could not read username', 'authentication failed', 'could not read password'),
     'GitHub не пустил к приватному репозиторию', 'Выполни gh auth login (или gh auth setup-git) и повтори.'),
    (('permission denied (publickey)', 'host key verification failed', 'could not read from remote repository'),
     'Сервер не принял ключ SSH', 'Настрой ключ SSH для GitHub или переведи клон на https и выполни gh auth setup-git.'),
    (('could not resolve host', 'failed to connect', 'connection timed out', 'operation timed out', 'network is unreachable',
      'connection refused', 'unable to access', 'ssl', 'tls'),
     'Нет связи с GitHub', 'Проверь интернет, прокси и доступ к github.com; повтори позже.'),
    (("couldn't find remote ref", 'not a valid ref', 'unknown revision', 'invalid refspec', 'bad revision', 'no such ref'),
     'Ветка не найдена на сервере', 'Проверь, что ветка клона отслеживает существующую ветку origin, и повтори.'),
    (('index.lock', 'could not lock', 'unable to create', 'permission denied', 'read-only file system'),
     'Нет прав на папку программы', 'Проверь права на папку программы и занятость файлов; закрой другие программы с этой папкой и повтори.'),
    (('not a git repository',),
     'Папка программы не похожа на клон git', 'Установи программу заново из клона git или выбери правильную папку.'),
)


def git_step(argv):
    """Название git-команды из argv (после -C <папка> и -c настроек)."""
    try:
        i = argv.index('-C') + 2
    except ValueError:
        return 'git'
    while i < len(argv) and argv[i].startswith('-'):
        i += 1
    return argv[i] if i < len(argv) else 'git'


class GitUpdate:
    def __init__(self, app, root=None):
        from ..core.paths import PROGRAM_DIR
        import time
        self.app = app
        self.root = root or PROGRAM_DIR
        self.next_at = max(time.time() + 60, float(app.store.meta_get('updates:checked', time.time())) + DAY)
        self.lock = None
        self.step = None  # текущий шаг проверки или установки — пульт показывает, где он сейчас

    def mark(self, title):
        import time
        now = time.time()
        self.step = {'title': title, 'at': now, 'started': (self.step or {}).get('started', now)}

    def progress(self):
        """Где сейчас обновление и чем кончилось прошлое; переживает перезагрузку страницы и перезапуск."""
        import time
        step = None
        if self.step:
            step = {'title': self.step['title'], 'seconds': int(time.time() - self.step['at']), 'total': int(time.time() - self.step['started'])}
            backup = getattr(self.app.backups, 'progress', None)
            if self.step['title'].startswith('Бекап') and backup:
                step['backup'] = {k: backup.get(k) for k in ('files', 'total', 'read', 'folder', 'first')}
        return {'step': step, 'last': self.app.store.meta_get('updates:last')}

    def finish(self, ok, message):
        import time
        self.step = None
        self.app.store.meta_set('updates:last', {'ok': ok, 'message': message, 'at': time.time()})

    def enabled(self):
        if self.app.safe or not self.app.settings.get('updates', 'enabled', True):
            raise error('Обновления выключены', 'Выйди из безопасного режима или включи обновления в настройках.')
        if not (self.root / '.git').exists():
            raise error('Эта программа установлена из архива', 'Скачай свежий ZIP ветки с GitHub, распакуй в новую папку и запусти с --data прежней папки данных. Старую папку не удаляй до проверки.')

    async def git(self, *args, allow_failure=False):
        return await command(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(self.root), *args], self.root, allow_failure=allow_failure)

    async def clean(self, incoming=()):
        """Мешают: правки отслеживаемых файлов, новые файлы в коде программы (bender/) и чужой файл там,
        куда обновление положит свой. Прочий посторонний файл в корне (nohup.out, заметки) — не мешает."""
        _, status = await self.git('status', '--porcelain', '-z', '--untracked-files=all')
        incoming = set(incoming)
        blocking = []
        entries = iter(status.split('\0'))
        for entry in entries:
            if len(entry) < 4:
                continue
            code, name = entry[:2], entry[3:]
            if code[0] in 'RC':
                next(entries, None)  # у переименования следом идёт прежнее имя
            # .py/.pth в корне подхватит Python службы (cwd в sys.path) — такой «мусор» тоже мешает.
            if code != '??' or name.startswith('bender/') or name.endswith(('.py', '.pth')) or name in incoming:
                blocking.append(name)
        if blocking:
            shown = ', '.join(blocking[:5]) + (f' и ещё {len(blocking) - 5}' if len(blocking) > 5 else '')
            raise error('В папке программы есть локальные правки: ' + shown, 'Перенеси эти файлы из папки программы, сохрани правки отдельно или закончи их коммитом; обновление ничего не меняло.')

    async def check(self):
        from ..tools.registry import ToolContext
        self.enabled()
        await ToolContext(self.app, actor='owner').check('update.check', str(self.root), 'проверить свою ветку git с входом хозяина')
        self.mark('Проверяю папку программы')
        try:
            return await self._check()
        finally:
            if self.step and self.step['title'] in ('Проверяю папку программы', 'Получаю коммиты с сервера'): self.step = None

    async def _check(self):
        await self.clean()
        _, branch = await self.git('symbolic-ref', '--quiet', '--short', 'HEAD', allow_failure=True)
        branch = branch.strip()
        if not branch: raise error('Программа стоит на отдельном коммите', 'Выбери обычную ветку клона, затем повтори обновление.')
        _, remote = await self.git('config', '--get', 'branch.' + branch + '.remote', allow_failure=True)
        _, ref = await self.git('config', '--get', 'branch.' + branch + '.merge', allow_failure=True)
        remote = remote.strip() or 'origin'; ref = ref.strip() or 'refs/heads/' + branch
        if remote == '.' or not ref.startswith('refs/heads/'):
            raise error('Для ветки не задан сервер обновлений', 'Используй клон с отслеживаемой веткой origin.')
        await self.git('check-ref-format', ref)
        tracking = 'refs/remotes/' + remote + '/' + ref[len('refs/heads/'):]
        self.mark('Получаю коммиты с сервера')
        await self.git('fetch', '--quiet', '--force', '--', remote, '+' + ref + ':' + tracking)  # отслеживающая ветка наша: пересобранная история сервера не должна давать отказ fetch
        _, head = await self.git('rev-parse', 'HEAD'); _, target = await self.git('rev-parse', tracking)
        head, target = head.strip(), target.strip()
        if (await self.git('merge-base', '--is-ancestor', head, target, allow_failure=True))[0]:
            if not (await self.git('merge-base', '--is-ancestor', target, head, allow_failure=True))[0]:
                return {'head': head, 'target': head, 'branch': branch, 'commits': [], 'available': False, 'ahead': True}
            from ..core.errors import HumanError
            raise HumanError('git_diverged', 'Локальная ветка разошлась с сервером', 'Если своих коммитов у тебя нет (например, история на сервере была пересобрана), нажми «Выровнять с сервером»: '
                             'прежний коммит сохранится в отдельной ветке, данные не тронутся. Свои коммиты автоматически не переписываются.', status=409)
        _, log = await self.git('log', '--format=%h %s', head + '..' + target)
        return {'head': head, 'target': target, 'branch': branch, 'commits': log.splitlines(), 'available': head != target}

    async def wipe_guard(self):
        """Чистая переустановка стирает всё, что не в git и не в .gitignore. Папка данных внутри программы должна быть в .gitignore,
        иначе её не тронуть нельзя; ключи и агенты лежат в данных. Возвращает список того, что будет стёрто (для журнала)."""
        from ..core.pathutil import within
        data = self.app.dirs.root.resolve()
        if data == self.root.resolve() or within(data, self.root.resolve()):
            rel = str(data.relative_to(self.root.resolve()))
            if (await self.git('check-ignore', '-q', '--', rel + '/x', allow_failure=True))[0]:
                raise error('Папка данных внутри программы не защищена от очистки', 'Добавь «/' + rel + '/» в .gitignore клона или запусти Бендера с --data вне папки программы; ничего не тронуто.')
        _, status = await self.git('status', '--porcelain', '-z', '--untracked-files=all')
        names = []
        entries = iter(status.split('\0'))
        for entry in entries:
            if len(entry) < 4: continue
            if entry[0] in 'RC': next(entries, None)
            names.append(entry[3:])
        return names

    async def realign(self, *, skip_backup=False, clean=False):
        """Восстановление после расхождения (сервер пересобрал историю): ветка клона приводится к серверной.
        Прежний коммит остаётся в ветке bender-before-realign-…, перед сбросом — бекап, после — самопроверка с возвратом при неудаче.
        Незакоммиченные правки в папке программы по-прежнему мешают: их не трогаем.
        clean=True — «чистая переустановка»: правки и лишние файлы программы стираются (после бекапа), а данные
        (папка данных, ключи, агенты — всё в .gitignore) остаются. Это же лечит рассинхрон перевода строк на Windows."""
        import time
        from ..core.errors import HumanError
        from ..tools.registry import ToolContext
        self.enabled()
        await ToolContext(self.app, actor='owner').check('update.install', str(self.root), 'выровнять клон программы с сервером после расхождения истории')
        dropped = await self.wipe_guard() if clean else await self.clean() or []
        _, branch = await self.git('symbolic-ref', '--quiet', '--short', 'HEAD', allow_failure=True)
        branch = branch.strip()
        if not branch: raise error('Программа стоит на отдельном коммите', 'Выбери обычную ветку клона.')
        _, remote = await self.git('config', '--get', 'branch.' + branch + '.remote', allow_failure=True)
        _, ref = await self.git('config', '--get', 'branch.' + branch + '.merge', allow_failure=True)
        remote = remote.strip() or 'origin'; ref = ref.strip() or 'refs/heads/' + branch
        if remote == '.' or not ref.startswith('refs/heads/'): raise error('Для ветки не задан сервер обновлений')
        await self.git('check-ref-format', ref)
        tracking = 'refs/remotes/' + remote + '/' + ref[len('refs/heads/'):]
        self.mark('Получаю коммиты с сервера')
        try:
            await self.git('fetch', '--quiet', '--force', '--', remote, '+' + ref + ':' + tracking)
            _, head = await self.git('rev-parse', 'HEAD'); _, target = await self.git('rev-parse', tracking)
            head, target = head.strip(), target.strip()
            if head == target and not (clean and dropped):
                self.finish(True, 'Клон уже совпадает с сервером.')
                return {'realigned': False, 'message': 'Клон уже совпадает с сервером.'}
            saved = 'bender-before-realign-' + str(int(time.time()))
            await self.git('branch', saved, head)
            if self.skip_backup(skip_backup):
                backup = {'skipped': True, 'path': ''}
            else:
                self.mark('Бекап данных перед выравниванием')
                backup = await self.safe_backup()
            self.mark('Выравниваю с сервером')
            await self.git('reset', '--hard', target)
            if clean: await self.git('clean', '-fd')  # без -x: игнорируемое (данные, ключи, .env) не трогаем
            self.mark('Самопроверка')
            try: healthy = await self.doctor()
            except Exception: healthy = False
            if not healthy:
                await self.git('reset', '--hard', head)
                reasons = getattr(self, 'doctor_reasons', None) or []
                raise error('Самопроверка после выравнивания не прошла — возвращён прежний коммит', ('Что не прошло: ' + '; '.join(reasons) + '. ' if reasons else '') + 'Данные не тронуты.')
            self.app.store.meta_set('updates:available', None)
            self.app.journal.write('update', 'Клон выровнен с сервером после расхождения истории' + (' (чистая переустановка)' if clean else ''), actor='owner', data={'from': head, 'to': target, 'saved_branch': saved, 'wiped': dropped[:50], 'wiped_count': len(dropped)})
            self.finish(True, 'Клон выровнен с сервером. Прежний коммит сохранён в ветке ' + saved + '.')
            return {'realigned': True, 'head': target, 'saved_branch': saved, 'backup': backup, 'message': 'Клон выровнен с сервером. Прежний коммит сохранён в ветке ' + saved + '.'}
        except HumanError as exc:
            self.finish(False, exc.title + '. ' + exc.hint)
            raise
        finally:
            self.step = None

    async def backup(self):
        from ..tools.registry import ToolContext
        import asyncio
        import hashlib
        from pathlib import Path
        backups = self.app.backups
        if backups.roots():
            result = await backups.now()
            return {**result, 'path': str(backups.roots()[0])}
        return await self.reserve_backup()

    async def reserve_backup(self):
        from ..tools.registry import ToolContext
        import asyncio
        import hashlib
        from pathlib import Path
        backups = self.app.backups
        backups.require()
        # Тот же формат/состав бекапа, без изменения настроек и вне дерева программы.
        suffix = hashlib.sha256(str(self.app.dirs.root).encode()).hexdigest()[:16]
        root = backups.destination(str(Path.home() / '.local/share/bender/update-backups' / suffix))
        await ToolContext(self.app, actor='owner').check('backup.write', str(root), 'сохранить обычный бекап перед обновлением')
        async with backups.lock:
            # только чтобы откатить обновление: последних снимков хватает, расписание дней/недель — для настоящих бекапов
            result = await asyncio.to_thread(backups._snapshot, [root], None, UPDATE_SNAPSHOTS)
            return {**result, 'path': str(root)}

    async def safe_backup(self):
        """Бекап перед обновлением со страховкой: основные папки не дались — снимок в запасную папку обновлений.
        Не вышло и там — понятная остановка с подсказкой про «без бекапа»; ничего не установлено."""
        from ..core.errors import HumanError
        try:
            return await self.backup()
        except (HumanError, OSError) as first:
            if isinstance(first, HumanError) and not first.code.startswith('backup'):
                raise  # отказ хозяина в карточке и подобное — не сбой носителя
            why = first.title if isinstance(first, HumanError) else str(first)[:120]
            try:
                result = await self.reserve_backup()
            except (HumanError, OSError) as second:
                again = second.title if isinstance(second, HumanError) else str(second)[:120]
                raise HumanError('update_backup', 'Бекап перед обновлением не получился: ' + why, 'Запасная папка тоже не приняла снимок (' + again + '). Ничего не установлено. Почини носитель '
                                 'или «Починить бекапы» в разделе Бекапы; если бекап сейчас не нужен — включи «Следующее обновление — без бекапа» (Настройки → Версии, в Telegram: /update и кнопка «без бекапа») и повтори.',
                                 detail=str(first)) from None
            self.app.journal.write('update', 'Основной бекап не получился (' + why + '), перед обновлением снимок сохранён в запасную папку', actor='system', data={'path': result.get('path')})
            return {**result, 'note': 'Основной бекап не получился (' + why + '); снимок лежит в запасной папке.'}

    def skip_backup(self, explicit=False):
        """Хозяин велел обновиться без бекапа один раз: выключатель гаснет сам, как только его учли."""
        if explicit:
            return True
        if self.app.settings.get('updates', 'skip_backup_once', False) is True:
            self.app.settings.set('updates', {'skip_backup_once': False})
            return True
        return False

    async def doctor(self):
        import json
        import sys
        self.doctor_reasons = []
        code, text = await command([sys.executable, '-m', 'bender', '--data', str(self.app.dirs.root), 'doctor', '--json'], self.root, allow_failure=True, timeout=60)
        try: result = json.loads(text)
        except (ValueError, TypeError):
            self.doctor_reasons = ['новая версия не выдала отчёт самопроверки' + (' (код ' + str(code) + ')' if code else '')]
            return False
        if isinstance(result, dict):
            self.doctor_reasons = [str(row.get('title') or row.get('id')) for row in result.get('rows') or [] if isinstance(row, dict) and row.get('level') == 'error'][:5]
        return code == 0 and isinstance(result, dict) and result.get('ok') is True

    async def install(self, plan, *, nightly=False, skip_backup=False):
        import asyncio
        import os
        import re
        from ..core.errors import HumanError
        from ..core.pathutil import within
        from ..tools.registry import ToolContext
        self.enabled()
        await ToolContext(self.app, actor='owner').check('update.install', str(self.root), 'обновить программу' + (' по ночному решению хозяина' if nightly else ' по подтверждению хозяина'))
        if not all(re.fullmatch('[0-9a-f]{40,64}', str(plan.get(k, ''))) for k in ('head', 'target')):
            raise error('Сначала просмотри новые коммиты')
        _, gitdir = await self.git('rev-parse', '--git-dir')
        lockfile = (self.root / gitdir.strip()).resolve() / 'bender-update.lock'
        try: fd = os.open(lockfile, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            raise error('Другое обновление уже выполняется', 'Дождись его завершения. После аварийной остановки проверь процесс перед удалением bender-update.lock в служебной папке git.') from None
        except OSError:
            raise error('Нельзя записать служебную блокировку обновления', 'Проверь права и свободное место в папке программы.') from None
        self.step = None
        self.mark('Проверяю папку программы')
        try:
            with os.fdopen(fd, 'w') as stream: stream.write(str(os.getpid()))
            await self.clean()
            _, head = await self.git('rev-parse', 'HEAD')
            _, branch = await self.git('symbolic-ref', '--quiet', '--short', 'HEAD', allow_failure=True)
            if head.strip() != plan['head'] or branch.strip() != plan.get('branch'):
                raise error('Программа изменилась после просмотра', 'Ещё раз проверь новые коммиты и подтверди их.')
            if not plan.get('available'): return {'updated': False, 'message': 'Установлена последняя версия.'}
            if (await self.git('merge-base', '--is-ancestor', plan['head'], plan['target'], allow_failure=True))[0]:
                raise error('Ветка разошлась после просмотра')
            _, tree = await self.git('ls-tree', '-rz', '--name-only', plan['target'])
            if any(within(self.root / name, self.app.dirs.root) for name in tree.split('\0') if name):
                raise error('Обновление содержит файлы в папке данных', 'Данные не менялись. Попроси разработчика убрать их из программы.')
            incoming = [name for name in tree.split('\0') if name]
            await self.clean(incoming)
            if self.skip_backup(skip_backup):
                self.mark('Обновление без бекапа (по слову хозяина)')
                backup = {'skipped': True, 'path': ''}
                self.app.journal.write('update', 'Обновление без бекапа: хозяин отключил его на один раз', actor='owner', data={'target': plan['target']})
            else:
                self.mark('Бекап данных перед обновлением')
                backup = await self.safe_backup()
            await self.clean(incoming)
            if (await self.git('rev-parse', 'HEAD'))[1].strip() != plan['head']:
                raise error('Программа изменилась во время бекапа', 'Ничего не установлено; проверь коммиты ещё раз.')
            self.app.store.meta_set('updates.in_progress', {'head': plan['head'], 'target': plan['target'], 'git': True})
            try:
                self.mark('Ставлю новые коммиты')
                await self.git('merge', '--ff-only', '--no-edit', plan['target'])
                self.mark('Самопроверка новой версии')
                try: healthy = await self.doctor()
                except Exception: healthy = False
                if not healthy:
                    _, current = await self.git('rev-parse', 'HEAD')
                    if current.strip() != plan['target']:
                        raise error('Самопроверка не прошла, но ветку уже изменил другой процесс', 'Не трогаю новые правки. Верни прежний коммит вручную после их сохранения.')
                    try: await self.git('reset', '--keep', plan['head'])
                    except HumanError:
                        raise error('Самопроверка не прошла, автоматический откат не удался', 'Не перезапускай программу. Сохрани появившиеся локальные правки и верни прежний коммит ' + plan['head'] + '. Данные не откатывай.') from None
                    reasons = getattr(self, 'doctor_reasons', None) or []
                    self.app.journal.write('update', 'Самопроверка новой версии не прошла, возвращена прежняя', actor='system', data={'target': plan['target'], 'reasons': reasons})
                    raise error('Новая версия не прошла самопроверку — возвращена прежняя', ('Что не прошло: ' + '; '.join(reasons) + '. ' if reasons else '') + 'Данные' + (' и бекап' if not backup.get('skipped') else '') + ' сохранены; пришли разработчику bender report и описание обновления.')
                self.app.journal.write('update', 'Установлено обычное обновление git', actor='owner', data={'from': plan['head'], 'to': plan['target'], 'night': nightly})
                self.finish(True, 'Программа обновлена. ' + ('Без бекапа (по твоему слову).' if backup.get('skipped') else 'Бекап: ' + str(backup.get('path', '')) + (' ' + backup['note'] if backup.get('note') else '') + heavy_note(backup)))
                return {'updated': True, 'head': plan['target'], 'backup': backup, 'message': 'Программа обновлена.'}
            finally: self.app.store.meta_set('updates.in_progress', None)
        except HumanError as exc:
            self.finish(False, exc.title + '. ' + exc.hint)
            raise
        except BaseException:
            self.finish(False, 'Обновление прервано; смотри Бортжурнал.')
            raise
        finally:
            self.step = None
            try: lockfile.unlink()
            except FileNotFoundError: pass

    def due(self):
        if self.app.safe or not self.app.settings.get('updates', 'enabled', True) or self.app.settings.get('updates', 'auto', 'notify') == 'off' or not (self.root / '.git').exists(): return None
        return self.next_at

    async def tick(self, *, sleep_event=False):
        import time
        from ..core.errors import HumanError
        mode = self.app.settings.get('updates', 'auto', 'notify')
        if self.due() is None: return
        pending = self.app.store.meta_get('updates:available')
        if not sleep_event and time.time() < self.next_at: return
        if sleep_event and mode != 'night': return
        if sleep_event and not pending and self.app.store.meta_get('updates:checked') is not None and time.time() < self.next_at: return
        self.next_at = time.time() + DAY
        try:
            plan = await self.check()
            self.app.store.meta_set('updates:checked', time.time())
            self.app.store.meta_set('updates:available', plan if plan['available'] else None)
            if plan['available'] and sleep_event and mode == 'night':
                if self.app.mind.turns or self.app.team.running:
                    return  # не перезапускаем чужой активный ход
                result = await self.install(plan, nightly=True)
                self.app.store.meta_set('updates:available', None)
                self.app.store.meta_set('updates:morning', result['message'] + (' Без бекапа.' if result['backup'].get('skipped') else ' Бекап: ' + result['backup']['path']))
                await self.restart()
            elif plan['available'] and (not pending or pending.get('target') != plan['target']):
                self.app.bus.publish({'type': 'notify', 'kind': 'update', 'text': 'Вышло обновление Бендера. Открой «Версии и откат» → «Обновить программу».', 'update': True})
        except HumanError as exc:
            text = exc.title + '. ' + exc.hint
            self.app.store.meta_set('updates:morning', text)
            if not sleep_event:
                self.app.bus.publish({'type': 'notify', 'kind': 'update', 'text': text})

    async def restart(self):
        import asyncio
        from pathlib import Path
        from ..core.errors import HumanError
        from ..net.http_client import Timeouts
        if getattr(self.app, 'watched', False) and hasattr(self.app, 'request_restart'):
            asyncio.get_running_loop().call_later(.3, self.app.request_restart)
            return 'Бендер перезапускается через Сторожа.'
        # CLI находит только свой работающий пульт по id общей сессии данных.
        port = self.app.store.meta_get('web:active_port') or self.app.settings.get('web', 'port', 8770)
        try:
            from ..core.web_auth import headers
            auth=headers(self.app.dirs.root)
            response = await self.app.http.get(f'http://127.0.0.1:{port}/api/state', headers=auth, timeouts=Timeouts(connect=2, read=3, total=5))
            async with response: state = await response.json()
            if state.get('main_session') == self.app.main_session() and state.get('boot', {}).get('watched'):
                response = await self.app.http.post(f'http://127.0.0.1:{port}/api/restart', json={'mode': 'normal'}, headers=auth, timeouts=Timeouts(connect=2, read=3, total=5))
                async with response:
                    if response.ok: return 'Бендер перезапускается через Сторожа.'
        except Exception: pass
        try:
            import shlex
            from ..core.pathutil import same
            unit = Path.home() / '.config/systemd/user/bender.service'
            settings = {}
            for line in unit.read_text().splitlines():
                if line.startswith('Environment='):
                    for entry in shlex.split(line[len('Environment='):]):
                        key, sep, value = entry.partition('=')
                        if sep: settings[key] = value.replace('%%', '%')
            if settings.get('BENDER_PROGRAM_DIR') and settings.get('BENDER_SERVICE_DATA') and same(settings['BENDER_PROGRAM_DIR'], self.root) and same(settings['BENDER_SERVICE_DATA'], self.app.dirs.root):
                code, _ = await command(['systemctl', '--user', 'restart', 'bender'], self.root, allow_failure=True, timeout=30)
                if code == 0: return 'Бендер перезапускается службой пользователя.'
        except (OSError, ValueError, HumanError): pass
        return 'Перезапусти Бендера.'


UPDATE_SNAPSHOTS = 3  # снимков в запасной папке бекапов обновлений


def heavy_note(backup):
    """Что больше всего читал бекап — чтобы хозяин видел, где тяжёлое."""
    items = [item for item in backup.get('heavy') or [] if item.get('bytes', 0) >= 50 * 1024 * 1024]
    skipped = f". Не копировалось (получается заново): папок {backup['skipped']}" if backup.get('skipped') else ''
    if not items:
        return skipped
    return '. Больше всего прочитано: ' + ', '.join(f"{item['folder']} — {item['bytes'] // (1024 * 1024)} МБ" for item in items[:3]) + skipped
