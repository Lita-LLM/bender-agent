"""Время словами → момент: «через 20 минут», «завтра в 9», «в пятницу 18:30», «2026-10-23 09:00», «+1h30m».

Модель может прислать время по-разному — понимаем всё разумное, а непонятное возвращаем ошибкой
с примерами, чтобы модель переспросила себя, а не поставила напоминание «на когда-нибудь».
Интервалы («каждые 2 часа», «30m», «1d») — every().
"""

from __future__ import annotations

import datetime as dt
import re
import time

from ..core.errors import HumanError

UNITS = {
    "s": 1, "sec": 1, "сек": 1, "секунд": 1, "секунду": 1, "секунды": 1,
    "m": 60, "min": 60, "мин": 60, "минут": 60, "минуту": 60, "минуты": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hour": 3600, "hours": 3600, "ч": 3600, "час": 3600, "часа": 3600, "часов": 3600,
    "d": 86400, "day": 86400, "days": 86400, "д": 86400, "дн": 86400, "день": 86400, "дня": 86400, "дней": 86400, "сутки": 86400, "суток": 86400,
    "w": 604800, "week": 604800, "weeks": 604800, "нед": 604800, "неделю": 604800, "недели": 604800, "недель": 604800,
}
WEEKDAYS = {"пн": 0, "понедельник": 0, "mon": 0, "monday": 0, "вт": 1, "вторник": 1, "tue": 1, "tuesday": 1,
            "ср": 2, "среда": 2, "среду": 2, "wed": 2, "wednesday": 2, "чт": 3, "четверг": 3, "thu": 3, "thursday": 3,
            "пт": 4, "пятница": 4, "пятницу": 4, "fri": 4, "friday": 4, "сб": 5, "суббота": 5, "субботу": 5, "sat": 5, "saturday": 5,
            "вс": 6, "воскресенье": 6, "sun": 6, "sunday": 6}
WORD_NUM = {"полчаса": (30, "m"), "полтора часа": (90, "m"), "час": (1, "h"), "минуту": (1, "m"), "день": (1, "d"), "неделю": (1, "w"), "сутки": (1, "d")}
EXAMPLES = "Примеры: «через 20 минут», «+2h», «завтра в 9:00», «в пятницу 18:30», «23.10 10:00», «2026-10-23 09:00»."
DEFAULT_HOUR = 9


def _span(text: str) -> float | None:
    """«1h30m», «20 минут», «2 часа 15 минут», «полчаса» → секунды."""
    t = text.strip().lower().replace(",", " ")
    for w, (n, u) in WORD_NUM.items():
        if t == w:
            return n * UNITS[u]
    total = 0.0
    pos = 0
    found = False
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*([a-zа-я]+)", t):
        if t[pos:m.start()].strip(" и"):
            return None
        unit = m.group(2)
        k = UNITS.get(unit) or next((v for u, v in UNITS.items() if len(unit) > 2 and unit.startswith(u) and len(u) >= 3), None)
        if not k:
            return None
        total += float(m.group(1).replace(",", ".")) * k
        pos = m.end()
        found = True
    if not found or t[pos:].strip():
        return None
    return total


def every(text: str) -> int:
    """Интервал повтора в секундах: «30m», «каждые 2 часа», «1d». Не меньше минуты."""
    t = re.sub(r"^(каждые|каждый|каждую|каждое|every)\s+", "", (text or "").strip().lower())
    if t.isdigit():
        t += "m"
    sec = _span(t)
    if sec is None:
        sec = _span("1 " + t)  # «каждый час», «каждую неделю»
    if sec is None:
        raise HumanError("bad_every", f"Не понял интервал «{text}»", "Примеры: «30m», «2h», «1d», «каждые 15 минут».")
    if sec < 60:
        raise HumanError("bad_every", "Слишком часто", "Чаще раза в минуту задачи не запускаются — Бендер бережёт машину.")
    return int(sec)


def _clock(text: str) -> tuple[int, int] | None:
    m = re.fullmatch(r"(?:в\s+|at\s+)?(\d{1,2})(?:[:.](\d{2}))?(?:\s*(утра|дня|вечера|ночи|am|pm))?", text.strip())
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2) or 0)
    part = m.group(3)
    if part in ("вечера", "дня", "pm") and h < 12:
        h += 12
    if part in ("ночи", "am") and h == 12:
        h = 0
    if h > 23 or mi > 59:
        return None
    return h, mi


