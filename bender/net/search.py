"""Поиск: ограниченные ответы, переход между поставщиками и кэш только в памяти."""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict, deque
from html.parser import HTMLParser
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from ..core.errors import HumanError
from ..guard import host_of
from ..tools.registry import Denied
from .http_client import NetError, Timeouts

PROVIDERS = ("duckduckgo", "searxng", "brave", "tavily")


class SearchError(HumanError):
    def __init__(self, message, hint="Проверь поставщика на экране «Настройки → Поиск»."):
        super().__init__("search", message, hint)


def web_url(value):
    try:
        parsed = urlsplit(value)
        return isinstance(value, str) and parsed.scheme in ("http", "https") and bool(parsed.hostname) and not parsed.username
    except (TypeError, ValueError):
        return False


class Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def plain(value, limit):
    p = Text()
    p.feed(str(value or "")[:20000])
    return " ".join(" ".join(p.parts).split())[:limit]


class DuckResults(HTMLParser):
    """Читаем элементы результата; скрипты, реклама и формы не являются ответами."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.results = [], []
        self.item, self.depth, self.capture = None, 0, None
        self.blocked = False

    def handle_starttag(self, tag, attrs):
        attrs = {key:value or "" for key,value in attrs}
        classes = attrs.get("class", "").split()
        if len(self.stack)>=256:raise SearchError("Слишком глубокая HTML-страница поиска")
        if tag == "form" and ("challenge" in attrs.get("id", "") or "anomaly" in attrs.get("action", "")):
            self.blocked = True
        if tag not in ("br", "img", "input", "meta", "link", "hr", "wbr", "source", "area", "embed", "param", "col", "base"):
            self.stack.append(tag)
        if tag == "div" and ("result" in classes or "body" in classes):
            if self.item:
                self.finish()
            self.item = {"title": "", "url": "", "snippet": ""}
            self.depth = len(self.stack)
        if not self.item:
            return
        if "result__snippet" in classes:
            self.capture = ("snippet", len(self.stack))
        elif tag == "a" and ("result__a" in classes or "h2" in self.stack or len(self.stack) == self.depth + 1):
            self.item["url"] = attrs.get("href", "")
            self.capture = ("title", len(self.stack))

    def handle_endtag(self, tag):
        if self.capture and len(self.stack) == self.capture[1]:
            self.capture = None
        if self.item and len(self.stack) == self.depth and tag == "div":
            self.finish()
        if tag in self.stack:
            del self.stack[len(self.stack) - 1 - self.stack[::-1].index(tag):]

    def handle_data(self, data):
        if self.item and self.capture and "script" not in self.stack and "style" not in self.stack:
            self.item[self.capture[0]] += data

    def finish(self):
        try:
            url = urljoin("https://duckduckgo.com", self.item["url"])
            redirect = parse_qs(urlsplit(url).query).get("uddg")
        except ValueError:
            self.item,self.capture=None,None
            return
        self.item["url"] = redirect[0] if redirect else url
        if len(self.results)<100:self.results.append(self.item)
        self.item, self.capture = None, None


class Search:
    def __init__(self, app):
        self.app = app
        self.cache = OrderedDict()
        self.requests = {p: deque() for p in PROVIDERS}

    async def search(self, query, count, lang, ctx, *, provider=""):
        app = self.app
        if app.safe or not app.settings.get("search", "enabled", True):
            raise SearchError("Поиск выключен")
        query = str(query).strip()
        if not query or len(query) > 2000:
            raise SearchError("Нужен поисковый запрос длиной от 1 до 2000 символов")
        count = max(1, min(int(count), 20))
        lang = str(lang or "").strip()[:32]
        settings = app.settings.group("search")
        order = [provider] if provider else [p.strip() for p in settings["providers"].split(",") if p.strip()]
        if not order or any(p not in PROVIDERS for p in order):
            raise SearchError("Неизвестный поставщик поиска", ", ".join(PROVIDERS))
        errors = []
        for name in dict.fromkeys(order):
            endpoint = settings.get(name + "_url", "").strip()
            if not endpoint:
                errors.append(name + ": адрес не настроен")
                continue
            if not web_url(endpoint):
                raise SearchError("Нужен адрес http(s) без логина и пароля", name)
            # Разрешения проверяются и при чтении кэша; отказ не запускает обход через другой сервис.
            await ctx.check("net.read", endpoint, f"найти в {name}: {query[:100]}")
            key_name = str(settings.get(name + "_secret", ""))
            credential = app.secrets.get(key_name) if key_name else None
            fingerprint = hashlib.sha256((credential or "").encode()).hexdigest()
            cache_key = (name, endpoint, query, count, lang, settings["safesearch"], settings["region"], fingerprint)
            cached = self.cache.get(cache_key)
            if cached and cached[0] > time.monotonic():
                self.cache.move_to_end(cache_key)
                ctx.tainted = True
                return {**cached[1], "cached": True, "failures": errors}
            if cached:
                del self.cache[cache_key]
            try:
                headers = {"User-Agent": "Bender/1.0 (+personal-assistant; web-search)", "Accept": "application/json,text/html;q=0.9"}
                if name in ("brave", "tavily"):
                    if not credential:
                        raise SearchError("Не настроен ключ поиска", name)
                    await ctx.check("secret.use", key_name, f"использовать ключ поиска {name}")
                    credential, _ = app.guard.expand_secrets("{{secret:" + key_name + "}}", host=host_of(endpoint), tool="web_search", session_id=ctx.session_id)
                    headers["X-Subscription-Token" if name == "brave" else "Authorization"] = credential if name == "brave" else "Bearer " + credential
                now = time.monotonic()
                history = self.requests[name]
                while history and history[0] <= now - 60:
                    history.popleft()
                if len(history) >= settings["per_minute"]:
                    raise SearchError("Достигнут лимит запросов в минуту", name)
                history.append(now)
                params = {"q": query}
                method, payload = "GET", None
                safe, region = settings["safesearch"], settings["region"]
                if name == "duckduckgo":
                    params.update({"kl": region, "kp": {"off": "-2", "moderate": "-1", "strict": "1"}[safe]})
                elif name == "searxng":
                    params.update({"format": "json", "language": lang or "auto", "safesearch": {"off": 0, "moderate": 1, "strict": 2}[safe]})
                elif name == "brave":
                    params.update({"count": count, "safesearch": safe, "country": (region.split("-")[0] if region != "wt-wt" else "US").upper(), "search_lang": lang or "en"})
                else:
                    method = "POST"
                    params = {}
                    payload = {"query": query, "max_results": count, "search_depth": "basic", "include_answer": False, "include_raw_content": False}
                    if settings.get("tavily_country"):
                        payload["country"] = settings["tavily_country"]
                    if lang:
                        payload["language"] = lang
                url = endpoint + (("&" if "?" in endpoint else "?") + urlencode(params) if params else "")
                ctx.tainted = True
                response = await app.http.request(method, url, headers=headers, json=payload, timeouts=Timeouts(connect=10, read=20, total=30), retry_stale=False)
                async with response:
                    data = await response.read(limit=2 * 1024 * 1024)
                if response.status >= 300:
                    raise SearchError(f"Поиск {name}: HTTP {response.status}", "При 429 подожди; при 401/403 проверь ключ и доступ. Перенаправления с ключом не выполняются.")
                if name == "duckduckgo":
                    parser = DuckResults()
                    page = data.decode("utf-8", "replace")
                    parser.feed(page)
                    if parser.item:
                        parser.finish()
                    if parser.blocked or "anomaly.js" in page or "unfortunately, bots" in page.lower():
                        raise SearchError("DuckDuckGo просит капчу или блокирует запросы", "Пройди проверку в браузере или выбери другой поставщик. Бендер капчу не обходит.")
                    raw = parser.results
                else:
                    decoded = json.loads(data)
                    raw = decoded.get("web", {}).get("results", []) if name == "brave" else decoded.get("results", [])
                    if not isinstance(raw, list):
                        raise ValueError("results должен быть списком")
                results, seen = [], set()
                for row in raw:
                    if not isinstance(row, dict) or not web_url(row.get("url")) or row["url"] in seen:
                        continue
                    seen.add(row["url"])
                    results.append({"title": app.guard.scrub(plain(row.get("title"), 500)), "url": app.guard.scrub(row["url"][:4096]),
                                    "snippet": app.guard.scrub(plain(row.get("snippet") or row.get("content") or row.get("description"), 2000))})
                    if len(results) >= count:
                        break
                result = {"provider": name, "results": results, "cached": False}
                self.cache[cache_key] = (time.monotonic() + settings["cache_seconds"], result)
                self.cache.move_to_end(cache_key)
                while len(self.cache) > 128:
                    self.cache.popitem(last=False)
                return {**result, "failures": errors}
            except Denied:
                raise
            except (HumanError, NetError, ValueError, TypeError, AttributeError) as exc:
                errors.append(name + ": " + app.guard.scrub(str(exc)))
        raise SearchError("Все поставщики поиска недоступны", "; ".join(errors))
