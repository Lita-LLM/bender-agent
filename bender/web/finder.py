"""Поиск по пульту: экраны, все настройки и подписи на экранах.

Подписи берутся из кода экранов, поэтому новый раздел или кнопка находятся сами, без ручного списка."""
from __future__ import annotations

import re
from pathlib import Path

# Вспомогательные файлы экранов: где их видно в пульте.
HOSTS = {"search": "settings", "matrix": "channels", "mcp-input": "mcp", "mcp-background": "mcp", "mcp-oauth": "mcp",
         "manners": "self", "proposal": "workshop", "metrics": "workshop", "inspector": "memory", "relocate": "self",
         "voice": "chat"}
# Экраны без пункта в меню.
EXTRA = {"rights": "Права агентов (Стая)", "correspondence": "Переписка агентов"}
FILES = {"": "chat"}  # маршрут → файл, если имя отличается

NAV = re.compile(r'\["#/([\w-]*)",\s*"[\w-]+",\s*"([^"]+)"\]')
TITLES = re.compile(r'const TITLES = \{(.*?)\};', re.S)
PAIR = re.compile(r'(?:"([\w-]*)"|([\w-]+))\s*:\s*"([^"]+)"')
HEADINGS = re.compile(r'<(h3|h4|summary)>([^<>${}]{2,80})</\1>')
LABELS = re.compile(r'\blabel=(?:"([^"${}]{2,80})"|\$\{\s*["\']([^"\'${}]{2,80})["\']\s*\})')
BUTTONS = re.compile(r'>\s*([А-ЯЁA-Z][^<>${}\n]{2,50}?)\s*</\$\{Button\}>')

# Кнопки, которые есть почти везде: по ним ничего не найти.
GENERIC = {"да", "нет", "ок", "сохранить", "отмена", "отменить", "закрыть", "удалить", "разрешить", "отказать", "применить",
           "отклонить", "одобрить", "изменить", "добавить", "проверить", "обновить", "готово", "назад", "далее", "открыть"}

_cache: dict = {}


def noise(kind, value):
    low = value.lower()
    if kind == "action" and low in GENERIC:
        return True
    # Подсказки-примеры в полях («шаблон: npm test*») и служебные слова без кириллицы.
    return any(c in value for c in ":*/{}") or not re.search(r"[а-яё]", low)


def screens(static: Path) -> dict[str, str]:
    """Маршрут → название экрана из меню пульта."""
    source = (static / "app.js").read_text(encoding="utf-8")
    out = {route: title for route, title in NAV.findall(source)}
    match = TITLES.search(source)
    for quoted, bare, title in PAIR.findall(match.group(1) if match else ""):
        out.setdefault(quoted or bare, title)
    for route, title in EXTRA.items():
        out.setdefault(route, title)
    return out


def places(static: Path) -> list[dict]:
    """Заголовки, подписи полей и кнопки каждого экрана; кеш до изменения файлов."""
    folder = static / "screens"
    files = sorted(folder.glob("*.js"))
    stamp = tuple((p.name, p.stat().st_mtime_ns) for p in files) + ((static / "app.js").stat().st_mtime_ns,)
    if _cache.get("stamp") == stamp:
        return _cache["places"]
    titles = screens(static)
    routes = {FILES.get(route, route): route for route in titles}
    out, seen = [], set()
    for path in files:
        name = path.stem
        route = routes.get(name, routes.get(HOSTS.get(name, ""), None))
        if route is None:
            continue
        text = path.read_text(encoding="utf-8")
        found = [("heading", m[1]) for m in HEADINGS.findall(text)]
        found += [("field", a or b) for a, b in LABELS.findall(text)]
        found += [("action", m) for m in BUTTONS.findall(text)]
        for kind, value in found:
            value = " ".join(value.split())
            if noise(kind, value):
                continue
            if (route, value.lower()) in seen or value.lower() == titles.get(route, "").lower():
                continue
            seen.add((route, value.lower()))
            out.append({"kind": kind, "route": "#/" + route, "screen": titles.get(route, route), "title": value, "find": [value]})
    _cache.update(stamp=stamp, places=out)
    return out


def index(settings: list[dict], static: Path) -> list[dict]:
    titles = screens(static)
    items = [{"kind": "screen", "route": "#/" + route, "screen": title, "title": title, "find": []} for route, title in titles.items()]
    for group in settings:
        # Настройки Предохранителя живут на экране «Права», остальные — в «Настройках».
        route = "guard" if group["name"] == "guard" else "settings"
        for field in group["fields"]:
            choices = field.get("choices") or {}
            items.append({"kind": "setting", "route": "#/" + route, "screen": titles.get(route, route), "group": group["title"],
                          "title": field["title"], "hint": field.get("hint") or "", "key": group["name"] + "." + field["key"],
                          "words": " ".join(str(v) for v in choices.values()) if isinstance(choices, dict) else "",
                          "find": [field["title"], group["title"]]})
    known = {(item["route"], item["title"].lower()) for item in items}
    return items + [item for item in places(static) if (item["route"], item["title"].lower()) not in known]
