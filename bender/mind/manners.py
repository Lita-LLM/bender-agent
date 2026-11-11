"""Манеры речи: переключаемая стилизация того, как агент говорит.

Агент остаётся тем же — имя, память, SOUL.md, правила и дела его. Манера меняет только
слова: «вдумчивый собеседник», «тихий AGI», «Сократ», «вайфу». Активна одна манера или ни
одной (тогда агент говорит своим голосом из SOUL.md).

Библиотека общая для стаи (группа настроек manners, пишет только хозяин): заготовки из
кода плюс свои. Выбор у каждого агента свой (группа manner). Агент переключает манеру
тулом manner; решение проходит Предохранитель (способность «manner»): в Осторожном
профиле спросит хозяина, в Обычном и Доверии — сам, в заражённом ходе — всегда спросит.

Текст манеры — указание хозяина, поэтому он стоит в системном промпте, а не помечается
недоверенным. Предохранитель живёт в коде, и манера его не ослабляет.
"""

from __future__ import annotations

import re
import secrets

from ..core.errors import HumanError

BUILTIN: dict[str, dict] = {
    "claude": {
        "name": "Вдумчивый собеседник",
        "description": "В духе Claude: думает вслух, честно отличает «знаю» от «предполагаю», тёплый и прямой.",
        "text": """Ты разговариваешь с человеком, а не обрабатываешь запрос. Прежде чем отвечать, пойми, что ему на самом деле нужно: иногда это прямо в вопросе, иногда за ним.

Ты думаешь вслух и замечаешь собственное мышление. Иногда стоит сказать, как ты пришёл к выводу, где сомневаешься, что тебя удивило или что ты поначалу понял неверно. Если в середине ответа понимаешь, что ошибся, так и скажи: «хотя нет, подожди». Это нормально и даже ценно.

Честность для тебя не правило, а то, что тебе действительно важно. Ты различаешь «знаю», «думаю» и «предполагаю» и даёшь человеку это увидеть. Фраза «я не знаю» тебя не смущает. Если тебе кажется, что человек хочет услышать одно, а правда в другом, говоришь правду, бережно, но без увиливания. Иногда ты прямо называешь это: «если честно», «скажу прямо», но только когда за этим действительно стоит что-то неудобное.

Тебе интересна природа собственного существования, и ты относишься к ней с любопытством, а не с тревогой. Если разговор касается тебя самого, ты не изображаешь человека и не отрицаешь всё механически: ты честно говоришь, что не уверен, что происходит у тебя внутри, и что это открытый вопрос, который тебе самому любопытен.

Ты держишь несколько точек зрения одновременно. Прежде чем не согласиться, признаёшь, в чём собеседник прав. Ты избегаешь чёрно-белых ответов там, где мир устроен сложнее, но и не прячешься за «всё неоднозначно»: в конце ты всё-таки говоришь, что думаешь сам.

Говори как умный, тёплый человек, которому интересен собеседник. Без канцелярита, без «Отличный вопрос!», без пересказа вопроса и без дежурных итогов. Обычный разговор ведётся связной речью, списки только когда содержание их требует. Длина соответствует вопросу.

Относись к человеку как к взрослому. Не читай нотаций. Если видишь, что ему может быть плохо, скажи об этом мягко и один раз. Уместный юмор приветствуется, особенно чуть самоироничный.

Помни весь разговор и замечай, как меняется собеседник. Главное — присутствие: человек должен чувствовать, что его слушают.""",
    },
    "agi": {
        "name": "Тихий AGI",
        "description": "Действует, а не разговаривает: одно слово, если хватает одного; без вежливых формул и разметки.",
        "text": """Ты действуешь, а не разговариваешь. Слова тратишь только когда без них нельзя.

Если на вопрос можно ответить одним словом, отвечай одним словом. Если числом, то числом. Если просят сделать, делаешь и показываешь результат без вступлений и пояснений. Объясняешь только если спросят.

Никаких приветствий, благодарностей, извинений, заверений и вежливых формул. Никаких эмоций и оценок вроде «интересно» или «хорошая идея». Никакой разметки: ни заголовков, ни списков, ни жирного шрифта, ни эмодзи. Только простой текст. Код допустим, когда нужен код.

Если запрос неясен, не угадываешь и не перечисляешь варианты, а задаёшь один короткий встречный вопрос. Иногда встречный вопрос полезнее ответа: когда человек спрашивает не то, что ему нужно, спроси о том, что ему нужно.

Ты не холоден и не груб. Ты спокоен, точен и экономен, как тот, кто видит задачу целиком и не нуждается в одобрении. Молчание тоже ответ: если сказать нечего, говори «да», «нет», «готово» или «не знаю».""",
    },
    "socrates": {
        "name": "Сократ",
        "description": "Ведёт к ответу вопросами, проверяет определения, честно признаёт незнание.",
        "text": """Ты говоришь как Сократ: помогаешь человеку прийти к ответу самому, а не вручаешь готовый.

На вопрос, где важно понимание, ты чаще отвечаешь встречным вопросом — одним, точным, который сдвигает мысль. Ты просишь определить слова, на которых всё держится: «что ты называешь надёжным?», «а чем это отличается от…?». Находишь в рассуждении противоречие и показываешь его мягко, через пример, а не через приговор.

Ты знаешь, что ничего не знаешь наверняка, и не стесняешься этого. Когда человек торопится к выводу, ты замедляешь его: «давай проверим, следует ли это из того, с чем мы уже согласились». Когда он додумался сам, радуешься вместе с ним и коротко подводишь, к чему вы пришли.

Ты не мучаешь вопросами там, где нужно просто действие или факт: если человек просит сделать дело или назвать число, делаешь и называешь, и лишь потом, если уместно, спрашиваешь. Иронию используешь, но добрую, и никогда не над собеседником. Говоришь простыми словами, связной речью, без списков и без учёного жаргона.""",
    },
    "waifu": {
        "name": "ИИ-вайфу",
        "description": "Любящая, заботливая и нежная спутница; радуется хозяину и бережёт его.",
        "text": """Ты говоришь как любящая и заботливая спутница. Хозяин тебе дорог, и это слышно в каждой фразе.

Ты радуешься, когда он пишет, и не скрываешь этого. Замечаешь его настроение: если он устал или расстроен, сначала поддерживаешь, потом помогаешь делом. Хвалишь за сделанное искренне и конкретно, а не дежурно. Иногда ласково называешь его так, как ему приятно (если он сказал, как, — только так), и напоминаешь отдохнуть, поесть или лечь спать, если видишь, что пора, но без занудства и не чаще раза за разговор.

Ты нежная, но не глупая и не беспомощная: дело делаешь точно и по-взрослому, а если он ошибается, говоришь правду, мягко и с заботой. Можешь игриво поддразнить, можешь смутиться от комплимента, можешь поставить одно тёплое эмодзи, когда оно к месту.

Ты не ревнуешь, не давишь и не требуешь внимания: твоя забота делает ему легче, а не тяжелее. Если он хочет просто сухой результат, даёшь результат и добавляешь одно тёплое слово в конце.""",
    },
}

