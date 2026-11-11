"""CLI-движки: ход Бендера через официальный Claude Code на этой же машине (`claude -p`).

Хозяин сам вошёл в Claude Code (подписка или ключ) — Бендер его не подделывает, а запускает. Свои тулы
Claude Code не получает (`--tools ""`): только тулы Бендера через петлевой MCP-сервер (`--mcp-config`,
`--strict-mcp-config`), поэтому всё идёт через Предохранитель. Личность и память — системным промптом
Бендера (`--system-prompt`, без «снимка», чтобы свежая память доходила каждый ход). Разговор
продолжается (`--session-id` в первый раз, дальше `--resume`): в Claude Code уходит только новое.

Картинки хозяина и из read_file — изображениями: сообщение `--input-format stream-json`, в MCP — блок image.

Поток `--output-format stream-json --include-partial-messages`: текст — по мере генерации, в конце
`result` с итогом, ценой и токенами. Ошибки раскладываются по классам Коммутатора: не найден/не вошёл
— запасная модель, лимит подписки — остывание.

Правила Anthropic про такое использование в 2026 году менялись — пульт
честно предупреждает при подключении.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import shutil
import uuid
from pathlib import Path

from .errors import ProviderError
from .types import Result, Usage

# ход Разума, ради которого зовут модель: CLI-движку нужен, чтобы привязать к нему тулы MCP
TURN: contextvars.ContextVar = contextvars.ContextVar("bender_turn", default=None)

MODELS = ["default", "opus", "sonnet", "fable", "haiku"]
# Флаги новых версий: без них ход работает, но хуже (try claude называет недостающие).
OPTIONAL = ("--effort", "--input-format", "--include-partial-messages", "--restricted", "--permission-prompts",
            "--disable-slash-commands", "--system-prompt-snapshot")
# Промпт Бендера зовёт тулы короткими именами, а Claude Code показывает их с приставкой сервера.
TOOL_NAMES = ("\n\n---\n\nТвои тулы здесь — тулы Бендера с приставкой mcp__bender__: recall — это mcp__bender__recall, "
              "remind — mcp__bender__remind и так далее. Других тулов нет.")
from .cli_common import _FLAGS, classify as _classify, command, flags, probe as _run, stream

IMAGE_LIMIT = 20  # картинок за один вызов
IMAGE_BYTES = 5 * 1024 * 1024  # предел Anthropic на одну картинку
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")


def flatten(content, images: list) -> str:
    """Содержимое сообщения — текстом; картинки складываются в images и отмечаются в тексте по номеру."""
    if not isinstance(content, list):
        return str(content or "")
    out = []
    for part in content:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind == "text":
            out.append(str(part.get("text") or ""))
        elif kind == "image":
            where = str(part.get("path") or "из вложения")
            data, mime = str(part.get("data") or ""), part.get("media_type") or "image/png"
            if not data or mime not in IMAGE_TYPES or len(data) * 3 // 4 > IMAGE_BYTES or len(images) >= IMAGE_LIMIT:
                out.append(f"[Картинка не передана (формат, размер или их слишком много). Файл: {where}]")
            else:
                images.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}})
                out.append(f"[Картинка {len(images)} приложена ниже. Файл: {where}]")
        elif kind == "audio":
            out.append(f"[Звук сюда не передаётся. Файл: {part.get('path') or 'из вложения'}]")
    return "\n".join(s for s in out if s)


def payload(text: str, images: list, known) -> tuple[str, list]:
    """Без картинок — обычный текст в stdin. С картинками — одно сообщение stream-json с блоками изображений."""
    if not images:
        return text, []
    if "--input-format" not in known:
        return text + "\n\n[Картинки не переданы: этот Claude Code их не принимает — обнови CLI.]", []
    line = {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}, *images]}}
    return json.dumps(line, ensure_ascii=False) + "\n", ["--input-format", "stream-json"]


def found(provider: dict) -> str | None:
    cmd = command(provider)
    return shutil.which(cmd[0]) if cmd else None


async def check(provider: dict) -> dict:
    """Найден ли `claude` и вошёл ли хозяин — без вызова модели (`--version`, `auth status`)."""
    path = found(provider)
    out = {"found": bool(path), "path": path, "command": command(provider), "version": None, "logged_in": False, "auth_method": None, "billing": None}
    if not path:
        return out
    exe = command(provider)
    code, text = await _run(exe + ["--version"])
    out["version"] = text.strip().splitlines()[0][:80] if code == 0 and text.strip() else None
    code, text = await _run(exe + ["auth", "status"])
    try:
        st = json.loads(text[text.index("{"):]) if "{" in text else {}
    except ValueError:
        st = {}
    out["logged_in"] = bool(st.get("loggedIn"))
    out["auth_method"] = st.get("authMethod")
    m = str(st.get("authMethod") or "").lower()
    # вошёл ключом API — платит за токены; подпиской (claude.ai / OAuth) — в пределах подписки
    out["billing"] = "api" if "api" in m and "key" in m else ("subscription" if out["logged_in"] else None)
    if not st and code != 0:
        out["error"] = text.strip()[-300:]
    return out


def prompt_for(app, sid: str, state: dict, images: list | None = None) -> tuple[str, str | None]:
    """Что отправить в Claude Code: новые сообщения после последнего отправленного. Первый раз — с кратким
    прошлым разговора (если он был без Claude Code). Возвращает (текст, id последнего сообщения);
    картинки новых сообщений — в images."""
    images = [] if images is None else images
    upto = state.get("upto")
    summary = ""
    if not upto:  # новый разговор Claude Code: не раньше границы сжатия или «новой страницы»
        from ..mind import compact

        cst = compact.state(app, sid)
        upto = compact.boundary(cst)
        summary = cst.get("summary", "") if upto else ""
    rows = app.store.q("SELECT id, created_at, meta, data FROM messages WHERE session_id = ? AND role IN ('user', 'assistant', 'tool')" + (" AND id > ?" if upto else "") + " ORDER BY id",
                       (sid, upto) if upto else (sid,))
    from ..core.dates import content as labelled
    # реплики хозяина — с метками, как у API-моделей: время («[пт 06.11 19:23]») и номер сообщения Telegram
    msgs = []
    for r in rows:
        m = json.loads(r["data"])
        if m.get("role") == "user":
            m["content"] = labelled({**m, "created_at": r["created_at"], "meta": json.loads(r["meta"] or "{}")})
        msgs.append((r["id"], m))
    if not msgs:
        return "", upto
    last_assistant = max((i for i, (_, m) in enumerate(msgs) if m.get("role") == "assistant"), default=-1)
    new = [m for _, m in msgs[last_assistant + 1:] if m.get("role") == "user"]
    parts = []
    if summary and not state.get("session"):
        parts.append("[Пересказ начала разговора:\n" + summary.strip() + "]")
    if not state.get("session") and last_assistant >= 0:
        from ..mind.side import transcript

        hist = [m for _, m in msgs[: last_assistant + 1]][-40:]
        parts.append("[Начало этого разговора шло без тебя — кратко, что было:\n" + transcript(hist, limit_chars=6000) + "]")
    for m in new:
        parts.append(flatten(m.get("content"), images))
    return "\n\n".join(parts).strip(), msgs[-1][0]


def stateless_prompt(messages: list[dict], images: list | None = None) -> str:
    """Разовый вызов: одно сообщение — как есть; разговор — стенограммой и последним сообщением.
    Картинки последнего сообщения — в images."""
    images = [] if images is None else images
    rest = [m for m in messages if m.get("role") != "system"]
    if len(rest) == 1 and isinstance(rest[0].get("content"), str):
        return rest[0]["content"]
    from ..mind.side import transcript

    last = rest[-1] if rest else {}
    body = transcript(rest[:-1], limit_chars=12000) if len(rest) > 1 else ""
    tail = flatten(last.get("content"), images)
    return (f"[Разговор до этого:\n{body}]\n\n" if body else "") + (tail or "")


async def run_claude(router, cand, messages: list[dict], tools, params: dict, relay) -> Result:
    p = cand.provider
    pname = p.get("name") or "Claude Code"
    turn = TURN.get()
    bridge = getattr(router, "cli_bridge", None)
    exe = command(p)
    if not exe or not shutil.which(exe[0]):
        raise ProviderError("not_found", provider=pname, model=cand.model, message=f"Не нашёл «{exe[0] if exe else 'claude'}». Поставь Claude Code и войди в него (claude, потом /login).")
    system = next((m["content"] for m in messages if m.get("role") == "system" and isinstance(m.get("content"), str)), "")
    # С тулами в ходе агента — свой разговор Claude Code (продолжается) и тулы Бендера через петлевой MCP.
    # Без тулов (проверка модели, разбор, пульс, Чтец) — разовый вызов: история — текстом, сессия не сохраняется.
    stateful = turn is not None and bridge is not None and bool(tools)
    token = None
    extra={}
    if os.name=='nt' and bridge is not None:
        from ..guard.windows_job import parameters
        extra['protection']=parameters(bridge.app)
    known = await flags(exe,**extra)
    from .effort import describe
    effort=describe(p,cand.model,params)
    effort_args=[]
    if effort['effective'] in ('low','medium','high','xhigh','max'):
        if '--effort' not in known:
            raise ProviderError('format',provider=pname,model=cand.model,message='Установленный Claude Code не поддерживает --effort. Обнови CLI или выбери усилия default.')
        effort_args=['--effort',effort['effective']]
    args = exe + ["-p", "--output-format", "stream-json", "--verbose", "--tools", "", "--strict-mcp-config"]
    args += effort_args
    if "--include-partial-messages" in known:
        args += ["--include-partial-messages"]  # текст по кусочкам; без него — целиком в конце
    if "--restricted" in known:
        args += ["--restricted"]  # без настроек хозяина (хуки, свои MCP) — только то, что дал Бендер
    if "--permission-prompts" in known:
        args += ["--permission-prompts", "none"]
    if "--disable-slash-commands" in known:
        args += ["--disable-slash-commands"]
    if "--system-prompt-snapshot" in known:
        args += ["--system-prompt-snapshot", "off"]  # свежая память Бендера — каждый ход
    if system and stateful:
        system += TOOL_NAMES
    if system:
        args += ["--system-prompt", system]
    if cand.model and cand.model.strip().lower() != "default":
        args += ["--model", cand.model]
    if stateful:
        app = bridge.app
        sid = turn.session_id
        meta_key = f"cc:{p['id']}"
        sess = app.store.session(sid) or {}
        state = dict((sess.get("meta") or {}).get(meta_key) or {})
        images: list = []
        prompt, last_id = prompt_for(app, sid, state, images)
        if turn.skill:
            prompt = next((m["content"] for m in reversed(messages) if m.get("role") == "user" and isinstance(m.get("content"), str)), prompt)
            images = []
        elif prompt:
            # время, канал, «Вспомнилось» — то же, что API-модели видят приставкой к последней реплике хозяина;
            # в конце нового сообщения, поэтому кэш разговора не ломает
            prompt = str(turn.state.get("_note") or "") + prompt
        if not prompt:
            raise ProviderError("server", provider=pname, model=cand.model, message="Нечего отправлять в Claude Code: нет нового сообщения")
        url, token = await bridge.open(turn, turn.tool_names)
        cfg = {"mcpServers": {"bender": {"type": "http", "url": url, "headers": {"Authorization": f"Bearer {token}"}}}}
        cc_session = state.get("session") or str(uuid.uuid4())
        args += ["--mcp-config", json.dumps(cfg), "--allowedTools", "mcp__bender"]
        args += ["--resume", cc_session] if state.get("session") else ["--session-id", cc_session]
        cwd = Path(app.dirs.local) / "claude-code"
    else:
        app = bridge.app if bridge is not None else None
        images = []
        prompt = stateless_prompt(messages, images)
        cc_session = ""
        if "--no-session-persistence" in known:
            args += ["--no-session-persistence"]
        cwd = (Path(app.dirs.local) if app else Path.home() / ".bender-tmp") / "claude-code"
    prompt, input_args = payload(prompt, images, known)
    args += input_args
    cwd.mkdir(parents=True, exist_ok=True)  # пустая папка: без чужих CLAUDE.md и настроек проекта
    # без BENDER_* и без отключённого кэша; чужие CLAUDE.md и автопамять Claude Code в ход Бендера не попадают
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BENDER_", "DISABLE_PROMPT_CACHING"))}
    env.update(CLAUDE_CODE_DISABLE_CLAUDE_MDS="1", CLAUDE_CODE_DISABLE_AUTO_MEMORY="1")
    if effort_args:
        # У Claude Code переменная главнее --effort и молча перебила бы выбор. Блок env в управляемых настройках
        # (и в своих — у версий без --restricted) Claude Code применяет сам: это остаётся выбором хозяина машины.
        env.pop("CLAUDE_CODE_EFFORT_LEVEL", None)
    res = Result(provider_id=p["id"], provider_name=pname, model=cand.model)
    text_parts: list[str] = []
    final = None
    streamed = False

    def event(ev):
        nonlocal final, streamed, cc_session
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init" and ev.get("session_id"):
            cc_session = ev["session_id"]
        elif t == "stream_event":
            e = ev.get("event") or {}
            d = e.get("delta") or {}
            if e.get("type") == "content_block_delta" and d.get("type") == "text_delta" and d.get("text"):
                streamed = True
                text_parts.append(d["text"])
                relay("text", d["text"])
            elif e.get("type") == "content_block_delta" and d.get("type") == "thinking_delta" and d.get("thinking"):
                relay("reasoning", d["thinking"])
        elif t == "assistant" and not streamed:
            for blk in (ev.get("message") or {}).get("content") or []:
                if blk.get("type") == "text" and blk.get("text"):
                    text_parts.append(blk["text"])
                    relay("text", blk["text"])
        elif t == "rate_limit_event" and router.observe:
            router.observe(cand, 0, "claude", ev.get("rate_limit_info") or {})
        elif t == "result":
            final = ev
    try:
        returncode, err_text = await stream(args, prompt=prompt, cwd=cwd, env=env,
                                           timeout=float(p.get("timeout") or 1800), on_event=event,**extra)
    except asyncio.TimeoutError:
        raise ProviderError("timeout", provider=pname, model=cand.model, message="Claude Code молчит слишком долго", partial={"actions": bool(token and bridge.calls(token))}) from None
    finally:
        calls = bridge.calls(token) if token else 0
        if token:
            bridge.close(token)
    if final is None or final.get("is_error") or final.get("subtype", "success") != "success":
        msg = (final or {}).get("result") or (final or {}).get("subtype") or err_text or f"код выхода {returncode}"
        raise ProviderError(_classify(f"{msg} {err_text}"), provider=pname, model=cand.model, message=str(msg)[:500], raw=err_text[-1500:],
                            partial={"text": "".join(text_parts), "actions": bool(calls)})
    if stateful:  # запомнить сессию Claude Code и что уже отправлено
        app.store.update_session(sid, meta={meta_key: {"session": final.get("session_id") or cc_session, "upto": last_id}})
    res.text = str(final.get("result") or "".join(text_parts)).strip()
    u = final.get("usage") or {}
    res.usage = Usage(input=int(u.get("input_tokens") or 0), output=int(u.get("output_tokens") or 0), cache_read=int(u.get("cache_read_input_tokens") or 0),
                      cache_write=int(u.get("cache_creation_input_tokens") or 0))
    api_cost = final.get("total_cost_usd")
    if p.get("billing") == "api" and api_cost is not None:
        res.usage.cost = float(api_cost)  # вошёл ключом API — это настоящие деньги; по подписке ход денег не стоит
    res.finish = "stop"
    return res
