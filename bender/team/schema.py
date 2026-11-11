"""Проверка ответа субагента по JSON Schema — подмножество, которого хватает для схем ответов:
type (в том числе список типов), properties, required, additionalProperties: false, items, enum, const,
minimum/maximum, minLength/maxLength, minItems/maxItems, anyOf/oneOf (упрощённо — «хоть одна подошла»).
Ошибки — по-русски, с путём: модель получает их и исправляет ответ.
"""

from __future__ import annotations

TYPES = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}


def _is(v, t: str) -> bool:
    if t == "integer":
        return isinstance(v, int) and not isinstance(v, bool)
    if t == "number":
        return isinstance(v, (int, float)) and not isinstance(v, bool)
    cls = TYPES.get(t)
    return isinstance(v, cls) if cls else True


def validate(value, schema: dict, path: str = "$") -> list[str]:
    if not isinstance(schema, dict) or not schema:
        return []
    errs: list[str] = []
    for key in ("anyOf", "oneOf"):
        if isinstance(schema.get(key), list):
            if not any(not validate(value, s, path) for s in schema[key]):
                errs.append(f"{path}: не подходит ни под один из вариантов")
            return errs
    t = schema.get("type")
    if t is not None:
        ts = t if isinstance(t, list) else [t]
        if not any(_is(value, x) for x in ts):
            return [f"{path}: нужен тип {' или '.join(ts)}, а пришло {type(value).__name__}"]
    if "const" in schema and value != schema["const"]:
        errs.append(f"{path}: должно быть ровно {schema['const']!r}")
    if isinstance(schema.get("enum"), list) and value not in schema["enum"]:
        errs.append(f"{path}: допустимо только {', '.join(repr(x) for x in schema['enum'])}")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            errs.append(f"{path}: не меньше {schema['minimum']}")
        if "maximum" in schema and value > schema["maximum"]:
            errs.append(f"{path}: не больше {schema['maximum']}")
    if isinstance(value, str):
        if "minLength" in schema and len(value) < schema["minLength"]:
            errs.append(f"{path}: не короче {schema['minLength']} символов")
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            errs.append(f"{path}: не длиннее {schema['maxLength']} символов")
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            errs.append(f"{path}: элементов не меньше {schema['minItems']}")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            errs.append(f"{path}: элементов не больше {schema['maxItems']}")
        if isinstance(schema.get("items"), dict):
            for i, v in enumerate(value):
                errs += validate(v, schema["items"], f"{path}[{i}]")
    if isinstance(value, dict):
        props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
        for k in schema.get("required") or []:
            if k not in value:
                errs.append(f"{path}: нет обязательного поля «{k}»")
        for k, v in value.items():
            if k in props:
                errs += validate(v, props[k], f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}: лишнее поле «{k}»")
    return errs


def harmless(value, schema: dict | None = None) -> bool:
    """Значение не может нести указаний: только числа, да/нет, пусто или строки из перечисления схемы.
    Такой ответ Чтеца на недоверенный текст не заражает родителя."""
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        return bool(schema and isinstance(schema.get("enum"), list) and value in schema["enum"])
    if isinstance(value, list):
        item = (schema or {}).get("items") if isinstance((schema or {}).get("items"), dict) else None
        return all(harmless(v, item) for v in value)
    if isinstance(value, dict):
        props = (schema or {}).get("properties") or {}
        return all(harmless(v, props.get(k) if isinstance(props.get(k), dict) else None) for k, v in value.items())
    return False
