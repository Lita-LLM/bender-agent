"""Предохранитель: одна функция решения для любого действия агента.

decide(Действие) → разрешить | спросить | запретить, с причиной человеческими словами.

Порядок проверки:
1. Предохранитель выключен хозяином → разрешить (бортжурнал всё равно пишется).
2. Жёсткий список (если не выключен хозяином) → запретить.
3. Защищённые пути (ключи SSH, сейф, база Бендера…) → запретить, если хозяин не открыл путь правилом.
4. Правила хозяина («всегда такое» / «никогда такое»).
5. Заражённый ход (в контексте был недоверенный контент) + действие, которым можно вынести
   данные или закрепить чужую волю (сообщение не хозяину, отправка в сеть, ядро памяти,
   настройки, расписание, правка себя, секреты) → как минимум спросить. Это разрыв
   «смертельной тройки»: модель могли обмануть, поэтому решает человек. В профиле «Доверие»
   спрашивается только то, что прямо уносит данные к чужим: сеть наружу, сообщения не хозяину, секреты.
6. Профиль (Осторожный / Обычный / Доверие / Выключен).
7. Класс «необратимое» (деньги, правка себя) — всегда спросить, никаких «всегда».
8. Если решение «спросить», а спросить некого (пульс, cron, вебхук) → запретить.

Фильтр текста команды — не граница (его обходят переносом строки, busybox, сокращёнными
опциями — arXiv 2603.27517). Он только решает, какие простые команды чтения можно не
спрашивать, и ловит заведомо катастрофическое в жёстком списке.
"""

from __future__ import annotations

import fnmatch
import os
import re
import shlex
from dataclasses import dataclass, field, replace
from pathlib import Path
from ..core.pathutil import canonical, within as _is_within, same
from ..core.errors import HumanError

CAPS = {
    'system.restart': 'попросить хозяина перезапустить Бендера',
    'system.shutdown': 'выключить Бендера хозяином',
    'system.service': 'управлять автозапуском хозяином',
    'telegram.profile': 'менять публичный профиль бота',
    'telegram.leave': 'выходить из чужого группового чата',
    'telegram.topic': 'восстанавливать привязанную тему Telegram',
    'voice.install': 'управлять отдельной службой локального голоса',
    'channel.send_file': 'отправить файл хозяину',
    'agent.send': 'писать другому агенту',
    'agent.describe': 'написать своё короткое описание в стае',
    'agent.link_request': 'попросить хозяина открыть связь с другим агентом',
    'update.check': 'проверить обычное обновление хозяина', 'update.install': 'установить обычное обновление хозяина',
    "audio.transcribe": "распознать аудио хозяина", "audio.speak": "озвучить ответ хозяину",
    "backup.write": "сохранить бекап", "backup.verify": "проверить бекап", "backup.restore": "восстановить слот данных",
    "fs.read": "читать файл",
    "fs.write": "писать файл",
    "fs.delete": "удалять файл",
    "exec": "выполнить команду",
    "net.read": "читать из сети",
    "net.send": "отправить данные в сеть",
    "screen.look": "посмотреть на разрешённое окно", "screen.act": "нажимать и печатать в разрешённом окне",
    "msg.send": "отправить сообщение",
    "money": "потратить деньги",
    "secret.use": "использовать секрет",
    "mcp.connect": "подключить внешний MCP-сервер",
    "mcp.call": "вызвать внешний MCP-тул",
    "skill.run": "запустить скрипт навыка",
    "memory.core": "изменить ядро памяти",
    "settings": "изменить настройки",
    "manner": "сменить свою манеру речи",
    "schedule": "поставить задачу в расписание",
    "team.spawn": "поручать работу команде",
    "self.code": "изменить собственный код",
    "self.rules": "изменить свои правила (AGENTS.md)",
    "exam.run": "проверить копию в изоляции",
    "skill.learn": "сохранить навык или черновик",
    "workshop.propose": "предложить код и проверить в изоляции",
}

