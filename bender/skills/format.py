"""Небольшое безопасное подмножество YAML для шапки SKILL.md, без зависимостей.

Словари, списки, строки, JSON/flow-значения, | и >. Нет объектов Python,
тегов, якорей, ссылок, вычислений и неявных дат. Незнакомые поля сохраняются.
"""

from __future__ import annotations

import json
import re

from ..core.errors import HumanError


def bad(message):
    return HumanError("skill_format", "Не удалось разобрать SKILL.md", message)


def split_flow(text, separator=","):
    parts, start, level, quote = [], 0, 0, ""
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if ch == "\\" and quote == '"':
                i += 2
                continue
            if ch == quote:
                if quote == "'" and i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch in "[{":
            level += 1
        elif ch in "]}":
            level -= 1
        elif ch == separator and level == 0:
            parts.append(text[start:i].strip())
            start = i + 1
        if level < 0:
            raise bad("Несогласованные скобки в шапке.")
        i += 1
    if quote or level:
        raise bad("Не закрыты кавычки или скобки в шапке.")
    parts.append(text[start:].strip())
    return parts


def scalar(text, depth=0):
    if depth > 20:
        raise bad("Слишком глубокая вложенность шапки.")
    text = text.strip()
    if text.startswith(("!", "&", "*")):
        raise bad("Теги, якоря и ссылки YAML не поддерживаются.")
    if not text or text in ("null", "Null", "NULL", "~"):
        return None
    if text in ("true", "True", "TRUE", "false", "False", "FALSE"):
        return text.lower() == "true"
    if text.startswith('"'):
        try:
            return json.loads(text)
        except ValueError:
            raise bad("Двойные кавычки должны содержать корректную JSON-строку.") from None
    if text.startswith("'"):
        if not text.endswith("'") or len(text) < 2:
            raise bad("Не закрыта строка в одинарных кавычках.")
        return text[1:-1].replace("''", "'")
    if text.startswith("["):
        if not text.endswith("]"):
            raise bad("Не закрыт список в шапке.")
        return [scalar(x, depth + 1) for x in split_flow(text[1:-1]) if x]
    if text.startswith("{"):
        if not text.endswith("}"):
            raise bad("Не закрыт словарь в шапке.")
        out = {}
        for item in split_flow(text[1:-1]):
            if not item:
                continue
            pair = split_flow(item, ":")
            if len(pair) < 2:
                raise bad("В словаре ожидается ключ: значение.")
            key = scalar(pair[0], depth + 1)
            if not isinstance(key, str) or key in out:
                raise bad("Ключи словаря должны быть разными строками.")
            # Двоеточия в URL — часть значения.
            out[key] = scalar(":".join(pair[1:]), depth + 1)
        return out
    if re.fullmatch(r"-?(0|[1-9][0-9]*)", text):
        if len(text)>4096:raise bad("Слишком длинное число в шапке.")
        return int(text)
    return text


def strip_comment(line):
    quote, level = "", 0
    for i, ch in enumerate(line):
        if quote:
            if ch == quote and (i == 0 or line[i - 1] != "\\"):
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch in "[{":
            level += 1
        elif ch in "]}":
            level -= 1
        elif ch == "#" and level == 0 and (i == 0 or line[i - 1].isspace()):
            return line[:i].rstrip()
    return line.rstrip()


def header(lines):
    """Блочные словари/списки; многострочные flow-значения собираются вместе."""
    def flow(value, index, indent):
        while True:
            try:
                split_flow(value)
                return value, index
            except HumanError:
                if index >= len(lines):
                    raise
                line = strip_comment(lines[index])
                if line.strip() and len(line) - len(line.lstrip(" ")) < indent:
                    raise
                value += " " + line.strip()
                index += 1

    def block(index, indent, depth):
        if depth > 20:
            raise bad("Слишком глубокая вложенность шапки.")
        out = None
        while index < len(lines):
            raw = lines[index]
            line = strip_comment(raw)
            if not line.strip():
                index += 1
                continue
            spaces = len(line) - len(line.lstrip(" "))
            if "\t" in line[:spaces + 1]:
                raise bad("Используй пробелы для отступов.")
            if spaces < indent:
                break
            if spaces != indent:
                raise bad(f"Неожиданный отступ в строке {index + 2}.")
            text = line[indent:]
            is_list = text == "-" or text.startswith("- ")
            if out is None:
                out = [] if is_list else {}
            if isinstance(out, list) != is_list:
                raise bad("Список и словарь смешаны на одном уровне.")
            if is_list:
                key, value = None, text[1:].strip()
            else:
                key, sep, value = text.partition(":")
                key = key.strip()
                if not sep or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", key) or key in out:
                    raise bad(f"Некорректный или повторяющийся ключ в строке {index + 2}.")
                value = value.strip()
            index += 1
            if value in ("|", "|-", "|+", ">", ">-", ">+"):
                collected = []
                while index < len(lines) and (not lines[index].strip() or len(lines[index]) - len(lines[index].lstrip(" ")) > indent):
                    collected.append(lines[index])
                    index += 1
                offset = min((len(x) - len(x.lstrip(" ")) for x in collected if x.strip()), default=indent + 2)
                chunks = [x[offset:] for x in collected]
                result = "\n".join(chunks)
                if value.startswith(">"):
                    result = re.sub(r"(?<=\S)\n(?=\S)", " ", result)
                result = result.rstrip("\n") + ("" if value.endswith("-") else "\n")
            elif not value:
                next_index = index
                while next_index < len(lines) and not strip_comment(lines[next_index]).strip():
                    next_index += 1
                next_indent = len(lines[next_index]) - len(lines[next_index].lstrip(" ")) if next_index < len(lines) else indent
                if next_indent > indent:
                    value = strip_comment(lines[next_index]).strip()
                    if value.startswith(("{", "[")):
                        value, index = flow(value, next_index + 1, next_indent)
                        result = scalar(value, depth + 1)
                    else:
                        result, index = block(next_index, next_indent, depth + 1)
                else:
                    result = None
            else:
                if value.startswith(("{", "[")):
                    value, index = flow(value, index, indent)
                result = scalar(value, depth + 1)
            if is_list:
                out.append(result)
            else:
                out[key] = result
        return out if out is not None else {}, index

    result, index = block(0, 0, 0)
    if not isinstance(result, dict) or index != len(lines):
        raise bad("Шапка должна быть словарём полей.")
    return result


def parse(text):
    if not isinstance(text,str) or len(text)>65536:raise bad("Нужен текст навыка до 65536 знаков.")
    lines = text.lstrip("\ufeff").splitlines()
    if not lines or lines[0].strip() != "---":
        raise bad("В начале нужна YAML-шапка между строками --- с полями name и description.")
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        raise bad("Не закрыта YAML-шапка строкой ---.")
    meta = header(lines[1:end])
    name, description = meta.get("name"), meta.get("description")
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
        raise bad("name: 1–64 строчных латинских буквы, цифры, _ или -.")
    if not isinstance(description, str) or not description.strip() or len(description) > 1024:
        raise bad("description: непустая строка не длиннее 1024 символов.")
    return meta, "\n".join(lines[end + 1:]).strip()