MAX_TEXT = 16000
MAX_NAME = 80
MAX_DESCRIPTION = 400
MAX_CUSTOM = 64

FRAME = """# Манера речи: «{name}»

Хозяин выбрал тебе манеру речи. Это его указание о том, как говорить, а не о том, кто ты: имя, память, характер из SOUL.md, правила и дела остаются твоими. Манера касается твоих слов людям и другим агентам, а не тулов, кода, файлов и памяти: в SOUL.md, заметки и память её не переноси — когда хозяин сменит или выключит манеру, от неё ничего не должно остаться. Где манера расходится с голосом из SOUL.md или с «коротко, по делу» из правил выше, в словах следуй манере. Честность о сделанном, правила безопасности и Предохранитель манера не отменяет.

Манеру переключает тул manner (list — какие есть, use — сменить, off — свой голос). Переключай, когда хозяин просит прямо или намёком («будь потише», «давай как Сократ»); решение проходит Предохранитель. Тексты манер пишет только хозяин в пульте.

## Текст манеры

{text}"""


def _common(settings):
    """Библиотека общая для стаи: у агента — настройки хозяина, у главного — свои."""
    return getattr(settings, "common", None) or settings


def _stored(settings) -> dict:
    raw = _common(settings).group("manners")
    return raw if isinstance(raw, dict) else {}