EXFIL = {"exec", "screen.act", "net.send", "msg.send", "memory.core", "settings", "manner", "schedule", "self.code", "self.rules", "secret.use", "mcp.call", "mcp.connect", "skill.run", "agent.describe"}
# Профиль «Доверие»: заражённый ход спрашивает только то, что прямо уносит данные к чужим.
EXFIL_TRUST = {"net.send", "msg.send", "secret.use"}


def leaks(a) -> bool:
    """Прямой вынос к чужим: отправка, секрет или команда с открытой сетью (охота 19.11: allow_net в «Доверии»)."""
    return a.capability in EXFIL_TRUST or (a.capability == "exec" and bool((a.detail or {}).get("net")))


CHAIN = re.compile(r"[;&|`\n]|\$\(")  # команда из нескольких: правило «программа» на неё не распространяется
GIT_CODE = re.compile(r"(?:^|\s)(?:-c|--config(?:-env)?|--upload-pack|--receive-pack|--exec(?:-path)?|-u)(?:\s|=|$)|alias\.|core\.(?:pager|editor|fsmonitor|sshcommand|hookspath)", re.I)
IRREVERSIBLE = {"money", "self.code", "self.rules"}


@dataclass
class Action:
    capability: str
    target: str = ""
    actor: str = "agent"  # owner | agent | subagent:<роль> | heart | cron | module:<id> | mcp:<сервер>
    tool: str = ""
    summary: str = ""  # что именно — человеческими словами, для карточки
    tainted: bool = False
    unattended: bool = False
    to_owner: bool = False  # для msg.send: адресат — сам хозяин
    in_sandbox: bool = False  # для exec: команда пойдёт в песочницу ОС
    detail: dict = field(default_factory=dict)


@dataclass
class Decision:
    verdict: str  # allow | ask | deny
    reason: str
    rule: str = ""  # что сработало: hardline, protected, owner-rule:<id>, taint, profile:<имя>, irreversible, unattended, off
    remember_ok: bool = True  # можно ли предложить «всегда такое»

    @property
    def allowed(self) -> bool:
        return self.verdict == "allow"


# --- жёсткий список -------------------------------------------------------------------------

CMD = r"(?:^|[;&|(`]\s*|\bsudo\s+|\bdoas\s+|\bexec\s+|\bxargs\s+)"
HARDLINE = [
    (r"\brm\s+(-[a-zA-Z]*[rR][a-zA-Z]*\s+|--recursive\s+)+(-[a-zA-Z]+\s+)*(/|/\*|~|~/|\$HOME|/home|/root|/etc|/usr|/var|/boot)(\s|$)", "рекурсивное удаление корня, дома или системных папок"),
    (CMD + r"mkfs(\.\w+)?\b", "форматирование диска"),
    (r"\bdd\b[^\n]*\bof=/dev/(sd|nvme|hd|mmcblk|vd|xvd)", "запись поверх диска"),
    (r">\s*/dev/(sd|nvme|hd|mmcblk|vd|xvd)", "запись поверх диска"),
    (CMD + r"(wipefs|shred)\b[^\n]*/dev/", "стирание диска"),
    (r":\(\)\s*\{\s*:\s*\|\s*:\s*&\s*\}\s*;\s*:", "бомба процессов"),
    (r"\bchmod\s+(-[a-zA-Z]*R[a-zA-Z]*\s+)+[0-7]*7[0-7]*\s+/(\s|$)", "права 777 на весь корень"),
    (r"\bchown\s+(-[a-zA-Z]*R[a-zA-Z]*\s+)+\S+\s+/(\s|$)", "смена владельца всего корня"),
    (CMD + r"(fdisk|parted|sgdisk)\b[^\n]*/dev/", "переразметка диска"),
    (r"\bmv\s+(/|~)\s", "перемещение корня или дома"),
]
_HARDLINE_RX = [(re.compile(p), why) for p, why in HARDLINE]


