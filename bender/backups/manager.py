"""Снимки data/: SHA-256, sqlite backup, проверка и восстановление в отдельный слот."""
from __future__ import annotations

import asyncio
from contextlib import closing
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import random
import re
import shutil
import sqlite3
import tempfile
import time

from ..core.errors import HumanError
from ..core.fsutil import atomic_write
from ..core.ids import new_id
from ..core.pathutil import canonical, within, relative_path
from ..tools.registry import ToolContext

HEX = re.compile(r'[0-9a-f]{64}')
# Получается заново (npm/pip, кэши) — где бы ни лежало; ещё виртуальные окружения Python (папка с pyvenv.cfg).
REBUILD_DIRS = {'node_modules', '__pycache__', '.cache', '.npm', '.pnpm-store', '.mypy_cache', '.pytest_cache', '.ruff_cache', '.tox', '.gradle'}
# Скачивается кнопкой заново: модели голоса, пакеты-кандидаты обновлений.
REBUILD_ROOTS = {'local/voice', 'local/update-candidates'}
# Копии базы перед миграциями: база и так в каждом снимке, а каждая копия — ещё одна полная база.
NEVER_ROOTS = {'local/migration-backups'}


def error(title, hint='Открой «Бекапы», проверь носитель и повтори действие.'):
    return HumanError('backup', title, hint)


def file_hash(path):
    digest = hashlib.sha256()
    try:
        with Path(path).open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
    except OSError as exc:
        raise HumanError('backup_read', 'Не удалось прочитать файл бекапа', 'Проверь носитель и наличие блока; попробуй вторую копию.', detail=str(exc)) from None
    return digest.hexdigest()


def copy_object(source, target, expected):
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.backup-', dir=target.parent)
    try:
        digest = hashlib.sha256()
        with os.fdopen(fd, 'wb') as output, Path(source).open('rb') as input_stream:
            # хэш — по ходу копирования: файл читается один раз, а не ещё раз после записи
            for chunk in iter(lambda: input_stream.read(1024 * 1024), b''):
                digest.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest.hexdigest() != expected:
            raise error('Файл изменился во время копирования', 'Повтори снимок после завершения записи файла.')
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def portable_alias(path):
    # Опись переносима: не обещаем восстановление двух имён одного файла на другом томе.
    if not path.isascii():
        import unicodedata
        path = unicodedata.normalize('NFC', path)
    return path.casefold()


def read_json(path):
    try:
        if path.is_symlink() or path.stat().st_size > 32 * 1024 * 1024:
            raise error('Недопустимая опись бекапа')
        value = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise error('В описи бекапа нет объекта')
        return value
    except (OSError, ValueError, UnicodeError) as exc:
        raise HumanError('backup_manifest', 'Не удалось прочитать опись бекапа', 'Проверь носитель и выбери целую копию снимка.', detail=str(exc)) from None


