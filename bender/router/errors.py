"""Ошибки провайдеров моделей: понятный текст + класс, по которому Коммутатор решает, что делать.

Классы:
    rate_limit   лимит запросов/токенов/окна            → подождать, сменить ключ, потом модель
    overloaded   провайдер перегружен (529)              → одна смена ключа, потом модель
    server       сбой сервера (5xx)                      → повтор, потом модель
    timeout      не ответил / замолчал                   → повтор, потом модель
    network      сеть, DNS, TLS, обрыв                   → повтор, потом модель
    billing      нет денег                               → выключить ключ надолго, сказать человеку
    auth         ключ неверный или отозван               → выключить ключ, сказать человеку
    not_found    нет такой модели                        → следующая модель
    context      не влезло в контекст                    → не переключаться: сжимать
    format       мы прислали что-то не то                → не переключаться: это наша ошибка
    refused      отказ по содержанию                     → не переключаться молча
"""

from __future__ import annotations

import json
import re

from ..core.errors import HumanError

RAW_LIMIT = 2048

FAILOVER = {"protocol", "rate_limit", "overloaded", "server", "timeout", "network", "billing", "auth", "not_found"}
RETRY_SAME = {"rate_limit", "overloaded", "server", "timeout", "network"}

CONTEXT_MARKERS = (
    "context length", "context_length", "maximum context", "too many tokens", "prompt is too long",
    "exceeds the context", "context window", "reduce the length", "n_ctx", "input is too long", "too long for",
)
RATE_MARKERS = (
    "rate limit", "rate_limit", "too many requests", "too many concurrent", "concurrency limit", "resource exhausted",
    "resource_exhausted", "throttl", "quota exceeded", "limit reached", "limit exhausted", "usage limit", "requests per",
    "tokens per", "free-models-per", "slow down",
)
BILLING_MARKERS = (
    "insufficient credit", "insufficient_quota", "insufficient funds", "credit balance", "billing", "payment required",
    "add more credits", "out of credits", "no credits", "exceeded your current quota", "key limit exceeded",
)
AUTH_MARKERS = ("invalid api key", "invalid_api_key", "incorrect api key", "unauthorized", "authentication", "api key not valid", "no auth credentials", "invalid x-api-key", "permission denied", "revoked", "deactivated")
NOT_FOUND_MARKERS = ("model not found", "does not exist", "no such model", "unknown model", "model_not_found", "not a valid model", "no endpoints found", "invalid model")
OVERLOAD_MARKERS = ("overloaded", "model is loading", "currently loading", "capacity", "server is busy", "modelnotready")
REFUSAL_MARKERS = ("content policy", "content_policy", "safety system", "flagged", "moderation", "content_filter", "refused")


def _short(text: str) -> str:
    raw = text.encode('utf-8', 'replace')
    return text if len(raw) <= RAW_LIMIT else raw[:RAW_LIMIT].decode('utf-8', 'ignore') + "\n…(обрезано)"


def api_message(body: str) -> str:
    """Человеческое сообщение из тела ошибки OpenAI/Anthropic/OpenRouter/прочих."""
    try:
        doc = json.loads(body)
    except ValueError:
        return body.strip()[:500]
    err = doc.get("error") if isinstance(doc, dict) else None
    if isinstance(err, dict):
        msg = str(err.get("message") or err.get("type") or "")
        meta = err.get("metadata")
        if isinstance(meta, dict) and meta.get("raw"):
            msg = f"{msg} ({meta.get('raw')})" if msg else str(meta.get("raw"))
        return msg
    if isinstance(err, str):
        return err
    if isinstance(doc, dict):
        for k in ("message", "detail", "error_message"):
            if isinstance(doc.get(k), str):
                return doc[k]
    return ""


def parse_retry_after(headers) -> float | None:
    if headers is None:
        return None
    for name in ("retry-after-ms", "retry-after"):
        v = headers.get(name)
        if not v:
            continue
        try:
            sec = float(v)
            return sec / 1000.0 if name == "retry-after-ms" else sec
        except ValueError:
            continue
    return None