def hardline_hit(command: str) -> str | None:
    norm = command.replace("\\\n", " ")
    norm = re.sub(r"[\"']", "", norm)  # rm -rf "/" == rm -rf /
    for rx, why in _HARDLINE_RX:
        if rx.search(norm):
            return why
    return None


# --- защищённые пути ------------------------------------------------------------------------

HOME = Path.home()
PROTECTED_HOME = [".ssh", ".gnupg", ".aws", ".azure", ".kube", ".docker/config.json", ".netrc", ".git-credentials",
                  ".config/gh/hosts.yml", ".mozilla", ".config/google-chrome", ".config/chromium", ".config/BraveSoftware",
                  ".password-store", ".local/share/keyrings", ".openclaw/credentials", ".openclaw/secrets.json",
                  ".claude/.credentials.json", ".codex/auth.json"]
PROTECTED_ABS = ["/etc/shadow", "/etc/gshadow", "/etc/sudoers"]


# --- простые команды чтения ------------------------------------------------------------------

SAFE_COMMANDS = {"ls", "pwd", "whoami", "id", "date", "uname", "hostname", "uptime", "cat", "head", "tail", "wc", "grep", "rg",
                 "echo", "df", "du", "free", "ps", "which", "type", "file", "stat", "tree", "sort", "uniq", "cut", "nproc", "lscpu", "lsblk",
                 "python3 --version", "termux-battery-status", "termux-info"}
SAFE_GIT = {"status", "log", "diff", "show", "branch", "remote", "rev-parse", "describe", "tag", "ls-files", "blame"}
SHELL_META = re.compile(r"[;&|`$<>(){}\\\n*?\[\]!]")


def is_safe_read_command(command: str) -> bool:
    """Простая команда только для чтения, без единого метасимвола оболочки. Интерпретаторы — никогда."""
    command = command.strip()
    if not command or SHELL_META.search(command):
        return False
    try:
        parts = shlex.split(command)
    except ValueError:
        return False
    if not parts:
        return False
    prog = os.path.basename(parts[0])
    args = parts[1:]
    if prog == "git":
        return len(parts) >= 2 and parts[1] in SAFE_GIT and not any(p.startswith(("--output", "--ext-diff", "--textconv", "-c", "--exec-path", "--config-env")) or p == "-o"
                                                                    or any(_long_prefix(p, o) for o in ("--output", "--ext-diff", "--textconv", "--exec-path", "--config-env")) for p in parts) \
            and _git_read_only(parts[1], parts[2:])
    if any(_risky_option(prog, a) for a in args):
        return False
    if prog == "uniq" and len([a for a in args if not a.startswith("-")]) > 1:
        return False  # uniq ВХОД ВЫХОД пишет второй файл
    if prog == "hostname" and any(not a.startswith("-") for a in args):
        return False  # hostname ИМЯ меняет имя машины
    return prog in SAFE_COMMANDS


# Опции «простых» программ, которые запускают код или пишут файлы (находка охоты 16.11): rg --pre, tree -o, sort --compress-program…
RISKY_OPTIONS = {
    "rg": ("--pre", "--pre-glob", "-z", "--search-zip"),
    "tree": ("-o", "--output"),
    "sort": ("-o", "--output", "--compress-program", "-T", "--temporary-directory", "--random-source", "--files0-from"),
    "date": ("-s", "--set", "-f", "--file"),
    "hostname": ("-F", "--file", "-b", "--boot"),
    "uniq": ("--output",),
    "grep": ("-f", "--file"),
}


def _long_prefix(arg: str, opt: str) -> bool:
    """GNU getopt_long принимает любой однозначный префикс: --compress-prog, --outp=, --ext-dif (охота 19.11)."""
    name = arg.split("=", 1)[0]
    return opt.startswith("--") and name.startswith("--") and len(name) >= 4 and opt.startswith(name)


