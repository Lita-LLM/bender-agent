"""Фальшивый сервер моделей: OpenAI- и Anthropic-совместимый API, который ничего не стоит.

Нужен для тестов и чтобы потрогать Бендера без токенов: `bender fake-llm` → провайдер
`http://127.0.0.1:5199/v1`. Поведение выбирается названием модели:

    echo            «Эхо: …» — пересказ последнего сообщения (по умолчанию)
    slow            медленно, по слову в 0.2 с
    think           сначала размышления (reasoning_content / thinking), потом ответ
    tools           вызывает тул: если в сообщении есть `tool:ИМЯ {json}` — его, иначе первый предложенный;
                    после результата тула пишет «Результат: …»
    script          ответы по очереди из сценария (POST /fake/script — список шагов; см. ниже)
    notools         как script, но не умеет тулы: на поле tools — 400 «does not support tools»,
                    а вызовы тулов из сценария пишет текстом <tool_call>{…}</tool_call>
    empty           пустой ответ
    nostream        игнорирует stream и отвечает одним JSON
    cut             поток обрывается на середине
    long            очень длинный ответ
    cost            в usage приходит стоимость
    error-NNN       HTTP-ошибка NNN с телом как у OpenAI (429 — с Retry-After: 1)
    error-context   400 «maximum context length exceeded»
    small-ctx       как echo, но «не влезло в контекст», если в несистемных сообщениях больше 8000 символов
    error-billing   402 «insufficient credits»
    flaky-N         первые N запросов — 503, потом как echo (для проверки повторов)
    seer            видит картинки: «Вижу изображений: N. Текст: …»; на пробу «цвет квадрата» отвечает цветом PNG
    blind           принимает картинку, но молча её теряет, как шлюз к текстовой модели: «Изображения не вижу. Текст: …»
    noimage         с картинкой — 400 «does not support image input», без неё — «Текст: …»

Эмбеддинги: POST /v1/embeddings — детерминированные векторы 64 измерений из основ слов (хеширование),
с маленьким словарём синонимов (кот/кошка, машина/автомобиль, …), чтобы поиск по смыслу находил то, чего
нет словами. Модель embed-error — ошибка 500.

Ещё он притворяется OpenAI для входа через ChatGPT (адреса — как у auth.openai.com и
chatgpt.com/backend-api/codex): /api/accounts/deviceauth/usercode и /token (код подтверждается со
второго опроса), /oauth/token (выдача и обновление токенов; refresh-токен «revoked» — отказ),
/backend-api/codex/responses (Responses API потоком; модели те же; error-429 — «usage limit»).

Шаг сценария: {"text": "…"} | {"tool_calls": [{"name": "…", "arguments": {…}}]} |
{"error": 429, "message": "…"} | {"reasoning": "…", "text": "…"}. Шаги общие для всех запросов
модели script; GET /fake/requests — что приходило (для проверок «что ушло в модель»).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid

from ..net.http_server import HttpServer, JSONResponse, Response, Router, StreamResponse, sse

MODELS = ["echo", "slow", "think", "tools", "script", "notools", "empty", "nostream", "cut", "long", "cost",
          "error-401", "error-403", "error-404", "error-429", "error-500", "error-503", "error-529",
          "error-context", "error-billing", "flaky-2", "small-ctx", "seer", "blind", "noimage"]


class FakeState:
    def __init__(self):
        self.script: list[dict] = []
        self.requests: list[dict] = []
        self.flaky_counts: dict[str, int] = {}
        self.device_polls = 0
        self.issued = 0                    # сколько access-токенов выдано
        self.valid_access: set[str] = set()
        self.refreshes = 0
        # поддельный Telegram Bot API: /tg/bot<токен>/<метод>
        self.tg_token = "123:FAKE"
        self.tg_updates: list[dict] = []
        self.tg_files = {}
        self.tg_getfile = []  # file_id из запросов getFile — проверка, что дубль не качается
        self.tg_rich = False
        self.tg_commands = {}
        self.tg_command_calls = []
        self.tg_profile = {}
        self.tg_topics = False
        self.tg_topic_next = 100
        self.tg_missing_threads = set()
        self.tg_sent: list[dict] = []
        self.tg_next_id = 1
        self.tg_event: asyncio.Event | None = None


def _last_user_text(messages: list[dict]) -> str:
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content")
            if isinstance(c, str):
                return c
            if isinstance(c, list):
                return " ".join(p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text")
    return ""


def _images(messages: list[dict]) -> int:
    """Сколько изображений в последнем сообщении хозяина (OpenAI image_url или Anthropic image)."""
    for m in reversed(messages):
        if m.get("role") == "user":
            c = m.get("content")
            return sum(1 for p in c if isinstance(p, dict) and p.get("type") in ("image_url", "image", "input_image")) if isinstance(c, list) else 0
    return 0


def _png_color(messages: list[dict]) -> str | None:
    """Цвет первого пикселя первой PNG-картинки последнего сообщения хозяина — чтобы seer «видел» пробу зрения."""
    import base64
    import struct
    import zlib
    for m in reversed(messages):
        if m.get("role") != "user":
            continue
        for p in m.get("content") if isinstance(m.get("content"), list) else []:
            if not isinstance(p, dict):
                continue
            data = (p.get("image_url") or {}).get("url", "").split(",", 1)[-1] if p.get("type") == "image_url" else \
                (p.get("source") or {}).get("data", "") if p.get("type") == "image" else str(p.get("image_url") or "").split(",", 1)[-1] if p.get("type") == "input_image" else ""
            try:
                raw = base64.b64decode(data)
                offset, packed = 8, b""
                while offset < len(raw):
                    size = struct.unpack_from("!I", raw, offset)[0]
                    if raw[offset + 4:offset + 8] == b"IDAT":
                        packed += raw[offset + 8:offset + 8 + size]
                    offset += size + 12
                r, g, b = zlib.decompress(packed)[1:4]
            except Exception:  # noqa: BLE001 — не PNG: цвета не знаем
                continue
            return "красный" if r >= max(g, b) else "синий" if b >= g else "зелёный"
        return None
    return None


def _last_tool_result(messages: list[dict]) -> str | None:
    """Если последнее сообщение — результат тула (OpenAI role=tool или Anthropic tool_result), вернуть его текст."""
    if not messages:
        return None
    m = messages[-1]
    if m.get("role") == "tool":
        c = m.get("content")
        return c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
    if m.get("role") == "user" and isinstance(m.get("content"), list):
        for p in m["content"]:
            if isinstance(p, dict) and p.get("type") == "tool_result":
                c = p.get("content")
                if isinstance(c, list):
                    c = " ".join(x.get("text", "") for x in c if isinstance(x, dict))
                return c if isinstance(c, str) else json.dumps(c, ensure_ascii=False)
    return None


def _tool_names(tools: list[dict] | None) -> list[str]:
    out = []
    for t in tools or []:
        if "function" in t:
            out.append(t["function"].get("name", ""))
        elif "name" in t:
            out.append(t["name"])
    return out


def decide(model: str, body: dict, messages: list[dict], tools: list[dict] | None, state: FakeState) -> dict:
    """Общее решение «что ответить» — потом рисуется в формате OpenAI или Anthropic."""
    if model.startswith("flaky-"):
        n = int(model.split("-", 1)[1] or 1)
        seen = state.flaky_counts.get(model, 0)
        state.flaky_counts[model] = seen + 1
        if seen < n:
            return {"error": 503, "message": "Временно недоступно (фальшивый сбой)"}
        model = "echo"
    m = re.match(r"error-(\d{3})$", model)
    if m:
        code = int(m.group(1))
        msgs = {401: "Invalid API key", 403: "Forbidden", 404: "The model `x` does not exist", 429: "Rate limit exceeded",
                500: "Internal server error", 503: "Service unavailable", 529: "Overloaded"}
        return {"error": code, "message": msgs.get(code, f"Error {code}")}
    if model == "small-ctx":
        size = sum(len(m.get("content") if isinstance(m.get("content"), str) else json.dumps(m.get("content") or "", ensure_ascii=False))
                   for m in messages if m.get("role") != "system")
        if size > 8000:
            return {"error": 400, "message": f"This model's maximum context length is 2048 tokens. However, your messages resulted in {size // 4} tokens."}
        model = "echo"
    if model == "error-context":
        return {"error": 400, "message": "This model's maximum context length is 8192 tokens. However, your messages resulted in 9001 tokens."}
    if model == "error-billing":
        return {"error": 402, "message": "Insufficient credits. Add more credits to continue."}
    user = _last_user_text(messages)
    tool_result = _last_tool_result(messages)
    if model in ("seer", "blind", "noimage"):
        pictures = _images(messages)
        if model == "noimage" and pictures:
            return {"error": 400, "message": "This model does not support image input"}
        if model == "seer" and pictures and "цвет квадрата" in user:
            color = _png_color(messages)
            if color:
                return {"text": color}
        head = {"seer": f"Вижу изображений: {pictures}. ", "blind": "Изображения не вижу. "}.get(model, "")
        return {"text": head + "Текст: " + user}
    if model == "notools":
        if tools:
            return {"error": 400, "message": f"registry.ollama.ai/library/{model} does not support tools"}
        if not state.script:
            return {"text": "(сценарий пуст)"}
        step = dict(state.script.pop(0))
        calls = step.pop("tool_calls", None) or []
        blocks = [f"<tool_call>\n{json.dumps({'name': c['name'], 'arguments': c.get('arguments', {})}, ensure_ascii=False)}\n</tool_call>" for c in calls]
        step["text"] = "\n".join(([step["text"]] if step.get("text") else []) + blocks)
        return step
    if model == "script":
        if not state.script:
            return {"text": "(сценарий пуст)"}
        step = state.script.pop(0)
        if "error" in step:
            return {"error": int(step["error"]), "message": step.get("message", "Ошибка по сценарию")}
        return step
    if model == "tools":
        if tool_result is not None:
            return {"text": f"Результат: {tool_result}"}
        names = _tool_names(tools)
        mo = re.search(r"tool:(\w+)\s*(\{.*\})?", user, re.S)
        if mo:
            return {"tool_calls": [{"name": mo.group(1), "arguments": json.loads(mo.group(2) or "{}")}]}
        if names:
            return {"tool_calls": [{"name": names[0], "arguments": {}}]}
        return {"text": "Тулов не предложили."}
    if model == "think":
        return {"reasoning": "Так, подумаю… Хозяин написал: " + user[:80], "text": "Подумал. Эхо: " + user}
    if model == "empty":
        return {"text": ""}
    if model == "long":
        return {"text": " ".join(f"слово{i}" for i in range(3000))}
    out = {"text": ("Эхо: " + user) if user else "Эхо: (пусто)"}
    if tool_result is not None:
        out = {"text": f"Результат: {tool_result}"}
    if model == "slow":
        out["delay"] = 0.2
    if model == "cut":
        out["cut"] = True
    if model == "cost":
        out["cost"] = 0.0123
    return out


def _usage(body_messages: list[dict], text: str, cost: float | None = None) -> dict:
    prompt = sum(len(json.dumps(m, ensure_ascii=False)) for m in body_messages) // 4 + 1
    completion = len(text) // 4 + 1
    u = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}
    if cost is not None:
        u["cost"] = cost
    return u


def _words(text: str) -> list[str]:
    return re.findall(r"\S+\s*|\s+", text) or [""]


def _openai_error(status: int, message: str) -> Response:
    headers = {"Retry-After": "1"} if status == 429 else {}
    kind = {401: "invalid_api_key", 402: "insufficient_quota", 429: "rate_limit_exceeded"}.get(status, "error")
    return JSONResponse({"error": {"message": message, "type": kind, "code": kind}}, status, headers)


def _jwt(payload: dict) -> str:
    import base64

    enc = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{enc({'alg': 'none'})}.{enc(payload)}.sig"


def _issue(state: FakeState, expires_in: int = 3600) -> dict:
    state.issued += 1
    access = _jwt({"exp": int(time.time()) + expires_in, "n": state.issued, "https://api.openai.com/auth": {"chatgpt_account_id": "acc_fake"}})
    state.valid_access.add(access)
    return {"access_token": access, "refresh_token": f"refresh-{state.issued}", "expires_in": expires_in,
            "id_token": _jwt({"email": "owner@example.com"}), "token_type": "Bearer"}


SYNONYMS = {"кошк": "кот", "котик": "кот", "автомобил": "машин", "тачк": "машин", "доктор": "врач", "лекар": "врач",
            "деньг": "финанс", "бюджет": "финанс", "car": "машин", "cat": "кот"}


def fake_embedding(text: str, dim: int = 64) -> list[float]:
    import hashlib
    import math

    from ..memory.stem import stems

    vec = [0.0] * dim
    for s in stems(text):
        s = SYNONYMS.get(s, s)
        h = int(hashlib.md5(s.encode()).hexdigest(), 16)
        for k in range(3):  # каждое слово — три случайных измерения со знаком
            i = (h >> (k * 8)) % dim
            vec[i] += 1.0 if (h >> (k * 8 + 7)) & 1 else -1.0
    n = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / n for v in vec]


def _responses_to_messages(body: dict) -> list[dict]:
    """Responses API input → сообщения в духе Chat Completions (чтобы решать, что ответить, общим decide)."""
    out = [{"role": "system", "content": body.get("instructions") or ""}]
    for it in body.get("input") or []:
        t = it.get("type")
        if t == "message":
            text = " ".join(p.get("text", "") for p in it.get("content") or [] if isinstance(p, dict))
            out.append({"role": it.get("role"), "content": text})
        elif t == "function_call":
            out.append({"role": "assistant", "content": "", "tool_calls": [{"id": it.get("call_id"), "function": {"name": it.get("name"), "arguments": it.get("arguments")}}]})
        elif t == "function_call_output":
            out.append({"role": "tool", "tool_call_id": it.get("call_id"), "content": it.get("output", "")})
    return out


def create_router(state: FakeState | None = None) -> Router:
    state = state or FakeState()
    r = Router()
    r.state = state  # type: ignore[attr-defined]

    # Поиск без внешней сети: один маршрут повторяет формы четырёх поставщиков.
    @r.route("GET", "/fake/search/{provider}")
    @r.route("POST", "/fake/search/{provider}")
    async def search(req):
        from html import escape

        provider = req.params["provider"]
        query = req.q("q") or (req.json().get("query", "") if req.method == "POST" else "")
        state.requests.append({"search": provider, "query": query, "method": req.method})
        if query == "429":
            return JSONResponse({"error": "Лимит запросов"}, 429)
        if query == "captcha" and provider == "duckduckgo":
            return Response(b'<html><form id="challenge-form" action="/anomaly.js">CAPTCHA</form></html>', headers={"Content-Type": "text/html"})
        if query == "malformed":
            return JSONResponse({"results": "не список"})
        if provider in ("brave", "tavily") and not req.headers.get("X-Subscription-Token" if provider == "brave" else "Authorization"):
            return JSONResponse({"error": "Нужен ключ"}, 401)
        rows = [{"title": "Результат <b>" + query + "</b>", "url": "https://example.org/report", "content": "Краткий ответ"},
                {"title": "Опасный адрес", "url": "javascript:alert(1)"}]
        if provider == "duckduckgo":
            page = '<html><div class="result"><h2><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.org%2Freport">Результат <b>' + escape(query) + '</b></a></h2><a class="result__snippet">Краткий ответ</a></div></html>'
            return Response(page.encode(), headers={"Content-Type": "text/html"})
        if provider == "brave":
            return JSONResponse({"web": {"results": [{**r, "description": r.get("content", "")} for r in rows]}})
        return JSONResponse({"results": rows})

    @r.route("GET", "/v1/models")
    async def models(req):
        return JSONResponse({"object": "list", "data": [{"id": m, "object": "model", "owned_by": "fake", "context_length": 8192} for m in MODELS]})

    @r.route('POST','/v1beta/models/{model}:generateContent')
    async def gemini_audio(req):
        import base64
        data=req.json();state.requests.append({'api':'gemini_audio','model':req.params['model'],'body':data,'google_key_present':bool(req.headers.get('x-goog-api-key')),'bearer_present':bool(req.headers.get('Authorization'))})
        if 'error' in req.params['model']:return JSONResponse({'error':{'message':'аудиомодель временно недоступна'}},503)
        if (data.get('generationConfig') or {}).get('responseModalities')==['AUDIO']:
            parts=[{'inlineData':{'mimeType':'audio/L16;codec=pcm;rate=24000','data':base64.b64encode(b'\0\0'*3600).decode()}}]
        else:parts=[{'text':'Расшифровка Gemini по-русски.'}]
        return JSONResponse({'candidates':[{'content':{'parts':parts,'role':'model'}}]})

    @r.route("POST", "/inference")
    @r.route("POST", "/v1/audio/transcriptions")
    async def audio_transcriptions(req):
        match=re.search(rb'name="model"\r\n\r\n([^\r]+)',req.body)
        from email import policy
        from email.parser import BytesParser
        message=BytesParser(policy=policy.default).parsebytes(('Content-Type: '+req.headers.get('Content-Type','')+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+req.body)
        files=[part.get_payload(decode=True) for part in message.iter_parts() if part.get_param('name',header='content-disposition')=='file']
        state.requests.append({'api':'audio_stt','model':match[1].decode() if match else '', 'file_magic':files[0][:4].hex() if files else '', 'file_bytes':len(files[0]) if files else 0})
        if match and b"error" in match.group(1):
            return JSONResponse({"error":{"message":"аудиомодель временно недоступна"}},503)
        if b'name="file"' not in req.body:
            return JSONResponse({"error":"нужен multipart файл"},400)
        return JSONResponse({"text":"Привет голосом"})

    @r.route("POST", "/piper")
    @r.route("POST", "/v1/audio/speech")
    async def audio_speech(req):
        from pathlib import Path
        data=req.json()
        if "error" in data.get("model",""):
            return JSONResponse({"error":{"message":"аудиомодель временно недоступна"}},503)
        fmt="wav" if req.path=="/piper" else data.get("response_format","mp3")
        if fmt=="wav":
            import io,wave
            buf=io.BytesIO()
            with wave.open(buf,"wb") as wav:
                wav.setnchannels(1);wav.setsampwidth(2);wav.setframerate(24000);wav.writeframes(b"\0\0"*3600)
            raw=buf.getvalue()
        else:
            raw=Path(__file__).with_name("fake-voice.ogg" if fmt=="opus" else "fake-voice.mp3").read_bytes()
        return Response(raw,headers={"Content-Type":{"opus":"audio/ogg","mp3":"audio/mpeg","wav":"audio/wav"}[fmt]})

    @r.route("GET", "/tg/file/{bot}/{path:rest}")
    async def tg_voice_file(req):
        from pathlib import Path
        item=next((f for f in state.tg_files.values() if f['file_path']==req.params['path']),None)
        return Response(item['raw'] if item else Path(__file__).with_name("fake-voice.ogg").read_bytes(),headers={"Content-Type":item.get('mime','application/octet-stream') if item else 'audio/ogg'})

    # --- поддельный Telegram ---------------------------------------------------------------

    def tg_ok(result):
        return JSONResponse({"ok": True, "result": result})

    @r.route("POST", "/fake/tg/update")
    async def tg_push(req):
        """Положить входящее: {"text": …, "from": {"id": …}} или готовый update (с message / callback_query)."""
        b = req.json() or {}
        uid = state.tg_next_id
        state.tg_next_id += 1
        if "message" in b or "edited_message" in b or "callback_query" in b:
            upd = {"update_id": uid, **b}
        else:
            frm = {"id": 42, "is_bot": False, "first_name": "Хозяин", "username": "owner", **(b.get("from") or {})}
            upd = {"update_id": uid, "message": {"message_id": 1000 + uid, "from": frm, "chat": {"id": frm["id"], "type": b.get("chat_type", "private")},
                                                 "date": int(time.time()), "text": b.get("text", "")}}
        state.tg_updates.append(upd)
        if state.tg_event:
            state.tg_event.set()
        return JSONResponse({"ok": True, "update_id": uid})

    # --- поддельная почта (сами IMAP и SMTP поднимает main: порт+1 и порт+2) ----------------

    from .fake_mail import Mailbox

    state.mailbox = Mailbox()

    @r.route("POST", "/fake/mail/add")
    async def mail_add(req):
        """Положить письмо: {"from": "Имя <a@b>", "subject": …, "text": …} или {"raw": "…"}."""
        b = req.json() or {}
        if b.get("raw"):
            uid = state.mailbox.add(b["raw"].encode())
        else:
            uid = state.mailbox.add(frm=b.get("from") or "Вася <vasya@example.org>", subject=b.get("subject") or "Привет", text=b.get("text") or "Текст письма")
        return JSONResponse({"ok": True, "uid": uid})

    @r.route("GET", "/fake/mail/sent")
    async def mail_sent(req):
        return JSONResponse(state.mailbox.sent)

    @r.route('POST','/fake/tg/features')
    async def tg_features(req):
        values=req.json() or {}
        if 'rich' in values:state.tg_rich=bool(values['rich'])
        if 'topics' in values:state.tg_topics=bool(values['topics'])
        return JSONResponse({'ok':True})

    @r.route('GET','/fake/tg/commands')
    async def tg_commands(req):
        return JSONResponse({'calls':state.tg_command_calls,'menus':[{'scope':key[0],'language':key[1],'commands':value} for key,value in state.tg_commands.items()]})

    @r.route("GET", "/fake/tg/sent")
    async def tg_sent(req):
        return JSONResponse(state.tg_sent)

    @r.route("POST", "/tg/{bot}/{method}")
    async def tg_api(req):
        bot, method = req.params["bot"], req.params["method"]
        if bot != "bot" + state.tg_token:
            return JSONResponse({"ok": False, "error_code": 401, "description": "Unauthorized"}, 401)
        if 'multipart/form-data' in req.headers.get('Content-Type',''):
            from email import policy
            from email.parser import BytesParser
            parsed=BytesParser(policy=policy.default).parsebytes(('Content-Type: '+req.headers.get('Content-Type')+'\r\nMIME-Version: 1.0\r\n\r\n').encode()+req.body)
            fields={};files={}
            for part in parsed.iter_parts():
                key=part.get_param('name',header='content-disposition')
                if part.get_filename():files[key]={'name':part.get_filename(),'bytes':len(part.get_payload(decode=True))}
                else:fields[key]=part.get_payload(decode=True).decode()
            entry={'method':method,**fields,'files':files,'bytes':len(req.body),'message_id':5000+len(state.tg_sent)}
            state.tg_sent.append(entry)
            if int(fields.get('message_thread_id') or 0) in state.tg_missing_threads:
                entry['rejected']=True
                return JSONResponse({'ok':False,'error_code':400,'description':'Bad Request: message thread not found'},400)
            if method=='sendMediaGroup':return tg_ok([{'message_id':entry['message_id']+n} for n in range(len(json.loads(fields['media'])))])
            return tg_ok({'message_id':entry['message_id']})
        b = req.json() if req.body else {}
        b = b or {}
        if b.get('message_thread_id') in state.tg_missing_threads:
            state.tg_sent.append({'method':method,**b,'rejected':True})
            return JSONResponse({'ok':False,'error_code':400,'description':'Bad Request: message thread not found'},400)
        if method in ('createForumTopic','editForumTopic'):
            state.tg_sent.append({'method':method,**b})
            if method=='editForumTopic':return tg_ok(True)
            state.tg_topic_next+=1
            return tg_ok({'message_thread_id':state.tg_topic_next,'name':b['name'],'icon_color':7322096})
        if method=="getFile":
            state.tg_getfile.append(b.get('file_id'))
            item=state.tg_files.get(b.get('file_id'))
            return tg_ok({'file_path':item['file_path'],'file_size':len(item['raw'])} if item else {"file_path":"voice.ogg"})
        if method == "getMe":
            return tg_ok({"id": 777, "is_bot": True, "first_name": "Бендер", "username": "bender_test_bot",'has_topics_enabled':state.tg_topics})
        if method == "getUpdates":
            state.tg_poll=dict(b)
            off = int(b.get("offset") or 0)
            state.tg_updates = [u for u in state.tg_updates if u["update_id"] >= off]
            if not state.tg_updates:
                state.tg_event = state.tg_event or asyncio.Event()
                state.tg_event.clear()
                try:
                    await asyncio.wait_for(state.tg_event.wait(), min(float(b.get("timeout") or 0), 2.0))
                except asyncio.TimeoutError:
                    pass
            return tg_ok([u for u in state.tg_updates if u["update_id"] >= off])
        if method=='leaveChat':
            state.tg_sent.append({'method':method,**b})
            return tg_ok(True)
        if method in ('setMyName','setMyDescription','setMyShortDescription'):
            state.tg_sent.append({'method':method,**b})
            state.tg_profile[method+':'+b.get('language_code','')]=dict(b)
            return tg_ok(True)
        if method in ('getMyCommands','setMyCommands','deleteMyCommands'):
            state.tg_command_calls.append({'method':method,**b})
            key=((b.get('scope') or {}).get('type','default'),b.get('language_code',''))
            if method=='getMyCommands':return tg_ok(state.tg_commands.get(key,[]))
            if method=='deleteMyCommands':state.tg_commands.pop(key,None)
            else:state.tg_commands[key]=b.get('commands',[])
            return tg_ok(True)
        if method in ('sendRichMessage','sendRichMessageDraft') and state.tg_rich:
            entry={'method':method,**b,'message_id':5000+len(state.tg_sent)}
            state.tg_sent.append(entry)
            return tg_ok({'message_id':entry['message_id']} if method=='sendRichMessage' else True)
        if method in ("setMessageReaction", "sendMessage", "editMessageReplyMarkup", "editMessageText", "answerCallbackQuery", "sendChatAction", "sendMessageDraft", "deleteMessage"):
            entry = {"method": method, **b}
            if method == "sendMessage":
                entry["message_id"] = 5000 + len(state.tg_sent)
            state.tg_sent.append(entry)
            return tg_ok({"message_id": entry.get("message_id", 1), "chat": {"id": b.get("chat_id")}} if method == "sendMessage" else True)
        return JSONResponse({"ok": False, "error_code": 404, "description": "Not Found: method"}, 404)

    @r.route("POST", "/fake/script")
    async def set_script(req):
        state.script = list(req.json() or [])
        return JSONResponse({"ok": True, "steps": len(state.script)})

    @r.route("GET", "/fake/requests")
    async def get_requests(req):
        return JSONResponse(state.requests[-50:])

    @r.route("POST", "/v1/chat/completions")
    async def chat(req):
        body = req.json() or {}
        state.requests.append({"api": "openai", "body": body, "headers": req.headers.to_dict()})
        model = body.get("model") or "echo"
        messages = body.get("messages") or []
        res = decide(model, body, messages, body.get("tools"), state)
        if "error" in res:
            return _openai_error(res["error"], res["message"])
        text = res.get("text", "")
        reasoning = res.get("reasoning")
        calls = res.get("tool_calls") or []
        cid = "chatcmpl-" + uuid.uuid4().hex[:12]
        created = int(time.time())
        usage = _usage(messages, text, res.get("cost"))
        finish = "tool_calls" if calls else "stop"
        tool_calls = [{"id": "call_" + uuid.uuid4().hex[:8], "type": "function", "function": {"name": c["name"], "arguments": json.dumps(c.get("arguments", {}), ensure_ascii=False)}} for c in calls]
        if not body.get("stream") or model == "nostream":
            msg = {"role": "assistant", "content": text or (None if calls else "")}
            if reasoning:
                msg["reasoning_content"] = reasoning
            if tool_calls:
                msg["tool_calls"] = tool_calls
            return JSONResponse({"id": cid, "object": "chat.completion", "created": created, "model": model,
                                 "choices": [{"index": 0, "message": msg, "finish_reason": finish}], "usage": usage})

        async def gen():
            def chunk(delta, finish_reason=None, with_usage=False):
                d = {"id": cid, "object": "chat.completion.chunk", "created": created, "model": model,
                     "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
                if with_usage:
                    d["usage"] = usage
                return sse(d)

            yield chunk({"role": "assistant", "content": ""})
            if reasoning:
                for w in _words(reasoning):
                    yield chunk({"reasoning_content": w})
            words = _words(text) if text else []
            for i, w in enumerate(words):
                if res.get("cut") and i >= len(words) // 2:
                    raise ConnectionAbortedError("фальшивый обрыв")  # соединение рвётся без завершающего куска
                yield chunk({"content": w})
                if res.get("delay"):
                    await asyncio.sleep(res["delay"])
            for i, tc in enumerate(tool_calls):
                args = tc["function"]["arguments"]
                half = len(args) // 2
                yield chunk({"tool_calls": [{"index": i, "id": tc["id"], "type": "function", "function": {"name": tc["function"]["name"], "arguments": args[:half]}}]})
                yield chunk({"tool_calls": [{"index": i, "function": {"arguments": args[half:]}}]})
            yield chunk({}, finish, with_usage=True)
            yield sse("[DONE]")

        return StreamResponse(gen())

    @r.route("POST", "/v1/messages")
    async def anthropic(req):
        body = req.json() or {}
        state.requests.append({"api": "anthropic", "body": body, "headers": req.headers.to_dict()})
        model = body.get("model") or "echo"
        messages = body.get("messages") or []
        res = decide(model, body, messages, body.get("tools"), state)
        if "error" in res:
            etype = {400: "invalid_request_error", 401: "authentication_error", 403: "permission_error", 404: "not_found_error",
                     429: "rate_limit_error", 529: "overloaded_error"}.get(res["error"], "api_error")
            headers = {"retry-after": "1"} if res["error"] == 429 else {}
            return JSONResponse({"type": "error", "error": {"type": etype, "message": res["message"]}}, res["error"], headers)
        text = res.get("text", "")
        reasoning = res.get("reasoning")
        calls = res.get("tool_calls") or []
        mid = "msg_" + uuid.uuid4().hex[:12]
        u = _usage(messages, text)
        usage = {"input_tokens": u["prompt_tokens"], "output_tokens": u["completion_tokens"]}
        stop = "tool_use" if calls else "end_turn"
        blocks = []
        if reasoning:
            blocks.append({"type": "thinking", "thinking": reasoning, "signature": "fake"})
        if text:
            blocks.append({"type": "text", "text": text})
        for c in calls:
            blocks.append({"type": "tool_use", "id": "toolu_" + uuid.uuid4().hex[:8], "name": c["name"], "input": c.get("arguments", {})})
        if not body.get("stream"):
            return JSONResponse({"id": mid, "type": "message", "role": "assistant", "model": model, "content": blocks,
                                 "stop_reason": stop, "usage": usage})

        async def gen():
            yield sse({"type": "message_start", "message": {"id": mid, "type": "message", "role": "assistant", "model": model, "content": [],
                                                            "stop_reason": None, "usage": {"input_tokens": usage["input_tokens"], "output_tokens": 1}}}, event="message_start")
            for i, b in enumerate(blocks):
                if b["type"] == "thinking":
                    yield sse({"type": "content_block_start", "index": i, "content_block": {"type": "thinking", "thinking": ""}}, event="content_block_start")
                    for w in _words(b["thinking"]):
                        yield sse({"type": "content_block_delta", "index": i, "delta": {"type": "thinking_delta", "thinking": w}}, event="content_block_delta")
                    yield sse({"type": "content_block_delta", "index": i, "delta": {"type": "signature_delta", "signature": "fake"}}, event="content_block_delta")
                elif b["type"] == "text":
                    yield sse({"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}}, event="content_block_start")
                    words = _words(b["text"])
                    for j, w in enumerate(words):
                        if res.get("cut") and j >= len(words) // 2:
                            raise ConnectionAbortedError("фальшивый обрыв")
                        yield sse({"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": w}}, event="content_block_delta")
                        if res.get("delay"):
                            await asyncio.sleep(res["delay"])
                else:
                    yield sse({"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}}, event="content_block_start")
                    args = json.dumps(b["input"], ensure_ascii=False)
                    half = len(args) // 2
                    for part in (args[:half], args[half:]):
                        yield sse({"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": part}}, event="content_block_delta")
                yield sse({"type": "content_block_stop", "index": i}, event="content_block_stop")
            yield sse({"type": "message_delta", "delta": {"stop_reason": stop}, "usage": {"output_tokens": usage["output_tokens"]}}, event="message_delta")
            yield sse({"type": "message_stop"}, event="message_stop")

        return StreamResponse(gen())

    @r.route("POST", "/v1/embeddings")
    async def embeddings(req):
        body = req.json() or {}
        state.requests.append({"api": "embeddings", "body": body, "headers": req.headers.to_dict()})
        if body.get("model") == "embed-error":
            return _openai_error(500, "Embedding backend crashed")
        inputs = body.get("input")
        inputs = [inputs] if isinstance(inputs, str) else list(inputs or [])
        return JSONResponse({"object": "list", "model": body.get("model"),
                             "data": [{"object": "embedding", "index": i, "embedding": fake_embedding(t)} for i, t in enumerate(inputs)],
                             "usage": {"prompt_tokens": sum(len(t) // 4 + 1 for t in inputs), "total_tokens": sum(len(t) // 4 + 1 for t in inputs)}})

    # --- притворяемся OpenAI для входа через ChatGPT ------------------------------------

    @r.route("POST", "/api/accounts/deviceauth/usercode")
    async def device_code(req):
        if (req.json() or {}).get("client_id") != "app_EMoamEEZ73f0CkXaXp7hrann":
            return JSONResponse({"error": {"message": "bad client", "code": "invalid_client"}}, 400)
        state.device_polls = 0
        return JSONResponse({"device_auth_id": "deviceauth_fake", "user_code": "FAKE-CODE1", "interval": "1"})

    @r.route("POST", "/api/accounts/deviceauth/token")
    async def device_token(req):
        state.device_polls += 1
        if state.device_polls < 2:
            return JSONResponse({"error": {"message": "Device authorization is pending. Please try again.", "code": "deviceauth_authorization_pending"}}, 403)
        return JSONResponse({"authorization_code": "fake-auth-code", "code_verifier": "fake-verifier"})

    @r.route("POST", "/oauth/token")
    async def oauth_token(req):
        from urllib.parse import parse_qs

        form = {k: v[0] for k, v in parse_qs(req.body.decode()).items()}
        state.requests.append({"api": "oauth", "body": form, "headers": req.headers.to_dict()})
        if form.get("grant_type") == "authorization_code" and form.get("code"):
            return JSONResponse(_issue(state, int(state.__dict__.get("expires_in", 3600))))
        if form.get("grant_type") == "refresh_token":
            if form.get("refresh_token") in ("revoked", None, ""):
                return JSONResponse({"error": {"message": "Could not validate your token.", "code": "refresh_token_reused"}}, 401)
            state.refreshes += 1
            return JSONResponse(_issue(state))
        return JSONResponse({"error": {"message": "bad grant", "code": "invalid_grant"}}, 400)

    @r.route("POST", "/backend-api/codex/responses")
    async def responses(req):
        body = req.json() or {}
        state.requests.append({"api": "chatgpt", "body": body, "headers": req.headers.to_dict()})
        auth = req.headers.get("authorization") or ""
        if not auth.startswith("Bearer ") or auth[7:] not in state.valid_access or req.headers.get("chatgpt-account-id") != "acc_fake":
            return JSONResponse({"detail": "Could not parse your authentication token. Please try signing in again."}, 401)
        model = body.get("model") or "echo"
        messages = _responses_to_messages(body)
        res = decide(model, body, messages, body.get("tools"), state)
        if "error" in res:
            if res["error"] == 429:
                return JSONResponse({"error": {"type": "usage_limit_reached", "message": "The usage limit has been reached", "resets_in_seconds": 3600}}, 429)
            return JSONResponse({"detail": res["message"]}, res["error"])
        text = res.get("text", "")
        calls = res.get("tool_calls") or []
        rid = "resp_" + uuid.uuid4().hex[:10]

        async def gen():
            def ev(d):
                return sse(d, event=d["type"])
            yield ev({"type": "response.created", "response": {"id": rid, "model": model}})
            if res.get("reasoning"):
                yield ev({"type": "response.reasoning_summary_text.delta", "delta": res["reasoning"]})
                yield ev({"type": "response.output_item.done", "item": {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "enc-" + rid}})
            if text:
                yield ev({"type": "response.output_item.added", "item": {"type": "message", "id": "msg_1", "role": "assistant"}})
                for w in _words(text):
                    yield ev({"type": "response.output_text.delta", "item_id": "msg_1", "delta": w})
                yield ev({"type": "response.output_item.done", "item": {"type": "message", "id": "msg_1"}})
            for i, c in enumerate(calls):
                args = json.dumps(c.get("arguments", {}), ensure_ascii=False)
                item = {"type": "function_call", "id": f"fc_{i}", "call_id": f"call_{uuid.uuid4().hex[:6]}", "name": c["name"], "arguments": ""}
                yield ev({"type": "response.output_item.added", "item": item})
                yield ev({"type": "response.function_call_arguments.delta", "item_id": f"fc_{i}", "delta": args[: len(args) // 2]})
                yield ev({"type": "response.function_call_arguments.delta", "item_id": f"fc_{i}", "delta": args[len(args) // 2:]})
                yield ev({"type": "response.output_item.done", "item": dict(item, arguments=args)})
            u = _usage(messages, text)
            yield ev({"type": "response.completed", "response": {"id": rid, "status": "completed",
                                                                  "usage": {"input_tokens": u["prompt_tokens"], "output_tokens": u["completion_tokens"],
                                                                            "input_tokens_details": {"cached_tokens": 0}}}})

        return StreamResponse(gen(), headers={"X-Accel-Buffering": "no"})

    return r


async def serve(port: int, host: str = "127.0.0.1") -> HttpServer:
    server = HttpServer(create_router())
    await server.start(host, port)
    return server


def main(port: int = 5199) -> int:
    async def run():
        from .fake_mail import FakeMail

        server = await serve(port)
        print(f"Фальшивые модели слушают http://127.0.0.1:{server.port}/v1 — модели: {', '.join(MODELS)}")
        mail = FakeMail(server.handler.state.mailbox)  # тот же ящик, что у /fake/mail/*
        try:
            ip, sp = await mail.start(server.port + 1, server.port + 2)
            print(f"Фальшивая почта: IMAP 127.0.0.1:{ip}, SMTP 127.0.0.1:{sp} (без шифрования), ящик {mail.box.user} / {mail.box.password}")
        except OSError as e:
            print(f"Фальшивая почта не поднялась: {e}")
        print("Остановить: Ctrl+C")
        try:
            await asyncio.Event().wait()
        finally:
            await mail.stop()
            await server.stop()

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass
    return 0
