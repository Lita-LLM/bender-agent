"""Провайдеры и роли моделей в настройках: добавить, проверить, выбрать модель для роли.

Провайдер (группа настроек providers, ключ — ID):
    {"id", "name", "kind": "openai" | "anthropic", "base_url", "keys": [имя секрета…], "headers": {},
     "enabled": true, "timeout": 300, "parallel": 4, "is_local": false, "free": false,
     "prices": {модель: {"in": $ за 1M, "out": …}}, "models": [кэш списка моделей]}

Роль (группа roles, ключ — имя роли): список {"provider", "model", "params", "enabled"}.
"""

from __future__ import annotations

import json
import re
import time
from urllib.parse import urlsplit

from ..core.errors import HumanError
from ..core.ids import new_id
from ..core.secrets import Secrets
from ..core.settings import Settings

ROLES = {
    "ears": ("Уши", "Распознавание речи через /audio/transcriptions; без подмены Главной."),
    "voice": ("Голос", "Синтез через /audio/speech; без подмены Главной."),
    "main": ("Главная", "Разговор с хозяином и основная работа."),
    "fast": ("Быстрая", "Дешёвая и быстрая: сортировка, пересказы, решение пульса, заголовки."),
    "smart": ("Умная", "Трудные рассуждения."),
    "coder": ("Кодер", "Работа с кодом."),
    "vision": ("Зрение", "Картинки."),
    "embed": ("Эмбеддинги", "Векторы для памяти."),
    "dreamer": ("Сновидец", "Сон: приведение памяти в порядок."),
    "risk": ("Оценщик риска", "Может только ужесточить решение Предохранителя."),
}