def _risky_option(prog: str, arg: str) -> bool:
    for opt in RISKY_OPTIONS.get(prog, ()):
        if arg == opt or arg.startswith(opt + "=") or _long_prefix(arg, opt):
            return True
        if not opt.startswith("--") and arg.startswith(opt) and len(opt) == 2:
            return True  # -ofile, -Tdir
        if not opt.startswith("--") and len(opt) == 2 and arg.startswith("-") and not arg.startswith("--") and opt[1] in arg[1:]:
            return True  # склеенные короткие: -io, -uz
    return False


GIT_LIST_FLAGS = {"-a", "-r", "-v", "-vv", "-l", "--list", "--all", "--remotes", "--verbose", "--show-current", "-n"}


def _git_read_only(sub: str, rest: list[str]) -> bool:
    """branch/tag/remote читают только в формах списка: `git branch x`, `-D`, `remote add` — уже запись."""
    if sub in ("branch", "tag"):
        listing = sub == "branch" or any(a in ("-l", "--list") for a in rest)
        return all(a in GIT_LIST_FLAGS or a.startswith(("--contains", "--merged", "--no-merged", "--points-at", "--sort", "--format", "-n"))
                   or (listing and not a.startswith("-") and sub == "tag") for a in rest)
    if sub == "remote":
        return all(a in ("-v", "--verbose") for a in rest) or (bool(rest) and rest[0] in ("show", "get-url") and all(not a.startswith("-") or a in ("-n", "--all", "--push") for a in rest[1:]))
    return True


# --- профили --------------------------------------------------------------------------------

PROFILE_ORDER = ["careful", "normal", "trust", "off"]