def classify(status: int | None, text: str) -> str:
    low = (text or "").lower()
    if any(m in low for m in CONTEXT_MARKERS):
        return "context"
    temporary = re.search(r"(daily|weekly|monthly|hourly|per[- ]minute|per[- ]day|resets?|try again)", low)
    if status == 402:
        return "billing"
    if any(m in low for m in BILLING_MARKERS) and not temporary:
        return "billing"
    if status == 429 or any(m in low for m in RATE_MARKERS):
        return "rate_limit"
    if status == 401 or any(m in low for m in AUTH_MARKERS):
        return "auth"
    if status == 529 or any(m in low for m in OVERLOAD_MARKERS):
        return "overloaded"
    if status == 404 or any(m in low for m in NOT_FOUND_MARKERS):
        return "not_found"
    if any(m in low for m in REFUSAL_MARKERS) and (status is None or status < 500):
        return "refused"
    if status in (408, 499, 504, 522, 524):
        return "timeout"
    if status is None or status >= 500:
        return "server"
    if status == 403:
        return "auth"
    return "format"


HINTS = {
    "rate_limit": ("Провайдер упёрся в лимит запросов", "Подожди немного или добавь запасную модель — Бендер переключится сам."),
    "overloaded": ("Провайдер перегружен", "Это у них, не у тебя. Бендер попробует запасную модель, если она есть."),
    "server": ("Сбой на стороне провайдера", "Обычно проходит само. Если повторяется — добавь запасную модель."),
    "timeout": ("Модель не ответила вовремя", "Сеть или провайдер медленные. Можно увеличить таймаут у провайдера."),
    "network": ("Нет связи с провайдером", "Проверь интернет и адрес провайдера. Для локальной модели — запущен ли сервер."),
    "billing": ("У провайдера кончились деньги на ключе", "Пополни баланс или переключись на бесплатную модель (режим бомжа)."),
    "auth": ("Ключ не подходит", "Проверь API-ключ у провайдера в «Моделях». Возможно, он отозван или вставлен не полностью."),
    "not_found": ("Нет такой модели у провайдера", "Проверь название модели — список есть в «Моделях» → «Все у провайдера»."),
    "context": ("Не влезло в контекст модели", "Бендер сожмёт историю. Если повторяется — уменьши размер контекста в профиле."),
    "format": ("Провайдер не принял запрос", "Проверь код и ответ провайдера в подробностях; причина может быть в параметре, модели или доступе."),
    "refused": ("Модель отказалась отвечать", "Отказ по содержанию. Переформулируй или выбери другую модель."),
}


class ProviderError(HumanError):
    def __init__(self, klass: str, *, provider: str = "", model: str = "", status: int | None = None, message: str = "",
                 raw: str = "", retry_after: float | None = None, partial: dict | None = None):
        title, hint = HINTS.get(klass, ("Ошибка провайдера", ""))
        where = " · ".join(x for x in (provider, model) if x)
        if where:
            title = f"{title} ({where})"
        detail_parts = []
        if status is not None:
            detail_parts.append(f"HTTP {status}")
        if message:
            detail_parts.append(_short(message))
        if raw and raw.strip() != message.strip():
            detail_parts.append(_short(raw))
        super().__init__(f"provider_{klass}", title, hint, detail="\n".join(detail_parts) or None, status=502,
                         data={"class": klass, "provider": provider, "model": model, "http": status})
        self.klass = klass
        self.provider = provider
        self.model = model
        self.http_status = status
        self.message = message
        self.raw = raw
        self.retry_after = retry_after
        self.partial = partial  # что успело прийти до обрыва

    @property
    def failover(self) -> bool:
        return self.klass in FAILOVER


def from_http(status: int, body: str, headers, *, provider: str, model: str) -> ProviderError:
    msg = api_message(body)
    klass = classify(status, msg or body)
    retry = parse_retry_after(headers)
    if retry is None and klass == "rate_limit":
        from .chatgpt import retry_after_from

        retry = retry_after_from(body)
    return ProviderError(klass, provider=provider, model=model, status=status, message=msg, raw=body, retry_after=retry)