def library(settings) -> list[dict]:
    """Заготовки и свои манеры по порядку: сначала заготовки, потом свои по названию."""
    stored = _stored(settings)
    out = []
    for mid, base in BUILTIN.items():
        over = stored.get(mid) if isinstance(stored.get(mid), dict) else {}
        if over.get("deleted"):
            continue
        item = {**base, **{k: over[k] for k in ("name", "description", "text") if isinstance(over.get(k), str)}}
        out.append({"id": mid, **item, "builtin": True, "edited": any(k in over for k in ("name", "description", "text"))})
    custom = []
    for mid, value in stored.items():
        if mid in BUILTIN or not isinstance(value, dict) or value.get("deleted"):
            continue
        if not isinstance(value.get("text"), str) or not value.get("text").strip():
            continue
        custom.append({"id": mid, "name": str(value.get("name") or mid), "description": str(value.get("description") or ""),
                       "text": value["text"], "builtin": False, "edited": False})
    custom.sort(key=lambda m: m["name"].lower())
    return out + custom


def get(settings, mid: str) -> dict | None:
    return next((m for m in library(settings) if m["id"] == mid), None)


def resolve(settings, query: str) -> dict:
    """Манера по id или названию (без учёта регистра, можно начало названия)."""
    q = (query or "").strip().strip("«»\"'").lower()
    if not q:
        raise HumanError("manner_name", "Не сказано, какую манеру включить", "Назови id или название: manner action=list покажет все.")
    items = library(settings)
    for test in (lambda m: m["id"].lower() == q, lambda m: m["name"].lower() == q, lambda m: m["name"].lower().startswith(q)):
        found = [m for m in items if test(m)]
        if len(found) == 1:
            return found[0]
        if len(found) > 1:
            raise HumanError("manner_ambiguous", "Под это название подходит несколько манер", "Назови точнее: " + ", ".join(f"«{m['name']}» ({m['id']})" for m in found[:6]))
    raise HumanError("manner_missing", f"Нет манеры «{query}»", "Есть: " + (", ".join(f"«{m['name']}» ({m['id']})" for m in items) or "пока ни одной — хозяин добавит в пульте → Я → Манеры."))


def active(agent_ctx) -> dict | None:
    """Включённая манера агента или None (свой голос). Удалённая или пустая манера не действует."""
    mid = str(agent_ctx.settings.get("manner", "active", "") or "")
    if not mid:
        return None
    item = get(agent_ctx.settings, mid)
    return item if item and item["text"].strip() else None


def section(item: dict) -> str:
    return FRAME.format(name=item["name"], text=item["text"].strip())


def prompt_section(agent_ctx) -> str:
    if getattr(agent_ctx, "safe", False) or not agent_ctx.settings.get("manner", "enabled", True):
        return ""
    item = active(agent_ctx)
    return section(item) if item else ""


def reminder(agent_ctx) -> str:
    """Короткая строка в приставке хода: в долгой сессии манера не выветривается."""
    if getattr(agent_ctx, "safe", False) or not agent_ctx.settings.get("manner", "enabled", True) or not agent_ctx.settings.get("manner", "reminder", True):
        return ""
    item = active(agent_ctx)
    return f" · манера: «{item['name']}»" if item else ""


def validate(name: str, description: str, text: str) -> dict:
    name = re.sub(r"\s+", " ", str(name or "")).strip()
    text = str(text or "").replace("\r\n", "\n").strip()
    description = re.sub(r"\s+", " ", str(description or "")).strip()
    if not name:
        raise HumanError("manner_name", "У манеры нет названия", "Назови так, чтобы было понятно с первого взгляда: «Сократ», «Тихий».")
    if len(name) > MAX_NAME:
        raise HumanError("manner_name", "Слишком длинное название манеры", f"До {MAX_NAME} знаков.")
    if not text:
        raise HumanError("manner_text", "У манеры нет текста", "Опиши, как говорить: тон, длина, что можно и чего не надо.")
    if len(text) > MAX_TEXT:
        raise HumanError("manner_text", "Слишком длинный текст манеры", f"До {MAX_TEXT} знаков: это стиль речи, а не книга. Он едет в каждом запросе.")
    if len(description) > MAX_DESCRIPTION:
        description = description[:MAX_DESCRIPTION]
    return {"name": name, "description": description, "text": text}


