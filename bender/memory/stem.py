"""Основы слов для поиска по памяти: русский — алгоритм Snowball (snowballstem.org/algorithms/russian),
английский — лёгкое отсечение окончаний. Свой велосипед без зависимостей.

Для поиска важна не лингвистическая точность, а одинаковость: запрос и индекс проходят через
одну и ту же функцию, поэтому «памяти», «памятью» и «память» сходятся в «памят».
"""

from __future__ import annotations

import re
from functools import lru_cache

VOWELS = set("аеиоуыэюя")

PERFECTIVE_GERUND_1 = ("вшись", "вши", "в")                     # после а/я
PERFECTIVE_GERUND_2 = ("ившись", "ывшись", "ивши", "ывши", "ив", "ыв")
ADJECTIVE = ("ими", "ыми", "его", "ого", "ему", "ому", "ее", "ие", "ые", "ое", "ей", "ий", "ый", "ой", "ем", "им", "ым", "ом",
             "их", "ых", "ую", "юю", "ая", "яя", "ою", "ею")
PARTICIPLE_1 = ("ем", "нн", "вш", "ющ", "щ")                     # после а/я
PARTICIPLE_2 = ("ивш", "ывш", "ующ")
REFLEXIVE = ("ся", "сь")
VERB_1 = ("ете", "йте", "ешь", "нно", "ла", "на", "ли", "ем", "ло", "но", "ет", "ют", "ны", "ть", "й", "л", "н")  # после а/я
VERB_2 = ("ейте", "уйте", "ила", "ыла", "ена", "ите", "или", "ыли", "ило", "ыло", "ено", "ует", "уют", "ены", "ить", "ыть", "ишь",
          "ей", "уй", "ил", "ыл", "им", "ым", "ен", "ят", "ит", "ыт", "ую", "ю")
NOUN = ("иями", "ями", "ами", "ией", "иям", "ием", "иях", "ев", "ов", "ие", "ье", "еи", "ии", "ей", "ой", "ий", "ям", "ем", "ам", "ом",
        "ах", "ях", "ию", "ью", "ия", "ья", "а", "е", "и", "й", "о", "у", "ы", "ь", "ю", "я")
SUPERLATIVE = ("ейше", "ейш")
DERIVATIONAL = ("ость", "ост")


def _regions(w: str) -> tuple[int, int]:
    """RV — после первой гласной; R2 — R1 внутри R1 (R1 — после первой согласной, идущей за гласной)."""
    rv = len(w)
    for i, ch in enumerate(w):
        if ch in VOWELS:
            rv = i + 1
            break

    def after_vc(start: int) -> int:
        for i in range(start + 1, len(w)):
            if w[i] not in VOWELS and w[i - 1] in VOWELS:
                return i + 1
        return len(w)

    r1 = after_vc(0)
    r2 = after_vc(r1) if r1 < len(w) else len(w)
    return rv, r2


def _longest(w: str, start: int, endings: tuple[str, ...]) -> str | None:
    best = None
    for e in endings:
        if w.endswith(e) and len(w) - len(e) >= start and (best is None or len(e) > len(best)):
            best = e
    return best


def _cut_group(w: str, rv: int, g1: tuple[str, ...], g2: tuple[str, ...]) -> str | None:
    """Окончание из двух групп: первая — только после «а» или «я» (они остаются). Самое длинное совпадение решает."""
    e1 = _longest(w, rv, g1)
    e2 = _longest(w, rv, g2)
    if e1 and (not e2 or len(e1) > len(e2)):
        i = len(w) - len(e1)
        if i - 1 >= rv and w[i - 1] in "ая":
            return w[:i]
        return None
    if e2:
        return w[: len(w) - len(e2)]
    return None