def parse(text: str, now: float | None = None) -> float:
    """Момент времени (секунды эпохи, местное время). Прошедшее — ошибка."""
    now = now if now is not None else time.time()
    raw = (text or "").strip()
    t = raw.lower().replace("ё", "е")
    t = re.sub(r"\s+", " ", t)
    base = dt.datetime.fromtimestamp(now)
    res: dt.datetime | None = None

    # относительное: «+20m», «через 2 часа», «in 15 minutes»
    m = re.fullmatch(r"(?:\+|через |in )\s*(.+?)(?: later)?", t)
    if m:
        sec = _span(m.group(1))
        if sec is not None:
            return now + sec
    # ISO и почти ISO
    try:
        iso = dt.datetime.fromisoformat(raw.replace("Z", "+00:00").replace(" ", "T", 1) if re.match(r"\d{4}-\d{2}-\d{2}", raw) else "")
        if iso.tzinfo is not None:
            res_ts = iso.timestamp()
            if res_ts <= now:
                raise HumanError("past_time", "Это время уже прошло", f"«{raw}». {EXAMPLES}")
            return res_ts
        if len(raw) <= 10:
            iso = iso.replace(hour=DEFAULT_HOUR)
        res = iso
    except ValueError:
        pass
    if res is None:
        day: dt.date | None = None
        rest = t
        for word, add in (("послезавтра", 2), ("завтра", 1), ("сегодня", 0), ("tomorrow", 1), ("today", 0)):
            if rest.startswith(word):
                day = base.date() + dt.timedelta(days=add)
                rest = rest[len(word):].strip()
                break
        if day is None:
            m = re.match(r"(?:в |во |on |next )?([a-zа-я]+)\b\s*(.*)$", rest)
            if m and m.group(1) in WEEKDAYS:
                wd = WEEKDAYS[m.group(1)]
                ahead = (wd - base.weekday()) % 7
                day = base.date() + dt.timedelta(days=ahead)
                rest = m.group(2).strip()
                if ahead == 0:
                    clock = _clock(rest) if rest else (DEFAULT_HOUR, 0)
                    if clock and dt.datetime.combine(day, dt.time(*clock)) <= base:
                        day += dt.timedelta(days=7)
        if day is None:
            m = re.match(r"(\d{1,2})[./](\d{1,2})(?:[./](\d{2,4}))?\s*(.*)$", rest)
            if m:
                y = int(m.group(3)) if m.group(3) else base.year
                if y < 100:
                    y += 2000
                try:
                    day = dt.date(y, int(m.group(2)), int(m.group(1)))
                except ValueError:
                    raise HumanError("bad_time", f"Нет такой даты: «{raw}»", EXAMPLES) from None
                if not m.group(3) and day < base.date():
                    day = day.replace(year=day.year + 1)
                rest = m.group(4).strip()
        rest = re.sub(r"^(в|at)\s+", "", rest)
        clock = _clock(rest) if rest else None
        if rest and clock is None:
            raise HumanError("bad_time", f"Не понял время «{raw}»", EXAMPLES)
        if day is None and clock is None:
            raise HumanError("bad_time", f"Не понял время «{raw}»", EXAMPLES)
        if day is None:  # только часы: сегодня, а если прошло — завтра
            day = base.date()
            if dt.datetime.combine(day, dt.time(*clock)) <= base:
                day += dt.timedelta(days=1)
        res = dt.datetime.combine(day, dt.time(*(clock or (DEFAULT_HOUR, 0))))
    ts = res.timestamp()
    if ts <= now:
        raise HumanError("past_time", "Это время уже прошло", f"«{raw}» — {res:%d.%m.%Y %H:%M}. {EXAMPLES}")
    return ts


def human(ts: float, now: float | None = None) -> str:
    """«сегодня в 18:30», «завтра в 09:00», «пт 24.10 в 10:00», «через 20 мин»."""
    now = now if now is not None else time.time()
    d = dt.datetime.fromtimestamp(ts)
    base = dt.datetime.fromtimestamp(now)
    left = ts - now
    if 0 <= left < 3600:
        return f"через {max(1, round(left / 60))} мин (в {d:%H:%M})"
    days = (d.date() - base.date()).days
    if days == 0:
        return f"сегодня в {d:%H:%M}"
    if days == 1:
        return f"завтра в {d:%H:%M}"
    wd = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"][d.weekday()]
    if 0 < days < 7:
        return f"{wd} {d:%d.%m} в {d:%H:%M}"
    return f"{d:%d.%m.%Y} в {d:%H:%M}"


def window_ok(window: str, ts: float) -> bool:
    """Окно активности «08:00-23:00» (через полночь — «22:00-06:00»). Пустое — всегда."""
    if not window:
        return True
    a, b = _window(window)
    t = dt.datetime.fromtimestamp(ts)
    cur = t.hour * 60 + t.minute
    return a <= cur < b if a <= b else (cur >= a or cur < b)


def next_window_start(window: str, ts: float) -> float:
    a, _ = _window(window)
    t = dt.datetime.fromtimestamp(ts)
    cand = t.replace(hour=a // 60, minute=a % 60, second=0, microsecond=0)
    if cand.timestamp() <= ts:
        cand += dt.timedelta(days=1)
    return cand.timestamp()


def _window(window: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*[-–—]\s*(\d{1,2}):(\d{2})\s*", window or "")
    if not m:
        raise HumanError("bad_window", f"Не понял окно «{window}»", "Пример: «08:00-23:00» или «22:00-06:00» (через полночь).")
    a = int(m.group(1)) * 60 + int(m.group(2))
    b = int(m.group(3)) * 60 + int(m.group(4))
    return a, b