PRESETS = {
    "chatgpt": {"name": "ChatGPT (подписка)", "kind": "chatgpt", "base_url": "https://chatgpt.com/backend-api/codex", "oauth": True},
    "claude_code": {"name": "Claude Code (на этой машине)", "kind": "claude_code", "subscription": True},
    "antigravity": {"name": "Antigravity (agy, превью, риск блокировки)", "kind": "antigravity", "subscription": True,
                    "note": "Текст, поток, тулы Бендера текстовым протоколом. Встроенные тулы agy ограничены режимом plan и инструкцией, без гарантии изоляции; подключение требует принятия риска."},
    "codex_cli": {"name": "Codex CLI (пока недоступен как движок)", "kind": "codex_cli", "blocked": True},
    "openrouter": {"name": "OpenRouter", "kind": "openai", "base_url": "https://openrouter.ai/api/v1"},
    "openai": {"name": "OpenAI", "kind": "openai", "base_url": "https://api.openai.com/v1"},
    "anthropic": {"name": "Anthropic", "kind": "anthropic", "base_url": "https://api.anthropic.com/v1"},
    "deepseek": {"name": "DeepSeek", "kind": "openai", "base_url": "https://api.deepseek.com/v1"},
    "groq": {"name": "Groq", "kind": "openai", "base_url": "https://api.groq.com/openai/v1"},
    'local_voice':{'name':'Локальный голос','kind':'openai','base_url':'http://127.0.0.1:8899/v1','is_local':True,'free':True,'audio_only':True,
                   'models':[{'id':'gigaam-v3-e2e-ctc','audio_role':'ears'},{'id':'piper-ru','audio_role':'voice'}]},
    'groq_voice':{'name':'Groq Whisper — Уши','kind':'openai','base_url':'https://api.groq.com/openai/v1','audio_only':True,'note':'Whisper large-v3/turbo; доступен бесплатный уровень с квотами. Проверь лимиты своего ключа.',
                  'models':[{'id':'whisper-large-v3-turbo','audio_role':'ears'},{'id':'whisper-large-v3','audio_role':'ears'}]},
    'gemini_voice':{'name':'Gemini — Уши и Голос','kind':'openai','base_url':'https://generativelanguage.googleapis.com/v1beta','audio_only':True,'audio_protocol':'gemini',
                    'note':'На бесплатном уровне аудио используется для улучшения продуктов Google. России нет в доступных регионах: нужен разрешённый посредник или прокси. Уровень и квоты проверь у Google. Подписка Antigravity голосового API не даёт.',
                    'models':[{'id':'gemini-3.5-transcribe','audio_role':'ears','audio_protocol':'gemini'},{'id':'gemini-3.8-flash-tts','audio_role':'voice','audio_protocol':'gemini'},{'id':'gemini-3.8-flash-lite-tts','audio_role':'voice','audio_protocol':'gemini'}]},
    'openai_voice':{'name':'OpenAI — платные Уши и Голос','kind':'openai','base_url':'https://api.openai.com/v1','audio_only':True,'note':'Платный API по отдельному ключу. Подписка ChatGPT не заменяет голосовой API-ключ.',
                    'models':[{'id':'whisper-1','audio_role':'ears'},{'id':'gpt-4o-mini-transcribe','audio_role':'ears'},{'id':'gpt-4o-mini-tts','audio_role':'voice'}]},
    "gemini": {"name": "Google Gemini", "kind": "openai", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai"},
    "mistral": {"name": "Mistral", "kind": "openai", "base_url": "https://api.mistral.ai/v1"},
    "omniroute": {"name": "OmniRoute", "kind": "openai", "base_url": "http://127.0.0.1:20128/v1", "is_local": True, "note": "Свои ключи и подписки за одним OpenAI-совместимым адресом. Осторожно со входом в чужой аккаунт через посредника: Google пишет, что доступ к Antigravity сторонним ПО нарушает его условия и может кончиться блокировкой аккаунта. Без такого риска — ключ API (Gemini — пресет Google Gemini)."},
    "ollama": {"name": "Ollama", "kind": "openai", "base_url": "http://127.0.0.1:11434/v1", "is_local": True, "free": True},
    "llamacpp": {"name": "llama.cpp", "kind": "openai", "base_url": "http://127.0.0.1:8080/v1", "is_local": True, "free": True},
    "lmstudio": {"name": "LM Studio", "kind": "openai", "base_url": "http://127.0.0.1:1234/v1", "is_local": True, "free": True},
    "fake": {"name": "Тестовый (без токенов)", "kind": "openai", "base_url": "http://127.0.0.1:5199/v1", "is_local": True, "free": True},
}


def normalize_base_url(url: str) -> str:
    url = (url or "").strip().rstrip("/")
    if not url:
        raise HumanError("bad_provider", "Пустой адрес провайдера", "Например: https://openrouter.ai/api/v1")
    if not re.match(r"^https?://", url):
        url = "http://" + url if url.startswith(("127.", "localhost", "192.168.", "10.")) else "https://" + url
    for tail in ("/chat/completions", "/messages", "/completions"):
        if url.endswith(tail):
            url = url[: -len(tail)]
    sp = urlsplit(url)
    if not sp.hostname:
        raise HumanError("bad_provider", "Не понимаю адрес провайдера", url)
    return url



GEMINI_CLI_NOTICE = ('Провайдер Gemini CLI убран: по подписке Google он больше не работает, его сменил Antigravity CLI. Модели Gemini подключи по ключу Google API '
                     '(пресет Google Gemini) или через OmniRoute — вход в аккаунт Google через посредника на твой риск, см. подсказку OmniRoute')

class ModelsConfig:
    def __init__(self, settings: Settings, secrets: Secrets):
        self.settings = settings
        self.secrets = secrets
        providers = self.providers()
        changed = False
        for pid, p in list(providers.items()):
            if p.get('kind') == 'gemini_cli' and (p.get('enabled', True) or not p.get('retired')):
                providers[pid] = {**p, 'enabled': False, 'retired': True}
                changed = True
        if changed:
            with self.settings.transaction():
                providers=self.providers()
                for pid,p in list(providers.items()):
                    if p.get('kind')=='gemini_cli':providers[pid]={**p,'enabled':False,'retired':True}
                self.settings.replace('providers',providers)
                self.settings.store.meta_set('models:removed_notice', GEMINI_CLI_NOTICE)
        else:
            # Незакрытое прежнее уведомление («Gemini CLI больше не существует») — заменить честным текстом.
            shown = self.settings.store.meta_get('models:removed_notice')
            if isinstance(shown, str) and shown.startswith('Провайдер Gemini CLI убран') and shown != GEMINI_CLI_NOTICE:
                self.settings.store.meta_set('models:removed_notice', GEMINI_CLI_NOTICE)

    def providers(self) -> dict[str, dict]:
        return dict(self.settings.group("providers"))

    def roles(self) -> dict[str, list[dict]]:
        return {k: list(v) for k, v in self.settings.group("roles").items() if isinstance(v, list)}

    def add_provider(self, *, name: str = "", kind: str = "openai", base_url: str = "", api_key: str | None = None, preset: str | None = None, extra: dict | None = None) -> dict:
        with self.settings.transaction():
            return self._add_provider(name=name,kind=kind,base_url=base_url,api_key=api_key,preset=preset,extra=extra)

    def _add_provider(self, *, name: str = "", kind: str = "openai", base_url: str = "", api_key: str | None = None, preset: str | None = None, extra: dict | None = None) -> dict:
        if kind == 'gemini_cli' or preset == 'gemini_cli':
            raise HumanError('provider_removed', 'Этот CLI-провайдер убран', 'Подключи модели через OmniRoute или Google API по ключу.')
        base = dict(PRESETS.get(preset or "", {}))
        if base.get("blocked") or kind == "codex_cli":
            raise HumanError("cli_unsafe", "Codex CLI пока нельзя подключить как движок", "Не подтверждён способ выключить все встроенные инструменты; проверь сведения в Полигоне.")
        cli = (base.get("kind") if preset else kind) in ("claude_code", "antigravity")
        p = {"id": "p" + new_id()[-10:].lower(), "name": name or base.get("name") or "Провайдер", "kind": kind or base.get("kind", "openai"),
             "base_url": "" if cli else normalize_base_url(base_url or base.get("base_url", "")), "keys": [], "enabled": True, "timeout": 300, "parallel": 4,
             "is_local": bool(base.get("is_local")), "free": bool(base.get("free")), "created_at": time.time()}
        if preset:
            p["preset"] = preset
            p["kind"] = base.get("kind", p["kind"])
            for field in ('audio_only','audio_protocol','models','note'):
                if field in base:p[field]=base[field]
        if kind and not preset:
            p["kind"] = kind
        if p["kind"] == "antigravity":
            from .agy import RISK
            if not (extra or {}).get("risk_accepted"):
                raise HumanError("agy_risk", "Нужно принять риск Antigravity", RISK)
            p.update({"subscription": True, "parallel": 1, "timeout": 600, "models": [{"id": "default", "subscription": True}]})
            extra = {k: v for k, v in (extra or {}).items() if k in ("command", "risk_accepted")}
        elif p["kind"] not in ("openai", "anthropic", "chatgpt", "claude_code"):
            raise HumanError("bad_provider", f"Неизвестный вид API «{p['kind']}»", "Бывает openai (OpenAI-совместимый), anthropic, chatgpt (подписка) или claude_code (Claude Code на этой машине).")
        if p["kind"] == "claude_code":
            p.update({"subscription": bool(base.get("subscription", True)), "parallel": 1, "timeout": 1800,
                      "models": [{"id": m, "subscription": True} for m in ("default", "opus", "sonnet", "fable", "haiku")]})
        p.update(extra or {})
        if p['kind'] not in ('openai', 'anthropic', 'chatgpt', 'claude_code', 'antigravity'):
            raise HumanError('bad_provider', 'Этот вид провайдера недоступен', 'Выбери API, ChatGPT или Claude Code.')
        if p["kind"] == "claude_code" and p.get("billing") == "api":
            p["subscription"] = False
            p["models"] = [{"id": m["id"]} for m in p["models"]]
        if api_key:
            secret_name = f"provider:{p['id']}:0"
            secret_name=self.secrets.reuse(secret_name, api_key.strip(), note=f"Ключ {p['name']}")
            p["keys"] = [secret_name]
        allp = self.providers()
        allp[p["id"]] = p
        self.settings.replace("providers", allp)
        return p

    def update_provider(self, pid: str, changes: dict) -> dict:
        with self.settings.transaction():return self._update_provider(pid,dict(changes))

    def _update_provider(self, pid: str, changes: dict) -> dict:
        allp = self.providers()
        if pid not in allp:
            raise HumanError("no_provider", "Нет такого провайдера", "")
        p = dict(allp[pid])
        if (changes.get('kind') or p.get('kind')) == 'gemini_cli':
            raise HumanError('provider_removed', 'Этот CLI-провайдер убран', 'Удали отключённую запись и подключи OmniRoute или Google API.')
        if "base_url" in changes:  # у CLI-движка адреса нет
            changes["base_url"] = "" if (changes.get("kind") or p.get("kind")) == "claude_code" else normalize_base_url(changes["base_url"])
        key = changes.pop("api_key", None)
        p.update({k: v for k, v in changes.items() if k not in ("id", "keys")})
        if key:
            secret_name = f"provider:{pid}:{len(p.get('keys') or [])}"
            secret_name=self.secrets.reuse(secret_name, key.strip(), note=f"Ключ {p['name']}")
            p["keys"] = list(dict.fromkeys([*(p.get("keys") or []),secret_name]))
        allp[pid] = p
        self.settings.replace("providers", allp)
        return p

    def remove_provider(self, pid: str) -> None:
        with self.settings.transaction():self._remove_provider(pid)

    def apply_catalog(self,pid,models):
        """Сеть уже закончилась: цены/прочие правки перечитываются внутри записи."""
        from .catalog import apply_catalog
        with self.settings.transaction():
            p=self.providers().get(pid)
            if not p:raise HumanError('no_provider','Провайдер уже удалён','Открой «Модели» заново.')
            return self.update_provider(pid,apply_catalog(p,models))

    def set_capability(self,pid,model,key,value,*,source='owner',reason=''):
        from .capabilities import KEYS
        if key not in KEYS or value is not None and not isinstance(value,bool) or not isinstance(model,str) or not model.strip() or len(model)>500:
            raise HumanError('model_capability','Не удалось изменить возможность модели','Нужны модель, vision/audio_input и true/false/null.')
        with self.settings.transaction():
            p=self.providers().get(pid)
            if not p:raise HumanError('no_provider','Провайдер уже удалён','Открой «Модели» заново.')
            records=dict(p.get('model_capabilities') or {});entry=dict(records.get(model) or {})
            previous=entry.get(key) if isinstance(entry.get(key),dict) else {}
            entry[key]={**previous,'value':value,'source':source,'at':time.time(),'reason':str(reason)[:2048]};records[model]=entry
            return self.update_provider(pid,{'model_capabilities':records})

    def _remove_provider(self, pid: str) -> None:
        allp = self.providers()
        p = allp.pop(pid, None)
        if p:
            for name in p.get("keys") or []:
                other=any(name in row.get('keys',[]) for row in allp.values())
                mcp=('{{secret:'+name+'}}') in json.dumps(self.settings.group('mcp'),ensure_ascii=False)
                if name.startswith('provider:'+pid+':') and not other and not mcp:self.secrets.delete(name)
        self.settings.replace("providers", allp)
        roles = self.roles()
        self.settings.set('roles',{r:[e for e in entries if isinstance(e,dict) and e.get('provider')!=pid]
                                   for r,entries in roles.items() if r in ROLES},by='owner')

    def set_role(self, role: str, entries: list[dict]) -> None:
        self.settings.set('roles', {role: entries}, by='owner')
        self.remember_models(entries)

    def remember_models(self, entries) -> list[str]:
        """Модель, введённая руками, остаётся в списке провайдера (manual) — её не надо вводить снова."""
        allp = self.providers();added = []
        for e in entries or []:
            if not isinstance(e, dict):continue
            pid, model = e.get('provider'), str(e.get('model') or '').strip()
            p = allp.get(pid)
            if not p or not model or len(model) > 300:continue
            models = list(p.get('models') or [])
            if any((m.get('id') if isinstance(m, dict) else m) == model for m in models):continue
            allp[pid] = {**p, 'models': models + [{'id': model, 'manual': True}]};added.append(model)
        if added:self.settings.replace('providers', allp)
        return added

    def forget_model(self, pid: str, model: str) -> dict:
        """Убрать введённую руками модель из списка (каталожные приходят с «Обновить список»)."""
        with self.settings.transaction():
            p = self.providers().get(pid)
            if not p:raise HumanError('no_provider', 'Нет такого провайдера', '', status=404)
            models = [m for m in p.get('models') or [] if not (isinstance(m, dict) and m.get('manual') and m.get('id') == model)]
            if len(models) == len(p.get('models') or []):
                raise HumanError('model_not_manual', 'Эта модель не из введённых вручную', 'Модели из каталога провайдера убираются только вместе с каталогом.')
            return self._update_provider(pid, {'models': models})

    def connect_chatgpt(self, tokens: dict, *, auth_url: str | None = None, base_url: str | None = None) -> dict:
        with self.settings.transaction():return self._connect_chatgpt(tokens,auth_url=auth_url,base_url=base_url)

    def _connect_chatgpt(self, tokens: dict, *, auth_url: str | None = None, base_url: str | None = None) -> dict:
        """Вход через ChatGPT удался: новый провайдер-подписка или новый вход у того же аккаунта."""
        from . import chatgpt

        packed = chatgpt.pack(tokens)
        for p in self.providers().values():
            if p.get("kind") == "chatgpt" and p.get("account_id") == tokens.get("account_id") and p.get("keys"):
                self.secrets.set(p["keys"][0], packed, note=f"Вход ChatGPT {tokens.get('email') or ''}".strip())
                return self.update_provider(p["id"], {"email": tokens.get("email"), "enabled": True})
        extra = {"subscription": True, "account_id": tokens.get("account_id"), "email": tokens.get("email"),
                 "models": [dict(m, subscription=True) for m in chatgpt.MODELS], "parallel": 2}
        if auth_url:
            extra["auth_url"] = auth_url
        name = "ChatGPT" + (f" ({tokens['email']})" if tokens.get("email") else "")
        p = self.add_provider(name=name, preset="chatgpt", base_url=base_url or chatgpt.backend_base(), extra=extra)
        secret_name = f"provider:{p['id']}:oauth"
        self.secrets.set(secret_name, packed, note=f"Вход ChatGPT {tokens.get('email') or ''}".strip())
        allp = self.providers()
        allp[p["id"]]["keys"] = [secret_name]
        self.settings.replace("providers", allp)
        if not self.roles().get("main"):
            self.set_role("main", [{"provider": p["id"], "model": chatgpt.DEFAULT_MODEL}])
        return allp[p["id"]]

    def public_providers(self) -> list[dict]:
        out = []
        for p in self.providers().values():
            q = {k: v for k, v in p.items() if k != "keys"}
            if p.get("kind") == "chatgpt":
                q["keys"] = [{"name": n, "masked": "вход " + (p.get("email") or "ChatGPT")} for n in p.get("keys") or []]
            else:
                q["keys"] = [{"name": n, "masked": self.secrets.masked(n)} for n in p.get("keys") or []]
            q["has_key"] = any(self.secrets.get(n) for n in p.get("keys") or [])
            out.append(q)
        return out


def role_changes(settings, changes):
    """Общая проверка схемы roles и границы самостоятельной перестановки."""
    providers, roles = settings.group('providers'), settings.group('roles')
    from .roles import cleaned,problem
    valid=cleaned(roles,providers)
    clean={role:entries for role,entries in valid.items() if entries!=roles[role]}
    reasons = []
    def access(entry):
        p = providers.get(entry.get('provider'), {})
        if p.get('subscription') or p.get('kind') == 'chatgpt': return 'subscription'
        return 'local' if p.get('is_local') or p.get('free') else 'key'
    for role, entries in changes.items():
        if role not in ROLES or not isinstance(entries, list) or len(entries) > 32:
            raise HumanError('bad_role', 'Не удалось изменить роль модели', 'Нужна известная роль и список не более 32 кандидатов.')
        old = valid.get(role) or ([] if role == 'main' else valid.get('main') or [])
        actual=roles.get(role,[])
        if not isinstance(actual,list):actual=[]
        result = []
        for i, entry in enumerate(entries):
            if problem(entry,providers):
                # Старый сломанный кандидат убирается; новый ошибочный ввод по-прежнему запрещён.
                if entry in actual:continue
                raise HumanError('bad_role', 'В роли не указаны подключённый провайдер и модель', 'Нового провайдера подключает хозяин в пульте → Модели.')
            if not isinstance(entry.get('params', {}), dict) or not isinstance(entry.get('enabled', True), bool):
                raise HumanError('bad_role', 'Повреждены параметры кандидата', 'params — объект, enabled — true/false.')
            from .adapters import RESERVED
            if set(entry.get('params') or {}) & RESERVED:
                raise HumanError('bad_role', 'Параметры кандидата не задают модель, сообщения и тулы', 'Модель выбирается полем model; убери из params: '+', '.join(sorted(set(entry['params']) & RESERVED))+'.')
            previous = actual[i] if i < len(actual) and isinstance(actual[i], dict) else {}
            params=previous.get('params') if isinstance(previous.get('params'),dict) else {}
            from .effort import validate
            validate(entry.get('params',params))
            result.append({**previous, **entry, 'model': entry['model'].strip(), 'params': dict(entry.get('params',params)), 'enabled': entry.get('enabled', True)})
        enabled = [e for e in result if e.get('enabled', True)]
        old_enabled = [e for e in old if e.get('enabled', True)]
        if len(enabled) < len(old_enabled): reasons.append(f'{role}: удаление или выключение модели/запасных')
        if any(providers[e['provider']].get('enabled') is False for e in enabled): reasons.append(f'{role}: выбран выключенный провайдер')
        if old_enabled and access(old_enabled[0]) != 'key' and any(access(e) == 'key' for e in enabled):
            # Уже разрешённый платный запасной не требует повторного согласия, пока не становится основным.
            old_paid = {(e['provider'], e['model']) for e in old_enabled[1:] if access(e) == 'key'}
            if enabled and access(enabled[0]) == 'key' or any(access(e) == 'key' and (e['provider'], e['model']) not in old_paid for e in enabled[1:]):
                reasons.append(f'{role}: переход с подписки/локальной модели на платный ключ')
        elif not old_enabled and any(access(e) == 'key' for e in enabled): reasons.append(f'{role}: включение платного ключа')
        clean[role] = result
    return clean, reasons
