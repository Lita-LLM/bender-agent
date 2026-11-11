"""Неизменяемые слоты, предпросмотр пакета и переключение только хозяином."""
from __future__ import annotations

from ..core.pathutil import same

import hashlib
import json
import os
import shutil
import stat
import tempfile
import time
import zipfile
from pathlib import Path

from ..core.errors import HumanError
from ..core.fsutil import atomic_write_text
from ..core.ids import new_id
from ..core.paths import PROGRAM_DIR
from ..core.watchdog import load_boot, save_boot
from ..mcp.manager import digest
from ..skills.manager import file_path

META = "bender-update.json"
MAX_FILES = 5000
MAX_BYTES = 64*1024*1024


def error(title, hint="Повтори просмотр пакета в разделе «Версии и откат»."):
    return HumanError("update", title, hint, status=409)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def package(root):
    """Только программа/тесты/экзамены; ни данных, ни git-фильтров, ни ссылок."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise error("Нужна обычная папка версии")
    files, total = {}, 0
    for folder in ("bender", "tests", "exams"):
        path = root/folder
        if not path.exists():
            continue
        if path.is_symlink():
            raise error("Ссылки в пакете обновления запрещены")
        for base, dirs, names in os.walk(path, followlinks=False):
            for name in list(dirs):
                p = Path(base)/name
                if p.is_symlink():
                    raise error("Ссылки в пакете обновления запрещены")
            dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", ".git", "node_modules", ".pytest_cache"))
            for name in sorted(names):
                if name.endswith((".pyc", ".pyo")):
                    continue
                p = Path(base)/name
                if p.is_symlink() or not p.is_file():
                    raise error("В пакете есть ссылка или специальный файл")
                with p.open("rb") as f:
                    raw = f.read(8*1024*1024+1)
                total += len(raw)
                if len(raw)>8*1024*1024 or total>MAX_BYTES or len(files)>=MAX_FILES:
                    raise error("Пакет версии слишком большой")
                files[p.relative_to(root).as_posix()] = raw
    for required in ("bender/__init__.py", "bender/__main__.py", "bender/core/server_main.py"):
        if required not in files:
            raise error("В пакете нет программы Бендера", required)
    return files


def manifest(files):
    return {p: hashlib.sha256(b).hexdigest() for p,b in sorted(files.items())}


def snapshot(root, target):
    files = package(root)
    target.mkdir(parents=True)
    for rel,raw in files.items():
        p=target/rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(raw)
    return files


class Versions:
    def __init__(self, app):
        self.app = app
        self.root = app.dirs.root/"versions"
        self.staging = app.dirs.local/"update-candidates"
        if self.root.is_symlink():
            raise error("Папка слотов не может быть ссылкой")
        self.root.mkdir(exist_ok=True, mode=0o700)
        self.staging.mkdir(exist_ok=True, mode=0o700)

    def enabled(self):
        if not self.app.settings.get("updates", "enabled", True):
            raise error("Обновления выключены", "Хозяин может включить их в настройках «Версии».")

    def slot(self, name):
        if not isinstance(name, str) or not name.startswith("v-") or not all(c.isalnum() or c in "-_" for c in name):
            raise error("Некорректное имя слота")
        path = self.root/name
        if path.is_symlink():
            raise error("Слот не может быть ссылкой")
        return path

    def public(self):
        boot = load_boot(self.app.dirs.root)
        slots = boot.get("slots") or {}
        candidates=[]
        for p in sorted(self.staging.glob("v-*/")):
            if (self.root/p.name).exists():
                continue
            try:
                candidates.append(self.inspect(p.name))
            except (HumanError, OSError, ValueError):
                continue
        return {"enabled": self.app.settings.get("updates", "enabled", True), "watched": bool(getattr(self.app,"watched",False)),
                "current": slots.get("current") or "source", "previous": slots.get("previous"), "pending": slots.get("pending"),
                "last_rollback": boot.get("rollback"), "health": boot.get("health"), "candidates":candidates,
                "slots":[p.name for p in sorted(self.root.glob("v-*/")) if not p.is_symlink()],
                "history":self.app.store.meta_get("updates:history", [])[-100:], "keys":self.app.settings.get("updates","public_keys",[])}

    def stage(self, source, *, development=False):
        return self._stage(source, development=development)

    def _stage(self, source, *, development=False):
        self.enabled()
        source = Path(source).expanduser()
        if source.is_symlink() or not source.exists():
            raise error("Источник обновления не найден")
        if self.app.guard.policy.protected(source.resolve()):
            raise error("Источник обновления находится в защищённой папке")
        with tempfile.TemporaryDirectory(prefix="update-", dir=self.app.dirs.local) as temp:
            folder = Path(temp)
            if source.is_file():
                if not zipfile.is_zipfile(source) or source.stat().st_size>MAX_BYTES:
                    raise error("Нужен ZIP-пакет обновления до 64 МБ")
                total,seen=0,set()
                with zipfile.ZipFile(source) as archive:
                    for info in archive.infolist():
                        if info.is_dir():
                            continue
                        rel = str(file_path(info.filename))
                        total += info.file_size
                        if rel in seen or len(seen)>MAX_FILES or total>MAX_BYTES or stat.S_ISLNK(info.external_attr>>16) or info.file_size>8*1024*1024:
                            raise error("В архиве ссылки, повторы или превышены лимиты")
                        seen.add(rel)
                        if rel != META and rel.split("/")[0] not in ("bender","tests","exams"):
                            raise error("В архиве есть файл вне пакета программы",rel)
                        dest=folder/rel
                        dest.parent.mkdir(parents=True,exist_ok=True)
                        dest.write_bytes(archive.read(info))
                source=folder
            files=package(source)
            data={"format":1,"files":manifest(files)}
            signature,public=None,None
            if (source/META).is_file():
                if (source/META).is_symlink() or (source/META).stat().st_size>1024*1024:
                    raise error("Манифест обновления недопустим")
                try:
                    metadata=json.loads((source/META).read_text())
                except (ValueError,UnicodeError):
                    raise error("Манифест обновления повреждён") from None
                if not isinstance(metadata,dict):
                    raise error("Манифест обновления должен быть объектом")
                if metadata.get("format")!=1 or metadata.get("files")!=data["files"]:
                    raise error("Хэши файлов не совпадают с манифестом")
                data=metadata
                signature,public=data.pop("signature",None),data.pop("public_key",None)
            signed=False
            if signature or public:
                from .ed25519 import verify
                try:
                    signed=public in self.app.settings.get("updates","public_keys",[]) and verify(bytes.fromhex(public),canonical(data),bytes.fromhex(signature))
                except (ValueError,TypeError):
                    pass
                if not signed:
                    raise error("Подпись обновления не подтверждена", "Добавь проверенный открытый ключ проекта/хозяина в настройках. Изменённый пакет не устанавливается.")
            elif (not development or not self.app.settings.get("updates","allow_development",False)):
                raise error("У пакета нет подписи", "Для своей разработки хозяин может явно разрешить неподписанные папки/git в настройках. ZIP требует подписи.")
            if not signed and same(temp, source):
                raise error("ZIP-пакет должен иметь доверенную подпись")
            name="v-"+new_id()
            dest=self.staging/name
            dest.mkdir()
            for rel,raw in files.items():
                p=dest/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(raw)
            record={"id":name,"hash":digest(data["files"]),"manifest":data["files"],"signed":signed,"public_key":public,
                    "version":str(data.get("version") or name),"at":time.time()}
            atomic_write_text(dest/".update.json",json.dumps(record,ensure_ascii=False))
            return self.inspect(name)

    async def stage_git(self, url, ctx, ref=""):
        from urllib.parse import urlsplit
        import shlex
        parsed=urlsplit(url)
        if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password:
            raise error("Нужен git HTTPS без логина и пароля")
        await ctx.check("net.read",url,"скачать версию из git")
        with tempfile.TemporaryDirectory(prefix="update-git-",dir=self.app.dirs.work) as temp:
            base=Path(temp)
            git=["git","-c","core.hooksPath=/dev/null","-c","core.fsmonitor=false","-c","protocol.file.allow=never"]
            argv=git+["clone","--depth","1","--no-checkout","--no-recurse-submodules"]+(["--branch",ref] if ref else [])+["--",url,str(base/"repo")]
            result=await self.app.tools.run("run",{"command":shlex.join(argv),"argv":argv,"allow_net":True},ctx)
            if not result.ok: raise error("Git не получил версию",result.content)
            # git archive не запускает фильтры checkout и код репозитория.
            argv=git+["-C",str(base/"repo"),"archive","--format=zip","--output="+str(base/"source.zip"),"HEAD","bender","tests"]
            result=await self.app.tools.run("run",{"command":shlex.join(argv),"argv":argv,"allow_net":False},ctx)
            if not result.ok: raise error("Git не создал архив",result.content)
            if (base/"source.zip").stat().st_size>MAX_BYTES:
                raise error("Git-пакет слишком большой")
            with zipfile.ZipFile(base/"source.zip") as archive:
                total=0
                if len(archive.infolist())>MAX_FILES:
                    raise error("Слишком много файлов git")
                for info in archive.infolist():
                    if info.is_dir():continue
                    rel=str(file_path(info.filename))
                    total+=info.file_size
                    if total>MAX_BYTES or stat.S_ISLNK(info.external_attr>>16) or info.file_size>8*1024*1024:
                        raise error("Недопустимый файл git")
                    p=base/"source"/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_bytes(archive.read(info))
            return self.stage(base/"source",development=True)

    def inspect(self, name):
        self.slot(name)  # Проверка идентификатора.
        path=self.staging/name
        if path.is_symlink():raise error("Кандидат не может быть ссылкой")
        record=json.loads((path/".update.json").read_text())
        current=manifest(package(path))
        if digest(current)!=record.get("hash") or current!=record.get("manifest"):
            raise error("Кандидат изменился после просмотра")
        return {**record,"files":len(current),"bytes":sum((path/p).stat().st_size for p in current)}

    def install(self, name, expected):
        self.enabled()
        self.app.store.meta_set('updates.in_progress',time.time())
        try:return self._install(name,expected)
        finally:self.app.store.meta_set('updates.in_progress',None)

    def _install(self, name, expected):
        self.enabled()
        record=self.inspect(name)
        if expected!=record["hash"]:
            raise error("Нужен просмотр точного хэша версии")
        if record.get("workshop_proposal"):
            raise error("Применение старых правок ядра отменено автором", "История сохранена; обновления устанавливает хозяин обычным подписанным пакетом.")
        if record["signed"]:
            if record["public_key"] not in self.app.settings.get("updates","public_keys",[]):
                raise error("Доверие ключу отозвано после подготовки версии")
        elif not self.app.settings.get("updates","allow_development",False):
            raise error("Режим собственной разработки выключен после подготовки")
        boot=load_boot(self.app.dirs.root)
        slots=boot.get("slots") or {}
        if slots.get("pending"):
            raise error("Другая версия ещё проходит самопроверку")
        current=slots.get("current")
        if not current:
            current="v-base-"+new_id()
            snapshot(PROGRAM_DIR,self.slot(current))
        elif not self.slot(current).is_dir():
            raise error("Текущий слот потерян")
        target=self.slot(name)
        if target.exists():
            if name in (slots.get('current'),slots.get('previous')) or digest(manifest(package(target)))!=expected:
                raise error("Слот уже установлен или его содержимое изменилось")
        else:
            try:
                with tempfile.TemporaryDirectory(prefix='.install-',dir=self.root) as folder:
                    staged=Path(folder)/'slot'
                    shutil.copytree(self.staging/name,staged)
                    os.replace(staged,target)
            except OSError as exc:
                raise HumanError('update_copy','Не удалось скопировать слот обновления','Проверь место и права, затем повтори; текущая версия не переключена.',detail=str(exc)) from None
        nonce="t-"+new_id()
        boot.pop("health",None)
        boot["slots"]={"current":name,"previous":current,"pending":name,"nonce":nonce,
                       "timeout":self.app.settings.get("updates","health_seconds",30)}
        if not save_boot(self.app.dirs.root,boot):
            raise error("Не удалось сохранить переключение версии")
        history=self.app.store.meta_get("updates:history",[])
        self.app.store.meta_set("updates:history",(history+[{"action":"install",**record}])[-100:])
        self.app.journal.write("update",f"Выбран слот {name}; самопроверка и автооткат при запуске",actor="owner")
        return self.public()

    def rollback(self):
        boot=load_boot(self.app.dirs.root)
        slots=boot.get("slots") or {}
        previous=slots.get("previous")
        if not previous or not self.slot(previous).is_dir():
            raise error("Прежнего слота пока нет")
        old=slots.get("current")
        boot["slots"]={"current":previous,"previous":old}
        boot["rollback"]={"at":time.time(),"from":old,"to":previous,"reason":"хозяин нажал «Откатить»"}
        if not save_boot(self.app.dirs.root,boot):raise error("Не удалось сохранить откат")
        self.app.journal.write("rollback",f"Хозяин выбрал прежний слот {previous}",actor="owner")
        return self.public()


async def report_healthy(app, server, port):
    """Самопроверка без модели/внешней сети; одно подтверждение испытуемого запуска."""
    nonce=os.environ.get("BENDER_TRIAL")
    if not nonce:return
    boot=load_boot(app.dirs.root)
    slots=boot.get("slots") or {}
    if nonce!=slots.get("nonce"):
        raise error("Маркер самопроверки не совпал")
    if app.store.one("PRAGMA quick_check")[0]!="ok":raise error("Самопроверка базы не прошла")
    app.router.chain("main")
    app.heart.jobs.all()
    if not app.safe and (not app.heart.task or app.heart.task.done()):raise error("Планировщик не запущен")
    from ..core.web_auth import headers
    response=await app.http.get(f"http://127.0.0.1:{port}/",headers=headers(app.dirs.root))
    async with response:
        await response.read(limit=1024*1024)
    if response.status!=200:raise error("Пульт не прошёл самопроверку")
    atomic_write_text(app.dirs.local/("health-"+nonce+".txt"),nonce)