class Backups:
    def __init__(self, app):
        self.app = app
        self.lock = asyncio.Lock()
        self.progress = None  # счётчики идущего снимка для пульта
        self.next_at = None

    def config(self):
        return self.app.settings.group('backups')

    def destination(self, value):
        path = Path(value).expanduser() / 'bender-backups'
        if path.is_symlink():
            raise error('Хранилище бекапов не должно быть ссылкой')
        root = canonical(path)
        if within(root, self.app.dirs.root) or within(self.app.dirs.root, root):
            raise error('Хранилище пересекается с живыми данными', 'Выбери другую папку или внешний диск.')
        for name in ('objects', 'snapshots'):
            if (root / name).is_symlink():
                raise error('В хранилище найдена ссылка: ' + name)
        return root

    def roots(self):
        cfg = self.config()
        return list(dict.fromkeys(self.destination(p) for p in (cfg['path'], cfg['second_path']) if p.strip()))

    def require(self):
        if not self.config()['enabled'] or self.app.safe:
            raise error('Бекапы выключены', 'Включи их в настройках; безопасный режим ничего не записывает.')

    def snapshots(self, root):
        if not (root / 'snapshots').is_dir():
            return []
        return sorted((p.stem for p in (root / 'snapshots').glob('s-*.json')), reverse=True)

    def manifest(self, root, ident):
        if not re.fullmatch(r's-[a-zA-Z0-9-]+', ident or ''):
            raise error('Некорректный номер снимка')
        value = read_json(root / 'snapshots' / (ident + '.json'))
        if value.get('format') not in (0, 1):
            raise error('Формат снимка новее этой программы', 'Обнови Бендера; данные не изменены.')
        files = value.get('files')
        if not isinstance(files, dict) or len(files) > 100000:
            raise error('Повреждена опись снимка')
        aliases = set()
        for rel, obj in files.items():
            relative_path(rel)
            alias = portable_alias(rel)
            if alias in aliases or not isinstance(obj, dict) or not HEX.fullmatch(obj.get('hash', '')):
                raise error('В описи повторяющийся путь или неверный хеш')
            aliases.add(alias)
        return value

    def block(self, root, digest):
        if not HEX.fullmatch(digest):
            raise error('Некорректный хеш блока')
        path = root / 'objects' / digest
        if path.is_symlink() or not within(path, root / 'objects'):
            raise error('Блок заменён ссылкой')
        return path

    def status(self):
        return {'settings': self.config(), 'destinations': [{'path': str(root), 'snapshots': [{**{k: v for k, v in self.manifest(root, i).items() if k != 'files'}, 'files_count': len(self.manifest(root, i)['files'])} for i in self.snapshots(root)]} for root in self.roots()],
                'state': self.app.store.meta_get('backups.state', {}), 'warning': 'Бекапы не шифруются. Ключи включаются отдельно; другие файлы тоже могут содержать личные данные.',
                'termux': 'В Termux выбери папку вне приложения (например, ~/storage/shared/BenderBackup после termux-setup-storage) и второй носитель. Удаление Termux уничтожает внутренние данные.'}

    async def now(self, *, scheduled=False):
        self.require()
        roots = self.roots()
        if not roots:
            raise error('Не выбрана папка бекапов', 'Укажи первый путь в «Настройки → Бекапы».')
        ctx = ToolContext(self.app, actor='owner' if not scheduled else 'heart', unattended=scheduled)
        await ctx.check('backup.write', str(roots[0]), 'сохранить данные в одобренные папки бекапов')
        async with self.lock:
            self.app.store.meta_set('backups.in_progress', time.time())
            healed = None
            try:
                try:
                    result = await asyncio.to_thread(self._snapshot, roots)
                except HumanError as exc:
                    if exc.code != 'backup_read':
                        raise
                    # Встроенная починка: целые блоки берутся со второй копии, битые убираются, снимок повторяется один раз.
                    healed = await asyncio.to_thread(self._repair)
                    self._note_repair(healed, ctx.actor, auto=True)
                    result = await asyncio.to_thread(self._snapshot, roots)
            finally:
                self.app.store.meta_set('backups.in_progress', None)
            if healed:
                result['healed'] = healed
            state = self.app.store.meta_get('backups.state', {})
            state['last_backup'] = time.time()
            self.app.store.meta_set('backups.state', state)
            self.app.journal.write('backup', 'Сохранён снимок данных', actor=ctx.actor, data=result)
            self.app.bus.publish({'type': 'backup', **result})
            return result

    def _note_repair(self, result, actor, auto=False):
        state = self.app.store.meta_get('backups.state', {})
        state['repair'] = {**result, 'at': time.time(), 'auto': auto}
        self.app.store.meta_set('backups.state', state)
        self.app.journal.write('backup', ('Встроенная починка бекапа' if auto else 'Починка бекапа') + f": восстановлено блоков {result['fixed']}, потеряно {result['lost']}", actor=actor, data=result)

    @staticmethod
    def _intact(block, digest):
        try:
            return block.is_file() and file_hash(block) == digest
        except HumanError:
            return False

    def _repair(self):
        """Сверить копии: недостающие и битые блоки и описи берутся из другой копии, где они целы.
        Блок, которого нет нигде целым, убирается (следующий снимок запишет файл заново, если он ещё на месте)."""
        roots = self.roots()
        fixed = manifests = 0
        lost_blocks, damaged, notes = set(), [], []
        for root in roots:
            for name in ('objects', 'snapshots'):
                try:
                    (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
                except OSError as exc:
                    notes.append(f'{root}: {exc}')
        # описи: снимок, которого нет на одном носителе, а на другом цел, копируется целиком
        for root in roots:
            for other in roots:
                if other == root:
                    continue
                for ident in self.snapshots(other):
                    target = root / 'snapshots' / (ident + '.json')
                    if target.exists():
                        continue
                    try:
                        self.manifest(other, ident)
                        atomic_write(target, (other / 'snapshots' / (ident + '.json')).read_bytes())
                        manifests += 1
                    except (OSError, HumanError):
                        continue
        for root in roots:
            wanted = {}
            for ident in self.snapshots(root):
                try:
                    for obj in self.manifest(root, ident)['files'].values():
                        wanted.setdefault(obj['hash'], []).append(ident)
                except HumanError:
                    damaged.append(f'{root.name}/{ident}: опись не читается')
            for digest, idents in wanted.items():
                block = self.block(root, digest)
                if self._intact(block, digest):
                    continue
                donor = next((self.block(o, digest) for o in roots if o != root and self._intact(self.block(o, digest), digest)), None)
                if donor is not None:
                    try:
                        copy_object(donor, block, digest)
                        fixed += 1
                        continue
                    except (OSError, HumanError):
                        pass
                lost_blocks.add(digest)
                damaged.extend(i for i in idents if i not in damaged)
                try:
                    if block.exists() and not block.is_symlink():
                        block.unlink()
                except OSError:
                    pass
        return {'fixed': fixed, 'lost': len(lost_blocks), 'manifests': manifests, 'damaged': damaged[:50], 'notes': notes[:10]}

    async def repair(self, *, snapshot=True):
        """Починить бекапы: сверить копии, подтянуть целые блоки и описи, затем сохранить свежий снимок."""
        self.require()
        roots = self.roots()
        if not roots:
            raise error('Не выбрана папка бекапов', 'Укажи первый путь в «Настройки → Бекапы».')
        ctx = ToolContext(self.app, actor='owner')
        await ctx.check('backup.write', str(roots[0]), 'починить бекап: взять целые блоки из другой копии')
        async with self.lock:
            result = await asyncio.to_thread(self._repair)
            self._note_repair(result, 'owner')
        if snapshot:
            try:
                result['snapshot'] = await self.now()
            except HumanError as exc:
                result['snapshot_error'] = exc.title + '. ' + exc.hint
        self.app.bus.publish({'type': 'backup', 'repair': True, **{k: v for k, v in result.items() if k != 'snapshot'}})
        return result

    def _previous(self, root):
        """Опись последнего снимка: неизменённый файл (тот же размер и время изменения) не перечитываем."""
        for ident in self.snapshots(root)[:1]:
            try: return self.manifest(root, ident)['files']
            except HumanError: return {}
        return {}

    def _walk(self, skip):
        """Пути данных; восстанавливаемые папки не обходятся вовсе (в node_modules бывают сотни тысяч файлов)."""
        root = self.app.dirs.root
        out, skipped = [], []
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            here = Path(dirpath)
            keep = []
            for name in dirnames:
                path = here / name
                rel = path.relative_to(root).as_posix()
                if rel in NEVER_ROOTS or skip and not path.is_symlink() and (name in REBUILD_DIRS or rel in REBUILD_ROOTS or (path / 'pyvenv.cfg').is_file()):
                    skipped.append(rel)
                    continue
                keep.append(name)
                out.append(path)
            dirnames[:] = keep
            out.extend(here / name for name in filenames)
        return sorted(out), skipped

    def _slim_db(self, dbpath):
        """Снимки запросов к моделям — диагностика за последние дни, в бекапе не нужны; без них база в разы меньше."""
        with closing(sqlite3.connect(dbpath)) as db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND (name = 'calls' OR name LIKE 'agent\\_%\\_\\_calls' ESCAPE '\\')")]
            changed = sum(db.execute(f'UPDATE "{t}" SET request = NULL WHERE request IS NOT NULL').rowcount for t in tables)
            db.commit()
            if changed:
                db.execute('VACUUM')

    def _snapshot(self, roots, preserve=None, keep=None):
        """keep — сколько последних снимков хранить вместо расписания из настроек."""
        cfg = self.config()
        ident = 's-' + new_id().lower()
        warnings, copied, size = [], 0, 0
        failed = {}  # носитель -> почему из снимка выпал
        previous = self._previous(roots[0]) if roots else {}
        for root in roots:
            for name in ('objects', 'snapshots'):
                (root / name).mkdir(parents=True, exist_ok=True, mode=0o700)
        with tempfile.TemporaryDirectory(prefix='bender-backup-') as temp:
            dbpath = Path(temp) / 'bender.db'
            self.app.store.backup_to(dbpath)
            skip = bool(cfg.get('skip_rebuildable', True))
            if skip:
                self._slim_db(dbpath)
            files, aliases = {}, set()
            paths, skipped = self._walk(skip)
            # first: прошлого снимка с отметками времени нет — читается всё; дальше только изменённое
            self.progress = {'files': 0, 'total': len(paths), 'read': 0, 'started': time.time(), 'folder': '', 'first': not previous, 'heavy': {}}
            for path in paths:
                self.progress['files'] += 1
                parts = path.relative_to(self.app.dirs.root).parts
                self.progress['folder'] = '/'.join(parts[:{'agents': 3, 'local': 2}.get(parts[0], 1)][:len(parts) - 1]) or parts[0]
                if path.is_symlink():
                    warnings.append('Пропущена ссылка: ' + str(path.relative_to(self.app.dirs.root)))
                    continue
                if not path.is_file():
                    continue
                rel = path.relative_to(self.app.dirs.root).as_posix()
                relative_path(rel)
                alias = portable_alias(rel)
                if alias in aliases or len(files) >= 100000:
                    raise error('Имена файлов неоднозначны для переносимого бекапа или их слишком много', 'Разведи имена, различающиеся лишь регистром/Unicode, или раздели набор данных. Предел — 100000 файлов.')
                aliases.add(alias)
                if rel in ('bender.db-wal', 'bender.db-shm') or (rel == 'secrets.json' and not cfg['include_secrets']):
                    continue
                if any(parent.is_symlink() for parent in path.parents):
                    raise error('Данные заменены ссылкой во время копирования')
                source = dbpath if rel == 'bender.db' else path
                if rel == 'local/journal.anchor':
                    with closing(sqlite3.connect(dbpath)) as db:
                        row = db.execute('SELECT seq, hash FROM journal ORDER BY seq DESC LIMIT 1').fetchone()
                    raw = (f'{row[0]}\t{row[1]}\n' if row else '0\t' + '0' * 64 + '\n').encode()
                    anchor = Path(temp) / 'anchor'
                    anchor.write_bytes(raw)
                    source = anchor
                try:
                    info = path.stat()
                except OSError:
                    warnings.append('Файл исчез во время снимка: ' + rel)
                    continue
                old = previous.get(rel) if source is path else None
                # Блок такого файла проверен при прошлом снимке; новые и изменённые файлы читаются целиком.
                same = bool(old) and old.get('size') == info.st_size and old.get('mtime') == info.st_mtime_ns and bool(HEX.fullmatch(str(old.get('hash', ''))))
                try:
                    digest = old['hash'] if same else file_hash(source)
                except HumanError:
                    if source is not path:
                        raise
                    warnings.append('Файл не прочитан, в снимок не попал: ' + rel)
                    continue
                count = info.st_size if source is path else source.stat().st_size
                if not same:
                    self.progress['read'] += count
                    top = self.progress['folder'] or rel
                    self.progress['heavy'][top] = self.progress['heavy'].get(top, 0) + count
                files[rel] = {'hash': digest, 'size': count, 'mode': 0o600 | (info.st_mode & 0o100) if rel != 'secrets.json' else 0o600}
                if source is path: files[rel]['mtime'] = info.st_mtime_ns
                for root in roots:
                    if root in failed:
                        continue
                    try:
                        block = self.block(root, digest)
                        if same and block.is_file() and block.stat().st_size == count:
                            continue
                        if block.exists() and file_hash(block) == digest:
                            continue
                        copy_object(source, block, digest)
                    except (OSError, HumanError) as exc:
                        if isinstance(exc, HumanError) and exc.code not in ('backup_read',):
                            raise
                        if not os.path.exists(source):
                            warnings.append('Файл исчез во время снимка: ' + rel)
                            break
                        # Сбойный носитель не отменяет снимок: пока цела хоть одна копия, пишем в неё.
                        failed[root] = getattr(exc, 'title', None) or str(exc)
                        continue
                    copied += 1
                    size += count
                    if same: self.progress['read'] += count  # новый/изменённый уже посчитан при проверке
            manifest = {'format': 1, 'id': ident, 'at': time.time(), 'files': files, 'secrets': cfg['include_secrets'], 'skipped': skipped[:1000], 'slim_db': skip,
                        'schemas': {r['key']: json.loads(r['value']) for r in self.app.store.q("SELECT key,value FROM meta WHERE key LIKE 'schema:%'")}, 'warnings': warnings}
            good = [root for root in roots if root not in failed]
            if not good:
                raise HumanError('backup_read', 'Ни один носитель бекапа не принял снимок', 'Проверь носитель и наличие блока; попробуй «Починить бекапы» в пульте или /backup repair в Telegram. ' + '; '.join(f'{r}: {why}' for r, why in failed.items()), detail=str(failed))
            warnings.extend(f'Носитель пропущен ({root}): {why}. Снимок сохранён на остальных.' for root, why in failed.items())
            manifest['warnings'] = warnings
            raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True).encode()
            if len(raw) > 32 * 1024 * 1024:
                raise error('Опись бекапа слишком велика', 'Раздели набор данных; предел описи — 32 МБ.')
            for root in good:
                atomic_write(root / 'snapshots' / (ident + '.json'), raw)
                self.rotate(root, preserve=preserve, keep=keep)
        heavy = sorted(self.progress['heavy'].items(), key=lambda item: -item[1])[:5]
        return {'id': ident, 'files': len(files), 'copied': copied, 'bytes': size, 'destinations': len(roots) - len(failed), 'warnings': warnings, 'skipped': len(skipped),
                'read': self.progress['read'], 'heavy': [{'folder': k, 'bytes': v} for k, v in heavy]}

    def rotate(self, root, preserve=None, keep=None):
        cfg = self.config()
        ids = self.snapshots(root)
        schedule = (('day', cfg['daily']), ('week', cfg['weekly']), ('month', cfg['monthly'])) if keep is None else ()
        keep = set(ids[:cfg['keep_last'] if keep is None else keep])
        if preserve in ids:
            keep.add(preserve)
        for kind, count in schedule:
            groups = set()
            for ident in ids:
                value = self.manifest(root, ident)
                day = dt.datetime.fromtimestamp(value['at'])
                key = day.strftime('%Y-%m-%d' if kind == 'day' else '%G-%V' if kind == 'week' else '%Y-%m')
                if key not in groups and len(groups) < count:
                    keep.add(ident)
                    groups.add(key)
        for ident in set(ids) - keep:
            (root / 'snapshots' / (ident + '.json')).unlink()
        used = {obj['hash'] for ident in keep for obj in self.manifest(root, ident)['files'].values()}
        for p in (root / 'objects').iterdir():
            if HEX.fullmatch(p.name) and p.name not in used and not p.is_symlink():
                p.unlink()

    async def verify(self, *, full=False):
        self.require()
        if not self.roots():
            raise error('Не выбрана папка бекапов')
        ctx = ToolContext(self.app, actor='owner')
        await ctx.check('backup.verify', 'бекапы', 'перечитать хеши блоков')
        async with self.lock:
            result = await asyncio.to_thread(self._verify, full)
        state = self.app.store.meta_get('backups.state', {})
        state['last_verify'] = time.time()
        if full:
            state['last_full'] = time.time()
        state['verification'] = result
        self.app.store.meta_set('backups.state', state)
        if not result['ok']:
            self.app.journal.write('alarm', 'Повреждён бекап', data=result)
            self.app.bus.publish({'type': 'notify', 'kind': 'alarm', 'title': 'Повреждён бекап', 'text': 'Не удаляй другие копии. Открой Бекапы.', **result})
        return result

    def _verify(self, full):
        bad, checked = [], 0
        for root in self.roots():
            try:
                objects = sorted({obj['hash'] for ident in self.snapshots(root) for obj in self.manifest(root, ident)['files'].values()})
                if not full and len(objects) > 32:
                    objects = random.SystemRandom().sample(objects, max(32, len(objects) // 10))
                for digest in objects:
                    checked += 1
                    block = self.block(root, digest)
                    if not block.is_file() or file_hash(block) != digest:
                        bad.append(str(block))
            except (OSError, ValueError, HumanError) as exc:
                bad.append(str(exc))
        return {'ok': not bad, 'checked': checked, 'bad': bad, 'full': full}

    def preview(self, ident, target, *, destination=0):
        root = self.roots()[destination]
        value = self.manifest(root, ident)
        target = canonical(target)
        if within(target, self.app.dirs.root) or within(self.app.dirs.root, target) or any(within(target, r) or within(r, target) for r in self.roots()):
            raise error('Для восстановления нужна отдельная папка данных', 'Останови старый экземпляр после проверки слота; затем запусти bender --data НОВАЯ_ПАПКА run. Живые данные не перезаписываются.')
        changed = [rel for rel, obj in value['files'].items() if not (target / rel).is_file() or file_hash(target / rel) != obj['hash']]
        extra = [p.relative_to(target).as_posix() for p in target.rglob('*') if p.is_file() and p.relative_to(target).as_posix() not in value['files']] if target.exists() else []
        for key, version in value.get('schemas', {}).items():
            if self.app.store.meta_get(key, None) is not None and int(version) > int(self.app.store.meta_get(key)):
                raise error('В снимке более новая схема данных: ' + key, 'Восстанови совместимой или более новой программой.')
        return {'id': ident, 'target': str(target), 'changed': changed, 'extra': extra, 'secrets': value.get('secrets', False), 'format': value['format']}

    async def restore(self, ident, target, *, destination=0):
        self.require()
        preview = self.preview(ident, target, destination=destination)
        await ToolContext(self.app, actor='owner').check('backup.restore', preview['target'], 'восстановить снимок в отдельный слот данных', detail=preview)
        async with self.lock:
            root = self.roots()[destination]
            value = self.manifest(root, ident)
            for rel, obj in value['files'].items():
                if file_hash(self.block(root, obj['hash'])) != obj['hash']:
                    raise error('Хеш блока не совпал: ' + rel)
            safety = await asyncio.to_thread(self._snapshot, self.roots(), ident)
            result = await asyncio.to_thread(self._restore, ident, Path(preview['target']), destination)
        return {**preview, 'safety': safety['id'], **result}

    def _restore(self, ident, target, destination):
        root = self.roots()[destination]
        value = self.manifest(root, ident)
        if target.exists() and any(target.iterdir()):
            raise error('Папка восстановления не пустая', 'Выбери новый слот: предпросмотр показывает различия, существующие файлы не уничтожаются.')
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.bender-restore-', dir=target.parent) as folder:
            stage = Path(folder) / 'data'
            stage.mkdir()
            for rel, obj in value['files'].items():
                restored=stage / relative_path(rel)
                copy_object(self.block(root, obj['hash']), restored, obj['hash'])
                os.chmod(restored,0o600 | (int(obj.get('mode',0o600)) & 0o100) if rel != 'secrets.json' else 0o600)
            if (stage / 'bender.db').exists():
                with closing(sqlite3.connect(stage / 'bender.db')) as db:
                    if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                        raise error('Восстановленная база не прошла проверку')
                    if not value.get('schemas'):
                        for key, version in db.execute("SELECT key,value FROM meta WHERE key LIKE 'schema:%'"):
                            if self.app.store.meta_get(key, None) is not None and int(json.loads(version)) > int(self.app.store.meta_get(key)):
                                raise error('Схема старого формата новее работающей программы')
            if target.exists():
                target.rmdir()
            os.replace(stage, target)
        return {'ok': True}

    def due(self):
        cfg = self.config()
        if not cfg['enabled'] or not cfg['auto_hours'] or not (cfg['path'] or cfg['second_path']) or self.app.safe:
            return None
        if self.next_at is None:
            state = self.app.store.meta_get('backups.state', {})
            self.next_at = max(time.time() + 60, min(state.get('last_backup', 0) + cfg['auto_hours'] * 3600, state.get('last_verify', 0) + 7 * 86400, state.get('last_full', 0) + 30 * 86400))
        return self.next_at

    async def tick(self, *, sleep_event=False):
        if self.app.guard.halted or any(self.app.mind.running(sid) for sid in self.app.mind.turns) or (not sleep_event and not self.app.heart.owner_idle(15)):
            self.next_at = time.time() + 600
            return
        if self.config()['mode'] == 'sleep' and not sleep_event:
            self.next_at = time.time() + 600
            return
        try:
            state = self.app.store.meta_get('backups.state', {})
            if time.time() - state.get('last_backup', 0) >= self.config()['auto_hours'] * 3600:
                await self.now(scheduled=True)
                state = self.app.store.meta_get('backups.state', {})
            if time.time() - state.get('last_verify', 0) >= 7 * 86400 or time.time() - state.get('last_full', 0) >= 30 * 86400:
                await self.verify(full=time.time() - state.get('last_full', 0) >= 30 * 86400)
        except (HumanError, OSError) as exc:
            self.app.journal.write('alarm', 'Не удалось сохранить бекап', data={'error': str(exc)})
            self.app.bus.publish({'type': 'notify', 'kind': 'alarm', 'title': 'Не удалось сохранить бекап', 'text': str(exc)})
        finally:
            self.next_at = None