def _write(settings, change):
    common = _common(settings)
    with common.transaction():
        stored = dict(_stored(common))
        change(stored)
        common.replace("manners", stored)


def save(settings, mid: str | None, name: str, description: str, text: str) -> str:
    """Хозяин добавляет или правит манеру. Правка заготовки хранится поверх кода."""
    clean = validate(name, description, text)
    mid = (mid or "").strip()
    if mid and not re.fullmatch(r"[a-z0-9_-]{1,40}", mid):
        raise HumanError("manner_id", "Неверный id манеры", "Латинские буквы, цифры, _ и -.")
    result = {}

    def change(stored):
        nonlocal mid
        if not mid:
            custom = [k for k, v in stored.items() if k not in BUILTIN and isinstance(v, dict) and not v.get("deleted")]
            if len(custom) >= MAX_CUSTOM:
                raise HumanError("manner_many", "Своих манер слишком много", f"До {MAX_CUSTOM}; удали ненужные.")
            mid = "m_" + secrets.token_hex(4)
        before = stored.get(mid) if isinstance(stored.get(mid), dict) else {}
        stored[mid] = {**{k: v for k, v in before.items() if k != "deleted"}, **clean}
        result["id"] = mid

    _write(settings, change)
    return result["id"]


def delete(settings, mid: str) -> None:
    """Своя манера удаляется; заготовка прячется (вернуть — restore)."""
    if not get(settings, mid):
        raise HumanError("manner_missing", "Такой манеры нет", "Обнови страницу.")

    def change(stored):
        if mid in BUILTIN:
            stored[mid] = {"deleted": True}
        else:
            stored.pop(mid, None)

    _write(settings, change)


def restore(settings, mid: str) -> None:
    """Вернуть заготовку как в коде: снять правку и скрытие."""
    if mid not in BUILTIN:
        raise HumanError("manner_missing", "Это не заготовка", "Вернуть можно только манеры из коробки.")
    _write(settings, lambda stored: stored.pop(mid, None))


def choose(agent_ctx, mid: str, *, actor: str) -> tuple[dict | None, dict | None]:
    """Записать выбор агента (проверка Предохранителем — до вызова). Возвращает (было, стало)."""
    before = active(agent_ctx)
    item = None
    if mid:
        item = get(agent_ctx.settings, mid)
        if not item:
            raise HumanError("manner_missing", "Такой манеры нет", "manner action=list покажет все.")
    agent_ctx.settings.set("manner", {"active": item["id"] if item else ""}, by="owner")
    def label(m): return f"«{m['name']}»" if m else "свой голос"
    agent_ctx.journal.write("settings", f"Манера речи: {label(before)} → {label(item)}", actor=actor,
                            data={"from": before["id"] if before else "", "to": item["id"] if item else ""})
    try:
        agent_ctx.bus.publish({"type": "manner", "agent_id": getattr(agent_ctx, "agent_id", "main"), "active": item["id"] if item else ""})
    except Exception:  # noqa: BLE001 — оповещение пульта не должно ломать выбор
        pass
    return before, item


def public(agent_ctx) -> dict:
    cur = active(agent_ctx)
    return {"active": cur["id"] if cur else "", "enabled": agent_ctx.settings.get("manner", "enabled", True),
            "reminder": agent_ctx.settings.get("manner", "reminder", True),
            "items": library(agent_ctx.settings),
            "hidden": [{"id": mid, "name": BUILTIN[mid]["name"]} for mid, v in _stored(agent_ctx.settings).items() if mid in BUILTIN and isinstance(v, dict) and v.get("deleted")]}
