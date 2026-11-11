"""Cron-выражения: пять полей (минута, час, день месяца, месяц, день недели) и псевдонимы @daily и т. п.

Поддерживается всё, что пишут люди и OpenClaw: `*`, числа, диапазоны `1-5`, шаги `*/15` и `10-50/10`,
списки `1,15`, имена месяцев и дней (`jan`, `mon-fri`), воскресенье — 0 или 7. Если ограничены и день
месяца, и день недели — срабатывает, когда подходит любой из них (как в классическом cron).

Время — местное (или часовой пояс IANA, если он есть в системе). Ближайшее срабатывание ищется по дням,
а не по минутам: даже «29 февраля в понедельник» находится за доли миллисекунды.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from ..core.errors import HumanError

MONTHS = {m: i for i, m in enumerate("jan feb mar apr may jun jul aug sep oct nov dec".split(), 1)}
DAYS = {d: i for i, d in enumerate("sun mon tue wed thu fri sat".split())}
ALIASES = {"@yearly": "0 0 1 1 *", "@annually": "0 0 1 1 *", "@monthly": "0 0 1 * *", "@weekly": "0 0 * * 0",
           "@daily": "0 0 * * *", "@midnight": "0 0 * * *", "@hourly": "0 * * * *"}
FIELDS = (("минута", 0, 59, None), ("час", 0, 23, None), ("день месяца", 1, 31, None), ("месяц", 1, 12, MONTHS), ("день недели", 0, 7, DAYS))
HORIZON_DAYS = 366 * 8  # дальше не ищем: «31 февраля» не наступит никогда


@dataclass(frozen=True)
class Cron:
    expr: str
    minutes: frozenset
    hours: frozenset
    days: frozenset
    months: frozenset
    weekdays: frozenset   # 0 — воскресенье
    any_day: bool         # день месяца — «*»
    any_weekday: bool     # день недели — «*»

    def day_matches(self, d: dt.date) -> bool:
        if d.month not in self.months:
            return False
        wd = (d.weekday() + 1) % 7  # Python: понедельник — 0; cron: воскресенье — 0
        dom, dow = d.day in self.days, wd in self.weekdays
        if self.any_day and self.any_weekday:
            return True
        if self.any_day:
            return dow
        if self.any_weekday:
            return dom
        return dom or dow

    def next_after(self, ts: float, tz: dt.tzinfo | None = None) -> float | None:
        """Ближайшее срабатывание строго после ts (секунды эпохи). None — не наступит."""
        start = dt.datetime.fromtimestamp(ts, tz).replace(second=0, microsecond=0) + dt.timedelta(minutes=1)
        day = start.date()
        hours = sorted(self.hours)
        minutes = sorted(self.minutes)
        for i in range(HORIZON_DAYS):
            d = day + dt.timedelta(days=i)
            if not self.day_matches(d):
                continue
            for h in hours:
                if i == 0 and h < start.hour:
                    continue
                for m in minutes:
                    if i == 0 and h == start.hour and m < start.minute:
                        continue
                    cand = dt.datetime(d.year, d.month, d.day, h, m, tzinfo=tz)
                    t = cand.timestamp()  # без tz — местное время; несуществующее (перевод часов) сдвинется вперёд
                    if t > ts:
                        return t
        return None

    def describe(self) -> str:
        return describe(self)


def _num(tok: str, names: dict | None, name: str, expr: str) -> int:
    t = tok.strip().lower()
    if names and t[:3] in names and t.isalpha():
        return names[t[:3]]
    if not t.isdigit():
        raise HumanError("bad_cron", f"Не понял cron «{expr}»", f"Поле «{name}»: «{tok}» — не число.")
    return int(t)


def _field(text: str, name: str, lo: int, hi: int, names: dict | None, expr: str) -> set[int]:
    out: set[int] = set()
    for part in text.split(","):
        if not part:
            raise HumanError("bad_cron", f"Не понял cron «{expr}»", f"Поле «{name}»: пустой элемент списка.")
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            if not s.isdigit() or int(s) < 1:
                raise HumanError("bad_cron", f"Не понял cron «{expr}»", f"Поле «{name}»: шаг «{s}» должен быть числом больше 0.")
            step = int(s)
        if part == "*":
            a, b = lo, hi
        elif "-" in part:
            x, y = part.split("-", 1)
            a, b = _num(x, names, name, expr), _num(y, names, name, expr)
        else:
            a = _num(part, names, name, expr)
            b = hi if step > 1 else a
        if not (lo <= a <= hi and lo <= b <= hi):
            raise HumanError("bad_cron", f"Не понял cron «{expr}»", f"Поле «{name}»: значения от {lo} до {hi}.")
        if a > b:
            raise HumanError("bad_cron", f"Не понял cron «{expr}»", f"Поле «{name}»: диапазон {a}-{b} задом наперёд.")
        out.update(range(a, b + 1, step))
    return out


def parse(expr: str) -> Cron:
    raw = (expr or "").strip()
    text = ALIASES.get(raw.lower(), raw)
    parts = text.split()
    if len(parts) != 5:
        raise HumanError("bad_cron", f"Не понял cron «{raw}»", "Нужно пять полей: минута час день месяц день_недели. Например, «0 9 * * 1-5» — в 9:00 по будням.")
    sets = [_field(p, n, lo, hi, names, raw) for p, (n, lo, hi, names) in zip(parts, FIELDS)]
    wd = {0 if x == 7 else x for x in sets[4]}
    return Cron(raw, frozenset(sets[0]), frozenset(sets[1]), frozenset(sets[2]), frozenset(sets[3]), frozenset(wd),
                parts[2] == "*", parts[4] == "*")


DAY_WORDS = ["воскресенье", "понедельник", "вторник", "среда", "четверг", "пятница", "суббота"]


def describe(c: Cron) -> str:
    """Коротко по-русски для пульта: «в 09:00 по будням», «каждые 15 минут»."""
    p = c.expr.split()
    if c.expr.lower() in ALIASES:
        p = ALIASES[c.expr.lower()].split()
    m, h, dom, mon, dow = (p + ["*"] * 5)[:5]
    if m.startswith("*/") and h == "*" and dom == mon == dow == "*":
        return f"каждые {m[2:]} мин"
    if m.isdigit() and h == "*" and dom == mon == dow == "*":
        return f"каждый час в :{int(m):02d}"
    if m.isdigit() and h.isdigit():
        when = f"в {int(h):02d}:{int(m):02d}"
        if dom == mon == "*":
            if dow == "*":
                return f"{when} каждый день"
            if sorted(c.weekdays) == [1, 2, 3, 4, 5]:
                return f"{when} по будням"
            if sorted(c.weekdays) == [0, 6]:
                return f"{when} по выходным"
            return f"{when}: " + ", ".join(DAY_WORDS[d] for d in sorted(c.weekdays))
        if mon == "*" and dow == "*" and dom.isdigit():
            return f"{when} {dom}-го числа каждый месяц"
    return f"cron «{c.expr}»"
