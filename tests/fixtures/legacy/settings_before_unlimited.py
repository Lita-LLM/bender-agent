"""Настройки со схемами: всё настраивается и всё отключается, без файлов для ручной правки.

У каждой настройки в коде есть тип, значение по умолчанию, человеческое название и
подсказка. В базе хранятся только изменённые значения; незнакомые ключи (от будущих
версий) сохраняются как есть. Ключи не переименовываются никогда — новое имя значит
новая настройка.

Некоторые изменения ослабляют защиту или расширяют самостоятельность агента (профиль
Предохранителя мягче, жёсткий список выключен, пульт открыт в сеть). Их может сделать
только хозяин: если просит сам агент (тул settings), Settings.set поднимает NeedsOwner,
и изменение превращается в карточку для подтверждения.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import HumanError
from .store import Store, dumps


@dataclass
class Field:
    key: str
    type: str  # bool | int | float | str | choice | json
    default: Any
    title: str
    hint: str = ""
    choices: dict[str, str] | None = None  # значение → название (порядок важен для ослабления)
    min: float | None = None
    max: float | None = None
    weakens: Callable[[Any, Any], bool] | None = None  # (старое, новое) → ослабляет ли
    restart: bool = False
    secret: bool = False


@dataclass
class Group:
    name: str
    title: str
    hint: str
    fields: list[Field] = field(default_factory=list)

    def field(self, key: str) -> Field | None:
        return next((f for f in self.fields if f.key == key), None)


def _order_weakens(order: list[str]):
    def check(old, new) -> bool:
        try:
            return order.index(new) > order.index(old)
        except ValueError:
            return True
    return check


def _turned_off(old, new) -> bool:
    return bool(old) and not bool(new)


def _turned_on(old, new) -> bool:
    return not bool(old) and bool(new)


GUARD_PROFILES = {"careful": "Осторожный", "normal": "Обычный", "trust": "Доверие", "off": "Выключен"}

SCHEMA: dict[str, Group] = {}


def _g(name: str, title: str, hint: str, *fields: Field) -> None:
    SCHEMA[name] = Group(name, title, hint, list(fields))


_g("inspectors", "Инспекторы", "Память и бортжурнал по запросу, без фонового опроса.",
   Field("enabled", "bool", True, "Инспекторы включены"),
   Field("track_recall", "bool", True, "Записывать связи recall с ходами", weakens=_turned_off))

_g("voice", "Голос", "Отдельные аудиороли «Уши» и «Голос»; совместимый локальный сервер тоже подходит.",
   Field("enabled", "bool", False, "Голос включён", weakens=_turned_on),
   Field("stt", "bool", True, "Распознавание"), Field("tts", "bool", True, "Синтез"),
   Field("auto_speak", "bool", False, "Автоозвучка ответа в пульте", weakens=_turned_on),
   Field("telegram_reply", "bool", False, "Отвечать голосом в Telegram", weakens=_turned_on),
   Field("stt_protocol", "choice", "openai", "Протокол распознавания", choices={"openai":"OpenAI","whisper":"whisper.cpp /inference"}),
   Field("tts_protocol", "choice", "openai", "Протокол синтеза", choices={"openai":"OpenAI","piper":"Piper HTTP (WAV)"}),
   Field("speaker", "str", "alloy", "Название голоса у провайдера"),
   Field("format", "choice", "mp3", "Формат в пульте", choices={"mp3":"MP3","opus":"OGG/Opus","wav":"WAV"}),
   Field("stt_usd_minute", "float", -1, "STT: $ за минуту (-1 — неизвестно)", min=-1, max=100),
   Field("tts_usd_million_chars", "float", -1, "TTS: $ за миллион знаков (-1 — неизвестно)", min=-1, max=10000),
   Field("max_mb", "int", 16, "Максимум аудио, МБ", min=1, max=32),
   Field("timeout", "int", 60, "Ожидание провайдера, секунд", min=5, max=300))

_g("appearance", "Лицо и тема", "SVG-визор реагирует на события, в простое и фоне не анимируется.",
   Field("enabled", "bool", True, "Показывать визор"), Field("first_steps", "bool", True, "Подсказки первых шагов"),
   Field("theme", "choice", "system", "Тема", choices={"system":"По системе","dark":"Тёмная","light":"Светлая"}),
   Field("large_font", "bool", False, "Крупный шрифт"), Field("high_contrast", "bool", False, "Высокая контрастность"))

_g("backups", "Бекапы", "Без шифрования. В Termux выбирай папку вне приложения и второй носитель.",
   Field("enabled", "bool", True, "Бекапы включены"),
   Field("path", "str", "", "Первая папка"), Field("second_path", "str", "", "Вторая папка"),
   Field("include_secrets", "bool", False, "Включать secrets.json", "В бекапе будут открытые ключи и токены. Ограничь доступ к носителю.", weakens=_turned_on),
   Field("auto_hours", "int", 0, "Интервал, часов (0 — вручную)", min=0, max=8760),
   Field("mode", "choice", "idle", "Когда сохранять", choices={"idle":"В простое", "sleep":"После сна"}),
   Field("keep_last", "int", 7, "Последних снимков", min=1, max=1000),
   Field("daily", "int", 14, "Дней", min=0, max=365), Field("weekly", "int", 8, "Недель", min=0, max=520),
   Field("monthly", "int", 12, "Месяцев", min=0, max=120))

_g("workshop", "Мастерская", "Предложения и экзамены; права проверки меняет только хозяин в пульте.",
   Field("enabled", "bool", True, "Мастерская включена"),
   Field("experience", "bool", True, "Навыки из опыта"),
   Field("lessons", "bool", True, "Уроки из ошибок и поправок"),
   Field("metrics", "bool", True, "Учёт технических исходов для метрик"),
   Field("curator", "bool", True, "Куратор навыков во сне"),
   Field("tools_enabled", "bool", True, "Предложения тулов-кода"),
   Field("network_proxy", "bool", False, "Сетевой посредник тулов-кода", "Только домены одобренного пакета; нужен bubblewrap. CONNECT может отправлять данные этим доменам."),
   Field("live_exams", "bool", False, "Живые экзамены с моделями"),
   Field("live_budget", "float", 0.1, "Бюджет живого прогона, $", min=0, max=10),
   Field("exam_seconds", "int", 30, "Срок одного экзамена, секунд", min=1, max=120),
   Field("stale_days", "int", 14, "Дней до отметки «устарел»", min=1, max=365),
   Field("archive_days", "int", 30, "Дней до архива", min=1, max=730),
   Field("merge_skills", "bool", False, "Предлагать склейку навыков моделью"))

_g("updates", "Версии", "Подписанные обновления и автооткат; управляет только хозяин из пульта.",
   Field("enabled", "bool", True, "Обновления включены"),
   Field('auto', 'choice', 'notify', 'Обычное обновление git', 'Проверка раз в сутки; ночью — только по решению хозяина, с бекапом и самопроверкой.',
         choices={'off': 'Выключено', 'notify': 'Сообщать о новых версиях', 'night': 'Ставить во время сна'}, weakens=lambda old, new: new == 'night' and old != 'night'),
   Field("allow_development", "bool", False, "Разрешить неподписанные папки/git для разработки", "Только явный просмотр хозяином; ZIP всегда требует подписи."),
   Field("public_keys", "json", [], "Доверенные открытые ключи Ed25519", "Список hex-строк по 32 байта. Проверь отпечаток у издателя отдельно."),
   Field("health_seconds", "int", 30, "Срок самопроверки новой версии, секунд", min=1, max=50))

_g("migration", "Переезд", "Hermes и заметки Claude Code: источник только читается; точный план одобряет хозяин.",
   Field("enabled", "bool", True, "Перенос включён", weakens=_turned_on),
   Field("text_mb", "int", 16, "Текст предпросмотра, МБ", min=1, max=128),
   Field("database_mb", "int", 256, "Файл базы источника, МБ", min=1, max=2048),
   Field("sessions", "int", 1000, "Сессий за перенос", min=1, max=100000),
   Field("messages", "int", 10000, "Сообщений за перенос", min=1, max=1000000))


_g("mcp", "Внешние MCP-серверы", "Подключения — в разделе «MCP». Новые определения проходят одобрение хозяина.",
   Field("enabled", "bool", True, "MCP-клиент включён", "В безопасном режиме подключения всегда выключены.", weakens=_turned_on),
   Field("timeout_seconds", "int", 30, "Время запроса MCP, секунд", min=1, max=600),
   Field("servers", "json", [], "Подключения", "Секреты — ссылками на сейф. Изменения подключения требуют одобрения определений заново.", weakens=lambda old, new: old != new))
_g("mcp_trust", "Одобрения MCP", "Отдельно от настроек подключения; только просмотр хозяином.")

_g("skills", "Навыки", "SKILL.md и его файлы раскрываются постепенно. Импортированное ждёт просмотра в разделе «Навыки».",
   Field("enabled", "bool", True, "Навыки включены", weakens=_turned_on),
   Field("scripts_enabled", "bool", True, "Скрипты навыков", "Каждый запуск проходит Предохранитель.", weakens=_turned_on),
   Field("disabled", "json", [], "Выключенные навыки", "Список имён; одобрения хранятся отдельно.", weakens=lambda old, new: any(x not in new for x in old)),
   Field("index_chars", "int", 6000, "Каталог в контексте, символов", min=1000, max=24000),
   Field("max_files", "int", 128, "Файлов в пакете", min=1, max=512),
   Field("max_total_mb", "int", 8, "Размер пакета, МБ", min=1, max=64),
   Field("timeout_seconds", "int", 120, "Время скрипта, секунд", min=1, max=1800))
_g("skill_trust", "Одобрения навыков", "Хэши просмотренных файлов; только хозяин.")

_g("modules", "Модули", "Пакеты в карантине; код только отдельным MCP-процессом.",
   Field("enabled", "bool", True, "Модули включены", weakens=_turned_on),
   Field("disabled", "json", [], "Выключенные модули", weakens=lambda old, new: any(x not in new for x in old)),
   Field("max_files", "int", 128, "Файлов в пакете", min=1, max=512),
   Field("max_total_mb", "int", 8, "Объём пакета, МБ", min=1, max=64))
_g("module_trust", "Одобрения модулей", "Только хозяин; хэш всех файлов, снимок и параметры запуска.")
_g("module_channels", "Каналы модулей", "Только хозяин; связь с защищёнными вебхуками.")

_g("processes", "Фоновые команды", "Те же ограничения и Предохранитель, что у run; при выключении процессы останавливаются.",
   Field("enabled", "bool", True, "Фоновые команды включены", weakens=_turned_on),
   Field("max_running", "int", 4, "Одновременно процессов", min=1, max=32, weakens=lambda old, new: new > old),
   Field("timeout_seconds", "int", 1800, "Предельное время, секунд", min=1, max=86400, weakens=lambda old, new: new > old))

_g("search", "Поиск в сети", "Поставщики перебираются по порядку. Ключи добавляются в Сейф; здесь только имена секретов.",
   Field("enabled", "bool", True, "Поиск включён", weakens=_turned_on),
   Field("providers", "str", "duckduckgo,searxng,brave,tavily", "Порядок поставщиков", "Имена через запятую: duckduckgo, searxng, brave, tavily."),
   Field("duckduckgo_url", "str", "https://html.duckduckgo.com/html/", "DuckDuckGo HTML"),
   Field("searxng_url", "str", "", "SearXNG /search", "Адрес своей инстанции с включённым JSON."),
   Field("brave_url", "str", "https://api.search.brave.com/res/v1/web/search", "Brave Search API"),
   Field("tavily_url", "str", "https://api.tavily.com/search", "Tavily API"),
   Field("brave_secret", "str", "search:brave", "Имя ключа Brave в сейфе"),
   Field("tavily_secret", "str", "search:tavily", "Имя ключа Tavily в сейфе"),
   Field("tavily_country", "str", "", "Страна Tavily", "Необязательно; название страны по-английски, например united states."),
   Field("per_minute", "int", 10, "Запросов в минуту на поставщика", min=1, max=120),
   Field("cache_seconds", "int", 300, "Хранить ответы в памяти, секунд", min=0, max=3600),
   Field("safesearch", "choice", "moderate", "Фильтрация выдачи", "Поддержка зависит от поставщика; Tavily не обещает эквивалент SafeSearch.", choices={"strict": "Строгая", "moderate": "Умеренная", "off": "Выключена"}),
   Field("region", "str", "wt-wt", "Регион поиска", "Для DDG — wt-wt или ru-ru/us-en; Brave использует код страны. Tavily — без региона по умолчанию."))


_g("fuel", "Топливо", "Квоты из уже полученных ответов; OmniRoute проверяется только по кнопке.",
   Field("enabled", "bool", True, "Собирать топливо"),
   Field("notify", "bool", True, "Предупреждать на 85% и 100% окна"))

_g("agent", "Агент", "Как Бендер ведёт ход: лимиты, язык, уведомления.",
   Field("max_steps", "int", 25, "Шагов с тулами за ход", "Сколько раз подряд агент может вызвать тулы, прежде чем остановиться и отчитаться.", min=1, max=200),
   Field("max_turn_seconds", "int", 900, "Время на ход, секунд", "Дольше — ход останавливается с тем, что успел.", min=10, max=86400),
   Field("max_turn_cost", "float", 0.0, "Денег на ход, $", "0 — без лимита. Превышение — пауза и вопрос.", min=0),
   Field("language", "str", "ru", "Язык", "На каком языке агент говорит по умолчанию."),
   Field("memory_notices", "choice", "on", "Строки «💾 Запомнил»", "Показывать ли в чате, что агент что-то сохранил.", {"off": "не показывать", "on": "коротко", "verbose": "подробно"}),
   Field("awakening", "bool", True, "Ритуал пробуждения", "При первом разговоре агент знакомится и строит себя. Выключи, чтобы пропустить."),
   Field("history_tokens", "int", 0, "История в каждом запросе, токенов", "0 — сам: по длине контекста модели, но не больше 32 000 (длинная история дорога на платных моделях). Не влезло — Бендер урежет и повторит.", min=0, max=1000000))

_g("guard", "Предохранитель", "Одна граница безопасности для всех действий агента. Ослабить может только хозяин.",
   Field("enabled", "bool", True, "Предохранитель включён", "Выключенный только пишет бортжурнал и ничего не останавливает.", weakens=_turned_off),
   Field("profile", "choice", "careful", "Профиль", "Насколько агент самостоятелен без вопросов.", GUARD_PROFILES, weakens=_order_weakens(list(GUARD_PROFILES))),
   Field("hardline", "bool", True, "Жёсткий список", "Неотменяемые запреты (rm -rf /, форматирование дисков, бомбы процессов…). Выключать — только если точно понимаешь зачем.", weakens=_turned_off),
   Field("approval_timeout", "int", 300, "Ждать одобрения, секунд", "Нет ответа — отказ.", min=10, max=86400),
   Field("checkpoints", "bool", True, "Снимки перед правками", "Перед разрушающей правкой файла — копия для отката.", weakens=_turned_off),
   Field("sandbox", "choice", "auto", "Песочница команд", "auto — лучшая доступная на этой машине.", {"auto": "авто", "off": "без песочницы"}, weakens=_order_weakens(["auto", "off"])),
   Field("resource_limits", "bool", True, "Лимиты дочерних процессов", "POSIX; для новых процессов, независимо от песочницы.", weakens=_turned_off),
   Field("cpu_seconds", "int", 120, "Процессор на процесс, секунд", min=1, max=86400, weakens=lambda old, new: new > old),
   Field("memory_mb", "int", 1024, "Адресное пространство процесса, МБ", "Для Node/V8 может понадобиться больше; это виртуальная память, не RSS.", min=64, max=65536, weakens=lambda old, new: new > old),
   Field("process_count", "int", 128, "Процессов на пользователя", "RLIMIT_NPROC учитывает другие процессы этого пользователя.", min=16, max=4096, weakens=lambda old, new: new > old),
   Field("file_mb", "int", 64, "Размер одного файла, МБ", min=1, max=4096, weakens=lambda old, new: new > old))

_g("heart", "Сердце", "Пульс и таймеры: агент сам просыпается проверить, не надо ли что-то сделать.",
   Field("enabled", "bool", True, "Сердце бьётся", "Выключенное сердце — агент отвечает только на сообщения."),
   Field("mode", "choice", "two_stage", "Режим пульса", "", {"two_stage": "сначала рефлексы, модель — только если есть что обсудить", "always_model": "всегда звать модель (как OpenClaw)", "reflexes": "только рефлексы без модели", "off": "пульса нет"}),
   Field("interval_minutes", "int", 30, "Пульс, минут", "", min=1, max=1440),
   Field("night_interval_minutes", "int", 120, "Пульс ночью, минут", "", min=1, max=1440),
   Field("quiet_hours", "str", "23:00-08:00", "Тихие часы", "В это время агент пишет только о срочном."),
   Field("intents", "bool", True, "Намерения", "«Когда зайдёт речь о … — напомни мне»: срабатывают на слова хозяина, без модели."),
   Field("think_role", "choice", "fast", "Чем думать на пульсе", "Какая роль моделей решает, что делать, когда пульс что-то нашёл.", {"fast": "Быстрая (дешевле)", "main": "Главная"}),
   Field("alarm_temp_c", "int", 85, "Тревога перегрева, °C", "Выше — пульс скажет хозяину сразу, даже в тихие часы.", min=40, max=120))

_g("sleep", "Сон", "Ночью агент приводит память в порядок.",
   Field("enabled", "bool", True, "Спать", ""),
   Field("at", "str", "03:00", "Когда засыпать", "По местному времени, если хозяин не пишет 30 минут; занят — позже, до 06:00; не вышло — следующей ночью."),
   Field("budget_usd", "float", 0.05, "Денег на ночь, $", "0 — только бесплатные и локальные модели.", min=0),
   Field("promote", "bool", True, "Закреплять в ядро", "То, что за день пригождалось не раз, переносится в MEMORY.md и USER.md."),
   Field("rewrite_core", "bool", True, "Ужимать ядро", "MEMORY.md почти полон — сон переписывает его плотнее (прежний вид сохраняется)."),
   Field("max_loss_pct", "int", 30, "Сколько записей можно потерять при ужатии, %", "Больше — переписанное отвергается.", min=0, max=90),
   Field("morning_report", "bool", True, "Утренний доклад", "Наутро в разговоре — коротко, что было во сне."))

_g("memory", "Память", "",
   Field("core_limit", "int", 3000, "Размер MEMORY.md, символов", "Переполнение — агент сам ужимает.", min=500, max=50000),
   Field("user_limit", "int", 2000, "Размер USER.md, символов", "", min=300, max=20000),
   Field("review", "bool", True, "Разбор после хода", "После обычного хода хозяина модель роли «Быстрая» решает, что стоит запомнить (USER.md, MEMORY.md, факты, дневник). Чаще всего — ничего. После чтения сети и чужих сообщений не идёт."),
   Field("review_batch", "int", 5, "Разбор без «Быстрой» — раз в N ходов", "Если роль «Быстрая» не настроена, разбор идёт на Главной модели — и, чтобы не платить за каждый ход, пачкой раз в N ходов. С «Быстрой» — после каждого хода.", min=1, max=50),
   Field("compact", "bool", True, "Сжатие разговора", "Разговор перестал влезать — старая часть заменяется пересказом (модель роли «Быстрая», в фоне после хода). Выключено — старое просто не показывается; найти его можно тулом recall."),
   Field("new_page", "choice", "off", "Новая страница разговора", "Разговор с моделью начинается заново; память, дневник и поиск остаются. Короче запросы, дешевле ходы — агент вспоминает нужное сам.",
         {"off": "никогда (только сжатие)", "daily": "каждый день", "idle": "после долгой паузы"}),
   Field("new_page_at", "str", "04:00", "Новый день начинается в", "Для «каждый день»: первое сообщение после этого времени начнёт новую страницу."),
   Field("new_page_idle_hours", "int", 8, "Долгая пауза, часов", "Для «после долгой паузы».", min=1, max=720),
   Field("triggers", "bool", True, "Триггеры записей", "Запись с <!-- trigger: … --> всплывает, когда хозяин заговорил о её теме (до 3 за ход)."),
   Field("auto_recall", "bool", True, "Вспоминать само", "Перед ответом Бендер ищет в дневнике, разговорах и фактах сильные совпадения со словами хозяина и кладёт до 3 рядом с сообщением."),
   Field("vectors", "bool", True, "Поиск по смыслу", "Векторы от модели роли «Эмбеддинги», считаются в фоне после ходов. Нет роли — поиск словами."))

_g("budget", "Деньги", "",
   Field("daily_usd", "float", 0.0, "Бюджет на день, $", "0 — без лимита. Кончился — режим бомжа.", min=0),
   Field("economy_auto", "bool", True, "Режим бомжа сам", "Когда бюджет кончился — только бесплатные модели."))

_g("telegram", "Telegram", "Бендер в Telegram: свой бот, привязанный к хозяину одноразовым кодом.",
   Field("enabled", "bool", False, "Telegram включён", "Нужен токен бота от @BotFather — вставь его в «Связи». Долгий опрос: в простое ни ЦП, ни записи."),
   Field("notify", "bool", True, "Напоминания и тревоги — сюда", "Напоминания, тревоги, итоги задач и пульса приходят сообщением."),
   Field("approvals", "bool", True, "Одобрения и вопросы — кнопками", "Карточки Предохранителя и вопросы агента приходят с кнопками."),
   Field("api_base", "str", "https://api.telegram.org", "Адрес Bot API", "Свой сервер Bot API или зеркало, если api.telegram.org недоступен."))

_g("mail", "Почта", "Ящик хозяина по IMAP/SMTP. Новые письма разбирает Чтец; сами письма агент читает, только когда нужно.",
   Field("enabled", "bool", False, "Почта включена", "Ящик открывается только для чтения: у тебя письма не становятся прочитанными."),
   Field("address", "str", "", "Адрес", "Твой ящик, например me@example.com."),
   Field("login", "str", "", "Логин", "Пусто — как адрес."),
   Field("name", "str", "", "Имя в письмах", "Пусто — имя агента."),
   Field("imap_host", "str", "", "Сервер входящих (IMAP)", ""),
   Field("imap_port", "int", 993, "Порт IMAP", "", min=1, max=65535),
   Field("imap_security", "choice", "ssl", "Защита IMAP", "", {"ssl": "SSL/TLS", "starttls": "STARTTLS", "none": "без шифрования"}),
   Field("smtp_host", "str", "", "Сервер исходящих (SMTP)", "Пусто — агент не сможет отправлять."),
   Field("smtp_port", "int", 465, "Порт SMTP", "", min=1, max=65535),
   Field("smtp_security", "choice", "ssl", "Защита SMTP", "", {"ssl": "SSL/TLS", "starttls": "STARTTLS", "none": "без шифрования"}),
   Field("folder", "str", "INBOX", "Папка", "Какую папку смотреть."),
   Field("poll_minutes", "int", 5, "Проверять раз в, минут", "Короткое соединение раз в N минут; в промежутках — ни сети, ни записи.", min=1, max=1440),
   Field("triage", "bool", True, "Чтец разбирает новые письма", "Спам, важное, ждёт ли ответа — дешёвой моделью, без рук. Выключено — письма просто складываются."),
   Field("on_new", "choice", "important", "Что делать с новым письмом", "",
         {"quiet": "только складывать", "important": "сказать мне о важных", "all": "сказать мне о каждом", "agent": "отдать агенту (он решит сам)"}),
   Field("known", "json", {}, "Знакомые адреса", "Адрес → как называть: {\"boss@work.ru\": \"начальник\"}. Агент видит подпись вместо адреса."),
   Field("owner_addresses", "json", [], "Мои другие адреса", "Письма на них агент шлёт без вопроса (как сообщение хозяину)."))

_g("team_roles", "Роли команды", "Свои роли и изменения встроенных. Агент предлагает; хозяин одобряет. Настройка — «Задачи → Роли».")

_g("team", "Команда", "Субагенты: агент зовёт помощников на своих моделях, с урезанными руками и бюджетом.",
   Field("enabled", "bool", True, "Звать субагентов", "Выключено — агент работает один.", weakens=_turned_on),
   Field("parallel", "int", 0, "Сколько сразу", "0 — сам: на телефоне 2, на компьютере 4.", min=0, max=16),
   Field("max_steps", "int", 15, "Шагов на задачу", "", min=1, max=100),
   Field("max_seconds", "int", 600, "Времени на задачу, секунд", "", min=10, max=7200),
   Field("max_usd", "float", 0.0, "Денег на задачу, $", "0 — без отдельного лимита (действует дневной бюджет).", min=0))

_g("api", "API", "Бендер как «модель» для других программ: OpenAI-совместимый /v1/chat/completions на порту пульта.",
   Field("enabled", "bool", False, "API включено", "Вход — ключом из «Связи». Пульт слушает только этот компьютер, если не включено «Пускать из локальной сети»."))

_g("web", "Пульт", "",
   Field("port", "int", 8770, "Порт", "", min=1, max=65535, restart=True),
   Field("listen", "bool", False, "Пускать из локальной сети", "Вход — по ссылке с токеном.", weakens=_turned_on, restart=True),
   Field("open_browser", "bool", True, "Открывать браузер при запуске", ""))

# Провайдеры и роли моделей — словари со своей проверкой (router/config.py)
_g("providers", "Провайдеры", "Откуда брать модели.")
_g("roles", "Роли моделей", "Какая модель за что отвечает и кто её подменяет.")
_g("guard_rules", "Правила Предохранителя", "«Всегда такое» и «никогда такое» от хозяина.")
_g("tools", "Тулы", "Выключатель каждого тула: имя → on/off.")


def _increased(old, new):
    """Ноль — без лимита; для ограничений длительности/числа/денег хода."""
    return old != 0 and (new == 0 or new > old)


# Права — часть той же схемы, из неё рисуются значки пульта.
OWNER_GROUPS = {'workshop', 'updates', 'backups'}
for key in ('enabled', 'auto_speak', 'telegram_reply'):
    SCHEMA['voice'].field(key).weakens = None
for group, keys in {
    'agent': ('max_steps', 'max_turn_seconds', 'max_turn_cost'),
    'team': ('max_steps', 'max_seconds', 'max_usd'),
    'budget': ('daily_usd',),
}.items():
    for key in keys: SCHEMA[group].field(key).weakens = _increased
for group in ('mcp', 'modules'):
    for f in SCHEMA[group].fields: f.weakens = lambda old, new: old != new
for group, keys in {
    'team': ('parallel',), 'sleep': ('budget_usd',), 'search': ('per_minute',),
    'skills': ('max_files', 'max_total_mb', 'timeout_seconds'),
}.items():
    for key in keys: SCHEMA[group].field(key).weakens = lambda old, new: new > old
SCHEMA['team'].field('parallel').weakens = lambda old, new: new != old and (new == 0 or old == 0 or new > old)
for group, keys in {
    'telegram': ('api_base', 'enabled'), 'mail': ('enabled', 'address', 'login', 'imap_host', 'imap_port', 'imap_security', 'smtp_host', 'smtp_port', 'smtp_security', 'owner_addresses'),
    'search': ('searxng_url', 'brave_url', 'tavily_url', 'brave_secret', 'tavily_secret'),
}.items():
    for key in keys: SCHEMA[group].field(key).weakens = lambda old, new: old != new


class NeedsOwner(HumanError):
    def __init__(self, group: str, changes: dict, reasons: list[str]):
        super().__init__("needs_owner", "Это может разрешить только хозяин", "; ".join(reasons), status=403,
                         data={"group": group, "changes": changes, "reasons": reasons})
        self.group = group
        self.changes = changes


def _coerce(f: Field, value):
    t = f.type
    try:
        if t == "bool":
            if isinstance(value, str):
                v = value.strip().lower()
                if v in ("1", "true", "да", "yes", "on", "вкл"):
                    return True
                if v in ("0", "false", "нет", "no", "off", "выкл"):
                    return False
                raise ValueError
            return bool(value)
        if t == "int":
            value = int(value)
        elif t == "float":
            value = float(value)
            if value!=value or value in (float("inf"),float("-inf")):raise ValueError
        elif t in ("str", "choice"):
            value = str(value)
        elif t == "json":
            return value
    except (TypeError, ValueError, OverflowError):
        raise HumanError("bad_setting", f"Неподходящее значение для «{f.title}»", f"Ожидается {t}.") from None
    if t == "choice" and f.choices and value not in f.choices:
        raise HumanError("bad_setting", f"Нет такого варианта для «{f.title}»", "Варианты: " + ", ".join(f"{k} ({v})" for k, v in f.choices.items()))
    if f.min is not None and value < f.min:
        raise HumanError("bad_setting", f"«{f.title}»: слишком мало", f"Минимум {f.min:g}.")
    if f.max is not None and value > f.max:
        raise HumanError("bad_setting", f"«{f.title}»: слишком много", f"Максимум {f.max:g}.")
    return value


class Settings:
    def __init__(self, store: Store, on_change: Callable[[str, dict], None] | None = None):
        self.store = store
        self.on_change = on_change
        self._cache: dict[str, dict] = {}
        for row in store.q('SELECT grp FROM settings'):
            self._stored(row['grp'])

    def _stored(self, group: str) -> dict:
        if group in self._cache:
            return self._cache[group]
        row = self.store.one("SELECT value FROM settings WHERE grp = ?", (group,))
        try:
            data = json.loads(row["value"]) if row else {}
            if not isinstance(data, dict):raise ValueError('Нужен объект группы')
            schema = SCHEMA.get(group)
            if schema:
                for field in schema.fields:
                    if field.key in data:data[field.key] = _coerce(field, data[field.key])
        except (ValueError, TypeError, HumanError) as exc:
            raise HumanError('settings_corrupt', 'Повреждены настройки группы «'+group+'»', 'Исходные данные сохранены. Проверь doctor и восстанови настройки из бекапа.', detail=str(exc)) from None
        self._cache[group] = data
        return data

    def group(self, group: str) -> dict:
        g = SCHEMA.get(group)
        stored = dict(self._stored(group))
        if g is None or not g.fields:
            return stored
        out = {f.key: f.default for f in g.fields}
        out.update(stored)  # незнакомые ключи будущих версий тоже остаются
        return out

    def get(self, group: str, key: str, default=None):
        return self.group(group).get(key, default)

    def weakening(self, group: str, changes: dict) -> list[str]:
        g = SCHEMA.get(group)
        if not g:
            return []
        cur = self.group(group)
        reasons = []
        for k, v in changes.items():
            f = g.field(k)
            if group == 'skills' and k == 'disabled':
                if not isinstance(v, list) or any(not isinstance(x, str) for x in v):
                    raise HumanError('bad_setting', 'Выключенные навыки — список имён', '')
                removed = set(cur.get(k) or []) - set(v)
                trust = self.group('skill_trust')
                stats = self.store.meta_get('skills.stats', {})
                if all(trust.get(name, {}).get('hash') and (trust.get(name, {}).get('origin') == 'experience' or stats.get(name, {}).get('source') == 'experience') for name in removed):
                    continue
            if f and f.weakens and f.weakens(cur.get(k), v):
                reasons.append(f"«{f.title}»: {self._show(f, cur.get(k))} → {self._show(f, v)}")
        return reasons

    @staticmethod
    def _show(f: Field, v) -> str:
        if f.type == "bool":
            return "вкл" if v else "выкл"
        if f.choices and v in f.choices:
            return f.choices[v]
        return str(v)

    def set(self, group: str, changes: dict, *, by: str = "owner") -> dict:
        """Изменить настройки группы. by: owner (хозяин в пульте/подтверждением) или agent.
        Возвращает {ключ: (было, стало)}."""
        if by != "owner" and group in OWNER_GROUPS:
            if group == "backups" and set(changes) == {"include_secrets"}:
                raise NeedsOwner(group, changes, ["Включение секретов в открытый бекап выбирает хозяин"])
            if group == 'updates' and set(changes) == {'auto'}:
                raise NeedsOwner(group, changes, ['Режим обычных обновлений выбирает хозяин; night разрешает менять программу ночью'])
            raise HumanError("owner_only", "Настройками версий и Мастерской управляет только хозяин", "Пульт → Настройки. Агент не меняет доверие или условия проверки через settings.")
        g = SCHEMA.get(group)
        if g is None:
            raise HumanError("bad_setting", f"Нет группы настроек «{group}»", "")
        if group == "roles":
            from ..router.config import role_changes
            changes, reasons = role_changes(self, changes)
            if by != "owner" and reasons: raise NeedsOwner(group, changes, reasons)
        if by != "owner" and not g.fields and group != "roles":
            # словари-группы (правила Предохранителя, провайдеры, роли) агент меняет только своими тулами,
            # а правила Предохранителя — никогда
            raise NeedsOwner(group, dict(changes), [f"«{g.title}» меняет только хозяин"])
        clean = {}
        if g.fields:
            for k, v in changes.items():
                f = g.field(k)
                if f is None:
                    raise HumanError("bad_setting", f"Нет настройки «{group}.{k}»", "Посмотри список в «Настройках».")
                clean[k] = _coerce(f, v)
        else:
            clean = dict(changes)
        if by != "owner":
            reasons = self.weakening(group, clean)
            if reasons:
                raise NeedsOwner(group, clean, reasons)
        if group == "team_roles":
            from ..team.roles import validate_roles

            validate_roles({**self.group(group), **clean})
        cur_full = self.group(group)
        stored = dict(self._stored(group))
        diff = {}
        for k, v in clean.items():
            if cur_full.get(k) != v:
                diff[k] = (cur_full.get(k), v)
            stored[k] = v
        if not diff:
            return {}
        self.store.x("INSERT INTO settings(grp, value, updated_at) VALUES(?,?,?) ON CONFLICT(grp) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                     (group, dumps(stored), time.time()))
        self._cache[group] = stored
        if self.on_change:
            self.on_change(group, {k: v[1] for k, v in diff.items()})
        return diff

    def replace(self, group: str, value: dict) -> None:
        """Записать группу-словарь целиком (провайдеры, роли)."""
        self.store.x("INSERT INTO settings(grp, value, updated_at) VALUES(?,?,?) ON CONFLICT(grp) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                     (group, dumps(value), time.time()))
        self._cache[group] = dict(value)
        if self.on_change:
            self.on_change(group, value)

    @staticmethod
    def permission(group, f):
        if group in OWNER_GROUPS:
            return 'ask' if (group, f.key) in (('updates', 'auto'), ('backups', 'include_secrets')) else 'owner'
        return 'ask' if f.weakens else 'free'

    def describe(self) -> list[dict]:
        """Схема для пульта: группы, поля, текущие значения."""
        out = []
        for name, g in SCHEMA.items():
            if not g.fields:
                continue
            cur = self.group(name)
            out.append({
                "name": name, "title": g.title, "hint": g.hint,
                "fields": [{"key": f.key, "type": f.type, "title": f.title, "hint": f.hint, "value": cur.get(f.key), "default": f.default,
                            "choices": f.choices, "min": f.min, "max": f.max, "guarded": f.weakens is not None, "restart": f.restart, "permission": self.permission(name, f)} for f in g.fields],
            })
        return out