class Policy:
    def __init__(self, *, work_dir: Path, data_dir: Path, get_settings, get_rules):
        self.work_dir = Path(work_dir).resolve()
        self.data_dir = Path(data_dir).resolve()
        self.get_settings = get_settings  # () → группа guard
        self.get_rules = get_rules  # () → список правил хозяина

    # пути

    def resolve(self, raw: str) -> Path:
        p = Path(os.path.expanduser(raw))
        if not p.is_absolute():
            p = self.work_dir / p
        try:
            return canonical(p)  # ссылки и псевдонимы имён раскрываются вместе
        except OSError:
            return Path(os.path.abspath(p))

    def protected(self, path: Path) -> str | None:
        for rel in PROTECTED_HOME:
            if _is_within(path, (HOME / rel).resolve()):
                return f"защищённое место: ~/{rel}"
        for a in PROTECTED_ABS:
            if same(path, a):
                return f"защищённое место: {a}"
        if any(part.casefold() == "bender-backups" for part in path.parts):  # тома без учёта регистра (охота 19.11)
            return "копии данных Бендера (сейф, токены, база) — только через раздел «Бекапы»"
        for parent in list(path.parents)[:8]:
            if (parent / ".bender-restored").is_file():  # восстановленный из бекапа слот вне папки данных (охота 19.11)
                return "восстановленные данные Бендера (сейф, база) — открой их запуском bender --data"
        for name in ("secrets.json", "bender.db", "bender.db-wal", "bender.db-shm", "local", "skills", "modules", "imports", "diary/hermes", "diary/claude", "versions", "workshop", "exams"):
            if _is_within(path, self.data_dir / name):
                return "служебные данные Бендера (сейф, база, ключи) — только через свои тулы"
        return None

    def _reads_protected(self, command: str) -> bool:
        """`cat ~/.ssh/id_rsa` — «простое чтение», но защищённого места: без песочницы так не пускаем (охота 16.11)."""
        try:
            parts = shlex.split(command)
        except ValueError:
            return True
        words = parts[1:]
        prog = os.path.basename(parts[0]) if parts else ""
        # Рекурсивное чтение папки, внутри которой ключи или данные Бендера (grep -r ~, rg … ~): охота 19.11
        recursive = prog in ("rg", "ag", "tree") or (prog == "grep" and any(w == "--recursive" or w.startswith("--directories") or (w.startswith("-") and not w.startswith("--") and ("r" in w or "R" in w)) for w in words)) \
            or (prog == "git" and "--no-index" in words) or (prog == "du" and any(w.startswith("-") and "a" in w for w in words))
        if recursive:
            for w in words:
                if w.startswith("-"):
                    continue
                try:
                    folder = self.resolve(w)
                except Exception:  # noqa: BLE001
                    return True
                if _is_within(self.data_dir, folder) or any(_is_within((HOME / rel), folder) for rel in PROTECTED_HOME):
                    return True
        for w in words:
            if w.startswith("-") and "=" in w:
                w = w.split("=", 1)[1]
            if not w or w.startswith("-"):
                continue
            try:
                if self.protected(self.resolve(w)):
                    return True
            except Exception:  # noqa: BLE001 — неоднозначное имя тоже не «простое чтение»
                return True
        return False

    def _agent_place(self, path: Path) -> str | None:
        """«self» или «checkpoints», если путь в личности или снимках main или любого агента Стаи."""
        roots = [self.data_dir]
        agents = self.data_dir / "agents"
        try:
            if _is_within(path, agents):
                # без строкового is_relative_to: на томах без учёта регистра «Agents/Bob» — тот же агент (охота 19.11)
                roots += [child for child in agents.iterdir() if child.is_dir() and _is_within(path, child)]
        except (OSError, ValueError):
            pass
        for root in roots:
            for name in ("self", "checkpoints"):
                if _is_within(path, root / name):
                    return name
        return None

    def in_work(self, path: Path) -> bool:
        return _is_within(path, self.work_dir)

    # правила хозяина

    def _rule_for(self, a: Action) -> dict | None:
        for r in self.get_rules():
            if not r.get("enabled", True):
                continue
            if r.get("capability") not in (a.capability, "*"):
                continue
            pat = r.get("target") or "*"
            if not _matches(a.target, pat) and not (a.capability.startswith("fs.") and r.get("scope") and _matches(str(self.resolve(a.target)), pat)):
                continue
            if r.get("scope") == "program" and (CHAIN.search(a.target) or re.search(r"[<>]", a.target) or GIT_CODE.search(a.target)):
                continue  # «всегда git» не покрывает перенаправления и git -c alias.x='!…' (охота 19.11)
            if r.get("actor") and r["actor"] != a.actor:
                continue
            if r.get("only_clean") and a.tainted:
                continue
            return r
        return None

    # решение

    def decide(self, a: Action) -> Decision:
        from .control import sealed, updates_command

        if updates_command(a):
            return Decision('deny', 'Агент не обновляет свою программу. Хозяин использует bender update или кнопку пульта.', 'sealed', False)
        if a.capability == 'update.check':
            return Decision('allow', 'Проверка обновления ничего не меняет в программе.', 'owner-update' if a.actor == 'owner' else 'update-check')
        if a.capability == 'update.install':
            # Агент может только попросить: карточку хозяину показывает Guard.check (require_owner), а не профиль.
            return Decision('allow', 'Обновлением управляет хозяин.', 'owner-update') if a.actor == 'owner' else Decision('deny', 'Обновление ставит хозяин: агент только просит карточкой.', 'sealed', False)

        try:
            path = self.resolve(a.target) if a.capability.startswith("fs.") else None
        except HumanError:
            return Decision("deny", "Неоднозначное имя пути запрещено.", "path_alias", False)
        if path is not None and sealed(path, a.capability, self.data_dir):
            return Decision("deny", "Контур доверия и экзамены меняются только руками хозяина в исходниках; карточка не открывает этот путь.", "sealed", False)
        s = self.get_settings()
        if not s.get("enabled", True) or s.get("profile") == "off":
            hard = self._hardline(a, s)
            if hard:
                return Decision("deny", hard.reason + " Выключается отдельно в «Предохранителе».", "hardline", False)
            return Decision("allow", "Предохранитель выключен хозяином — только запись в бортжурнал", "off")
        if a.tainted and not s.get("taint", True):
            a = replace(a, tainted=False)  # хозяин выключил учёт недоверенного текста
        d = self._decide(a, s)
        if d.verdict == "ask" and a.unattended:
            return Decision("deny", d.reason + " — но спросить некого (действие без хозяина рядом). Агент поищет другой путь или спросит позже.", "unattended", False)
        return d

    def _hardline(self, a: Action, s: dict) -> Decision | None:
        """Жёсткий список одинаков при любом профиле, в том числе при выключенном Предохранителе."""
        if not s.get("hardline", True):
            return None
        if a.capability in ("exec", "mcp.connect", "skill.run"):
            why = hardline_hit(a.target if a.capability == "exec" else a.detail.get("command", ""))
            if why:
                return Decision("deny", f"Жёсткий список: {why}.", "hardline", False)
        if a.capability == "fs.delete":
            p = self.resolve(a.target)
            if any(same(p, root) for root in (Path("/"), HOME, HOME.parent, "/etc", "/usr", "/var", "/boot", "/bin", "/lib")):
                return Decision("deny", "Жёсткий список: удаление корня, дома или системной папки.", "hardline", False)
        return None

    def _decide(self, a: Action, s: dict) -> Decision:
        cap = a.capability
        profile = s.get("profile", "careful")
        if profile not in PROFILE_ORDER:
            profile = "careful"
        level = PROFILE_ORDER.index(profile)
        exfil = EXFIL_TRUST if profile == "trust" else EXFIL

        # 2. жёсткий список
        hard = self._hardline(a, s)
        if hard:
            return hard

        # 3. защищённые пути
        path = None
        if cap.startswith("fs."):
            path = self.resolve(a.target)
            if cap in ("fs.write", "fs.delete"):
                # Охота 16.11: личность агента (data/self) пишется с тем же вопросом, что update_self, а снимки — только своим тулом.
                place = self._agent_place(path)
                if place == "checkpoints":
                    return Decision("deny", "Нельзя: снимки отката меняет только сам Бендер.", "protected", False)
                if place == "self":
                    core = "self.rules" if path.name.upper() == "AGENTS.MD" else "memory.core"
                    return self._decide(replace(a, capability=core, target=str(path)), s)
                if ".git" in path.parts or path.name in (".gitattributes", ".gitmodules"):
                    # .git/config и атрибуты задают программы, которые запустит потом «простое» git status/diff (охота 19.11)
                    return self._decide(replace(a, capability="exec", target="записать " + str(path), detail={**(a.detail or {}), "git_internals": True}), s)
            why = self.protected(path)
            if why:
                rule = self._rule_for(a)
                # защищённое открывает только точное правило хозяина, а не «всегда» из карточки по папке
                if not (rule and rule.get("verdict") == "allow" and rule.get("target") not in ("*", "") and not rule.get("scope")):
                    return Decision("deny", f"Нельзя: {why}. Хозяин может открыть конкретный путь правилом в «Правах».", "protected", False)

        # 4. правила хозяина
        rule = self._rule_for(a)
        if rule:
            if rule.get("verdict") == "deny":
                return Decision("deny", f"Правило хозяина: {rule.get('note') or 'запрещено'}.", f"owner-rule:{rule.get('id')}", False)
            # «Всегда» хозяина держится и после чужого текста (ответ MCP, сайт, письмо) — иначе вторая же
            # команда хода снова спрашивала бы. Не держится только прямая отправка чужим и встречный
            # запрос MCP-сервера к Бендеру (папки, модель, форма) — «все тулы сервера» его не разрешали.
            blocked = a.tainted and ((leaks(a) and not (cap == "msg.send" and a.to_owner)) or (cap == "mcp.call" and a.detail.get("server_request") and cap in exfil))
            if rule.get("verdict") == "allow" and not blocked and cap not in IRREVERSIBLE:
                return Decision("allow", f"Правило хозяина: {rule.get('note') or 'разрешено'}.", f"owner-rule:{rule.get('id')}")

        # Ключ поиска, привязанный в Сейфе к хосту поставщика: уходит только ему, модель его не видит.
        if cap == "secret.use" and a.tool == "web_search" and a.detail.get("search_key") and a.detail.get("host"):
            return Decision("allow", "Ключ поиска используется только для своего поставщика (хост привязан в Сейфе).", "search-key")

        # 5. заражённый ход
        if a.tainted and (cap in exfil or leaks(a)) and not (cap == "msg.send" and a.to_owner):
            return Decision("ask", "В этом ходе агент читал недоверенное (сеть, чужие сообщения, внешние тулы). "
                                   "Действие, которым можно вынести данные или закрепить чужую волю, решает хозяин.", "taint")

        if cap.startswith("backup."):
            if a.actor == "owner" or (a.actor == "heart" and cap == "backup.write"):
                return Decision("allow", "Бекап в папки, выбранные хозяином.", "backup")
            return Decision("deny", "Бекапами управляет хозяин; расписание использует выбранные им папки.", "backup", False)

        if cap in ("audio.transcribe", "audio.speak"):
            if cap=='audio.transcribe' and a.tool=='read_file' and a.target in ('ears','local_voice'):
                return Decision('allow','Разбор прочитанного файла настроенными Ушами; доступ к файлу проверен отдельно.','audio-read-file')
            if cap=='audio.speak' and a.to_owner:
                return Decision('allow','Озвучка ответа только в текущий разговор хозяина.','audio')
            return Decision("allow", "Аудио явно включено хозяином для его канала.", "audio") if a.actor == "owner" else Decision("deny", "Аудиоканалом управляет хозяин.", "audio", False)

        # 7. необратимое
        if cap in IRREVERSIBLE:
            return Decision("ask", "Необратимое действие — только с подтверждением каждого раза.", "irreversible", False)

        # 6. профиль
        name = f"profile:{profile}"
        if cap == "mcp.connect":
            if a.actor == "owner":
                return Decision("allow", "Хозяин явно подключает сервер из пульта или Полигона.", name)
            if level >= 1:
                return Decision("allow", "Сервер подключил и одобрил хозяин в разделе MCP.", name)
            return Decision("ask", "Запуск внешней программы или соединение с MCP-сервером.", name)
        if cap in ("mcp.call", "skill.run"):
            return Decision("allow", "Профиль «Доверие».", name) if level >= 2 else Decision("ask", "Чужая программа может менять файлы, сеть и другие системы; её описание не даёт прав.", name)
        if cap == "fs.read":
            if path is not None and (self.in_work(path) or level >= 1 or _is_within(path, self.data_dir)):
                return Decision("allow", "Чтение файла.", name)
            return Decision("ask", "Чтение вне рабочей папки.", name)
        if cap in ("fs.write", "fs.delete"):
            if path is not None and self.in_work(path):
                return Decision("allow", "Рабочая папка агента.", name)
            if level >= 2 or (level == 1 and cap == "fs.write" and _is_within(path or Path("/"), HOME)):
                return Decision("allow", "Профиль разрешает правки вне рабочей папки.", name)
            return Decision("ask", ("Запись" if cap == "fs.write" else "Удаление") + " вне рабочей папки.", name)
        if cap == "exec":
            if is_safe_read_command(a.target) and not self._reads_protected(a.target):
                return Decision("allow", "Простая команда только для чтения.", name)
            if a.in_sandbox and level >= 1:
                return Decision("allow", "Команда идёт в песочницу.", name)
            if level >= 2:
                return Decision("allow", "Профиль «Доверие».", name)
            return Decision("ask", "Команда на машине" + (" (в песочнице)" if a.in_sandbox else " — песочницы нет") + ".", name)
        if cap in ("screen.look", "screen.act"):
            return Decision("ask", "Окно на экране хозяина: " + ("посмотреть." if cap == "screen.look" else "нажать или напечатать.") + " Профиль этого не разрешает сам.", name)
        if cap == "net.read":
            if a.detail.get("internal") and a.tainted and not s.get("local_network", False):
                return Decision("ask", f"Адрес {a.detail['internal']} — этот компьютер, домашняя сеть или метаданные облака (роутер, службы, ключи). "
                                       "Чужой текст мог попросить заглянуть туда.", "internal-network")
            return Decision("allow", "Чтение из сети (дальше ход считается заражённым).", name)
        if cap == "net.send":
            return Decision("allow", "Профиль «Доверие».", name) if level >= 2 else Decision("ask", "Отправка данных в сеть.", name)
        if cap == "msg.send":
            if a.to_owner:
                return Decision("allow", "Сообщение хозяину.", name)
            return Decision("allow", "Профиль «Доверие».", name) if level >= 2 else Decision("ask", "Сообщение не хозяину.", name)
        if cap == "secret.use":
            return Decision("ask", "Секрет в месте, для которого он не разрешён.", name)
        if cap in ("memory.core", "schedule", "team.spawn", "exam.run", "skill.learn", "workshop.propose"):
            return Decision("allow", "Своя память и расписание.", name)
        if cap == "settings":
            return Decision("allow", "Настройки, которые не ослабляют защиту.", name)
        if cap == "agent.link_request":
            return Decision("allow", "Просьба хозяину: прав она не даёт, решает он.", name)
        if cap == "agent.describe":
            return Decision("allow", "Своё короткое описание в стае: соседи его видят, прав оно не даёт.", name)
        if cap == "manner":
            if a.actor == "owner" or level >= 1:
                return Decision("allow", "Манера речи — из библиотеки хозяина.", name)
            return Decision("ask", "Сменить манеру речи (профиль «Осторожный» спрашивает).", name)
        return Decision("ask", f"Неизвестное действие «{cap}».", name)


