"""Русские мелочи для сообщений: склонение после числа."""

from __future__ import annotations


def plural(n: int, one: str, few: str, many: str) -> str:
    """1 файл, 2 файла, 5 файлов, 21 файл, 11 файлов."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def count(n: int, one: str, few: str, many: str) -> str:
    return f"{n} {plural(n, one, few, many)}"
