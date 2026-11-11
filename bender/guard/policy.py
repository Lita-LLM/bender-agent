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
CHAIN = re.compile(r"[;&|`\n]|\$\(")  # команда из нескольких: правило «программа» на неё не распространяется
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
    if prog == "git":
        return len(parts) >= 2 and parts[1] in SAFE_GIT and not any(p.startswith("--output") or p == "-o" for p in parts)
    if prog in ("sort",) and any(p.startswith("-o") or p.startswith("--output") for p in parts[1:]):
        return False
    return prog in SAFE_COMMANDS


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
        for name in ("secrets.json", "bender.db", "bender.db-wal", "bender.db-shm", "local", "skills", "modules", "imports", "diary/hermes", "diary/claude", "versions", "workshop", "exams"):
            if _is_within(path, self.data_dir / name):
                return "служебные данные Бендера (сейф, база, ключи) — только через свои тулы"
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
            if r.get("scope") == "program" and CHAIN.search(a.target):
                continue
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
            blocked = a.tainted and ((cap in EXFIL_TRUST and not (cap == "msg.send" and a.to_owner)) or (cap == "mcp.call" and a.detail.get("server_request") and cap in exfil))
            if rule.get("verdict") == "allow" and not blocked and cap not in IRREVERSIBLE:
                return Decision("allow", f"Правило хозяина: {rule.get('note') or 'разрешено'}.", f"owner-rule:{rule.get('id')}")

        # 5. заражённый ход
        if a.tainted and cap in exfil and not (cap == "msg.send" and a.to_owner):
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
            if is_safe_read_command(a.target):
                return Decision("allow", "Простая команда только для чтения.", name)
            if a.in_sandbox and level >= 1:
                return Decision("allow", "Команда идёт в песочницу.", name)
            if level >= 2:
                return Decision("allow", "Профиль «Доверие».", name)
            return Decision("ask", "Команда на машине" + (" (в песочнице)" if a.in_sandbox else " — песочницы нет") + ".", name)
        if cap in ("screen.look", "screen.act"):
            return Decision("ask", "Окно на экране хозяина: " + ("посмотреть." if cap == "screen.look" else "нажать или напечатать.") + " Профиль этого не разрешает сам.", name)
        if cap == "net.read":
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