def stem_ru(word: str) -> str:
    w = word.lower().replace("ё", "е")
    rv, r2 = _regions(w)
    if rv >= len(w):
        return w
    # шаг 1
    cut = _cut_group(w, rv, PERFECTIVE_GERUND_1, PERFECTIVE_GERUND_2)
    if cut is not None:
        w = cut
    else:
        e = _longest(w, rv, REFLEXIVE)
        if e:
            w = w[: -len(e)]
        e = _longest(w, rv, ADJECTIVE)
        if e:
            w = w[: -len(e)]
            p = _cut_group(w, rv, PARTICIPLE_1, PARTICIPLE_2)
            if p is not None:
                w = p
        else:
            v = _cut_group(w, rv, VERB_1, VERB_2)
            if v is not None:
                w = v
            else:
                e = _longest(w, rv, NOUN)
                if e:
                    w = w[: -len(e)]
    # шаг 2
    if w.endswith("и") and len(w) - 1 >= rv:
        w = w[:-1]
    # шаг 3
    e = _longest(w, max(r2, rv), DERIVATIONAL)
    if e:
        w = w[: -len(e)]
    # шаг 4
    e = _longest(w, rv, SUPERLATIVE)
    if e:
        w = w[: -len(e)]
        if w.endswith("нн") and len(w) - 2 >= rv:
            w = w[:-1]
    elif w.endswith("нн") and len(w) - 2 >= rv:
        w = w[:-1]
    elif w.endswith("ь") and len(w) - 1 >= rv:
        w = w[:-1]
    return w


def stem_en(word: str) -> str:
    w = word.lower()
    if len(w) <= 3:
        return w
    if w.endswith("'s"):
        w = w[:-2]
    for suf, rep in (("ational", "ate"), ("ization", "ize"), ("fulness", "ful"), ("ousness", "ous"), ("iveness", "ive"),
                     ("ies", "y"), ("sses", "ss"), ("ness", ""), ("ment", ""), ("ingly", ""), ("edly", ""),
                     ("ing", ""), ("ed", ""), ("ly", ""), ("es", ""), ("s", "")):
        if w.endswith(suf) and len(w) - len(suf) >= 3:
            if suf == "s" and w.endswith(("ss", "us", "is")):
                continue
            if suf == "es" and not w[:-2].endswith(("s", "x", "z", "ch", "sh")):
                continue
            w = w[: len(w) - len(suf)] + rep
            break
    if len(w) > 3 and w[-1] == w[-2] and w[-1] not in "lsz":
        w = w[:-1]  # running → run
    return w


WORD = re.compile(r"[0-9a-zа-яё]+(?:[-'][0-9a-zа-яё]+)*", re.I)

STOP = set("""
и в во не что он на я с со как а то все она так его но да ты к у же вы за бы по только ее мне было вот от меня еще нет о из ему
теперь когда даже ну вдруг ли если уже или ни быть был него до вас нибудь опять уж вам ведь там потом себя ничего ей может они
тут где есть надо ней для мы тебя их чем была сам чтоб без будто чего раз тоже себе под будет ж тогда кто этот того потому этого
какой совсем ним здесь этом один почти мой тем чтобы нее сейчас были куда зачем всех никогда можно при наконец два об другой хоть
после над больше тот через эти нас про всего них какая много разве три эту моя впрочем хорошо свою этой перед иногда лучше чуть
том нельзя такой им более всегда конечно всю между это
the a an and or but if of to in on at by for with from as is are was were be been being it its this that these those
i you he she we they me him her us them my your our their not no do does did so than then there here what which who whom
""".split())


@lru_cache(maxsize=20000)
def stem(word: str) -> str:
    w = word.lower().replace("ё", "е")
    if any("а" <= ch <= "я" for ch in w):
        return stem_ru(w)
    if w.isdigit():
        return w
    return stem_en(w)


def words(text: str) -> list[str]:
    return [m.group(0).lower().replace("ё", "е") for m in WORD.finditer(text or "")]


def stems(text: str, *, keep_stop: bool = False) -> list[str]:
    """Основы всех значимых слов текста (по порядку, с повторами)."""
    out = []
    for w in words(text):
        if not keep_stop and w in STOP:
            continue
        s = stem(w)
        if s:
            out.append(s)
    return out