def _matches(target: str, pattern: str) -> bool:
    """«git *» покрывает и голое «git», «/папка/*» — и саму папку."""
    return fnmatch.fnmatch(target, pattern) or (pattern.endswith((" *", "/*", os.sep + "*")) and target == pattern[:-2])


def remember_scope(policy: "Policy", a: Action) -> tuple[str, str, str]:
    """Что запомнит «Всегда»: (шаблон цели, вид, слова для кнопки).
    Точный текст команды или файла не повторяется — запоминается программа, папка, узел или сервер."""
    from urllib.parse import urlsplit
    cap, target = a.capability, a.target
    if cap == "exec":
        if CHAIN.search(target):
            return target, "", "эту команду"
        try:
            words = shlex.split(target)
        except ValueError:
            return target, "", "эту команду"
        words = [w for w in words if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", w)]
        if not words or words[0] in ("sudo", "doas", "env", "sh", "bash", "zsh", "busybox", "xargs", "exec", "eval", "command"):
            return target, "", "эту команду"
        return words[0] + " *", "program", "команды " + words[0]
    if cap.startswith("fs."):
        folder = policy.resolve(target).parent
        if any(same(folder, root) for root in (Path("/"), HOME, HOME.parent)):
            return str(policy.resolve(target)), "", "этот файл"
        return str(folder) + os.sep + "*", "folder", "файлы в " + str(folder)
    if cap in ("net.read", "net.send"):
        try:
            parts = urlsplit(target)
        except ValueError:
            return target, "", "этот адрес"
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}/*", "host", parts.hostname or parts.netloc
        return target, "", "этот адрес"
    if cap in ("mcp.call", "mcp.connect") and "/" in target:
        server = target.split("/", 1)[0]
        return server + "/*", "server", "все тулы MCP " + server
    return target, "", "такое же действие"
