"""Пульт: HTTP API, поток событий (SSE) и раздача интерфейса.

Защита:
- по умолчанию слушаем только 127.0.0.1 — пароля нет;
- заголовок Host проверяется (от DNS rebinding);
- меняющие запросы требуют заголовок X-Bender и совпадающий Origin (от CSRF со сторонних сайтов);
- каждый запрос, включая localhost, требует секретный токен
  (ссылка с ?token=… ставит куку HttpOnly SameSite=Strict).
"""

from __future__ import annotations

from ..core.pathutil import within

import asyncio
import hmac
import json
import re
import time
from pathlib import Path

from .. import APP_NAME, __version__
from ..core.errors import HumanError, unexpected
from ..guard import sandbox
from ..guard.policy import CAPS, Action
from ..net.http_server import FileResponse, JSONResponse, Request, Response, Router, StreamResponse, sse
from ..router.config import PRESETS, ROLES

STATIC = Path(__file__).parent / "static"
TOKEN_COOKIE = "bender_token"
LOCAL = {"127.0.0.1", "::1", "localhost"}


def error_response(err: HumanError) -> JSONResponse:
    return JSONResponse({"error": err.to_dict()}, err.status if 400 <= err.status < 600 else 400)


def create_web(app, *, allowed_hosts: set[str], token: str | None = None):
    host_app = app
    from ..core.web_auth import token as web_token
    token = token or web_token(host_app.dirs.root, create=True)
    from ..agents.web import ContextProxy
    from ..agents.runtime import ACTIVE
    app = ContextProxy(host_app)
    r = Router()
    hosts = {h.lower() for h in allowed_hosts}

    def J(fn):
        """Обёртка: тело JSON → аргумент, HumanError → понятный ответ."""
        async def wrapped(req: Request):
            try:
                out = await fn(req)
            except HumanError as e:
                return error_response(e)
            except json.JSONDecodeError:
                return error_response(HumanError("bad_json", "Запрос повреждён", "Обнови страницу."))
            if isinstance(out, Response):
                return out
            return JSONResponse(out if out is not None else {"ok": True})
        return wrapped

    def body(req: Request) -> dict:
        b = req.json() if req.body else {}
        return b if isinstance(b, dict) else {}

    from ..agents.web import routes as agent_routes
    agent_routes(r, host_app, J, body)
    from .manners_api import routes as manner_routes
    manner_routes(r, app, J, body)
    from .mcp_oauth_api import routes as oauth_routes
    oauth_routes(r,app,J,body)

    from .matrix_api import routes as matrix_routes
    matrix_routes(r,app,J,body)
    from ..userbot.web import routes as userbot_routes
    userbot_routes(r,app,J,body)

    # --- состояние ---------------------------------------------------------------------------

    @r.route('GET','/api/doctor')
    @J
    async def doctor_read(req):
        from ..diagnostics.doctor import collect
        return await asyncio.to_thread(collect,app.dirs.root,app=app)

    @r.route('POST','/api/doctor')
    @J
    async def doctor_probe(req):
        from ..diagnostics.doctor import collect,probe_models
        repaired=None
        if body(req).get('repair_roles') is True:
            from ..diagnostics.doctor import repair_roles
            repaired=await repair_roles(app.context())
        result=await asyncio.to_thread(collect,app.dirs.root,app=app)
        if repaired:result['repair']=repaired
        if body(req).get('models') is True:result['rows']+=await probe_models(app)
        result['ok']=not any(row['level']=='error' for row in result['rows'])
        return result

    @r.route('GET','/api/report/preview')
    @J
    async def report_preview(req):
        from ..diagnostics.report import preview
        return preview()

    @r.route('POST','/api/report')
    @J
    async def report_download(req):
        from ..diagnostics.report import build
        if body(req).get('reviewed') is not True:
            raise HumanError('report_preview','Сначала просмотри состав отчёта','Нажми «Подготовить отчёт», затем кнопку скачивания.')
        raw,_=await asyncio.to_thread(build,app.dirs.root)
        return Response(raw,headers={'Content-Disposition':'attachment; filename="bender-report.zip"'},content_type='application/zip')

    @r.route("GET", "/api/state")
    @J
    async def state(req):
        from ..memory.avatar import read as read_avatar

        g = app.guard.policy.get_settings()
        sb = sandbox.for_app(app)
        return {
            "agent_id": app.agent_id, "agents": host_app.agents.public(),
            "avatar": read_avatar(app.dirs.self_dir), "appearance": app.settings.group("appearance"), "sleeping": app.sleep.running, "voice": app.settings.group("voice"), "inspectors": app.settings.group("inspectors"),
            "app": APP_NAME, "version": __version__, "identity": app.selfdir.identity(), "born": app.selfdir.born(), "relocating": app.selfdir.relocating(),
            "halted": app.guard.halted, "main_session": app.main_session(), "has_model": bool(app.models.roles().get("main")),
            "guard": {"enabled": g.get("enabled"), "profile": g.get("profile"), "hardline": g.get("hardline"), "sandbox": sb},
            "cost_total": round(host_app.agents.total_cost(),6), "cost_today": round(app.cost_today(), 6), "safe": app.safe, "mood": app.body.mood(),
            "pending": [p["card"] for p in app.guard.pending.values()] + [{"kind": "question", **q["card"]} for q in app.questions.values()],
            "running": app.mind.running(app.main_session()),
            "boot": _boot_info(app),
            "model_notice": app.store.meta_get('models:removed_notice'),
            'update_available': bool(app.store.meta_get('updates:available')),
            'import_rights_offer': app.agents.rights.effective(app)['profile']=='helper' and bool(app.store.meta_get('import:openclaw:last')) and not app.store.meta_get('import:rights_reviewed'),
            'lifecycle':host_app.lifecycle.state(),
        }

    @r.route("POST", "/api/restart")
    @J
    async def restart(req):
        """Перезапуск ядра через Сторожа. mode: same | normal (выйти из безопасного) | safe."""
        from ..tools.registry import ToolContext
        b=body(req)
        return await host_app.lifecycle.request(ToolContext(app.context(),actor='owner'),'restart',mode=str(b.get('mode') or 'same'),policy=b.get('policy','interrupt'))

    @r.route('GET','/api/lifecycle')
    @J
    async def lifecycle_state(req):return host_app.lifecycle.state()

    @r.route('POST','/api/control/{action}')
    @J
    async def lifecycle_action(req):
        from ..tools.registry import ToolContext
        b=body(req)
        if b.get('confirmed') is not True:
            raise HumanError('control_confirm','Подтверди действие в пульте','Выбери дождаться или прервать, затем нажми подтверждение.',status=409)
        return await host_app.lifecycle.request(ToolContext(app.context(),actor='owner'),req.params['action'],policy=b.get('policy','wait'))

    @r.route('GET','/api/service')
    @J
    async def service_status(req):
        from ..core.service import status
        return await asyncio.to_thread(status,host_app.dirs.root)

    @r.route('POST','/api/service/install')
    @J
    async def service_install(req):
        from ..core.service import change
        if body(req).get('confirmed') is not True:raise HumanError('service_confirm','Подтверди включение автозапуска','Просмотри режим запуска и нажми кнопку подтверждения.',status=409)
        return await change(host_app,'install')

    @r.route("GET", "/api/events")
    async def events(req):
        ctx = app.context()
        q = ctx.bus.subscribe()

        async def gen():
            try:
                yield b": bender\n\n"
                while True:
                    try:
                        ev = await asyncio.wait_for(q.get(), 15)
                        if ev.get("agent_id", "main") in (ctx.agent_id, "*"): yield sse(ev)
                    except asyncio.TimeoutError:
                        yield b": ping\n\n"
            finally:
                ctx.bus.unsubscribe(q)

        return StreamResponse(gen(), headers={"X-Accel-Buffering": "no"})

    # --- разговор ---------------------------------------------------------------------------

    @r.route("GET", "/api/session/{sid}/messages")
    @J
    async def messages(req):
        sid = req.params["sid"]
        limit = int(req.q("limit") or 300)
        return {"messages": app.store.messages(sid, limit=limit), "running": app.mind.running(sid),'session':app.store.session(sid)}

    @r.route('POST','/api/attachments')
    @J
    async def attachment_upload(req):
        import base64
        from ..channels import media
        b=body(req)
        try:raw=base64.b64decode(b.get('data',''),validate=True)
        except (ValueError,TypeError):raise HumanError('attachment_data','Не удалось прочитать вложение','Прикрепи файл ещё раз.')
        name=str(b.get('name') or 'файл')
        kind='photo' if str(b.get('mime','')).startswith('image/') else 'document'
        return await media.save(app.context(),name,raw,kind=kind,session=b.get('session'))

    @r.route('GET','/api/attachments/{key}')
    async def attachment_download(req):
        from urllib.parse import quote
        from ..channels import media
        from ..tools.registry import ToolContext
        item=media.get(app.context(),req.params['key'])
        await ToolContext(app.context(),actor='owner').check('fs.read',item['path'],'скачать вложение своего агента')
        with Path(item['path']).open('rb') as handle:raw=handle.read(media.UPLOAD_LIMIT+1)
        if len(raw)>media.UPLOAD_LIMIT:raise HumanError('attachment_size','Файл стал больше 50 МБ','Раздели его перед скачиванием.')
        return Response(raw,content_type='application/octet-stream',headers={'Content-Disposition':"attachment; filename*=UTF-8''"+quote(item['name'],safe=''),'X-Content-Type-Options':'nosniff'})

    @r.route("POST", "/api/say")
    @J
    async def say(req):
        b = body(req)
        text = str(b.get("text") or "").strip()
        keys=b.get('attachments') or []
        if not isinstance(keys,list) or len(keys)>10 or any(not isinstance(key,str) for key in keys):raise HumanError('attachment_count','Не больше 10 вложений','Отправь остальные следующим сообщением.')
        if not text and not keys:
            raise HumanError("empty", "Пустое сообщение", "")
        sid = b.get("session") or app.main_session()
        quoted=False;reply_to=b.get('reply_to')
        if reply_to:
            if not isinstance(reply_to,str) or len(reply_to)>100:raise HumanError('quote','Неверный номер цитаты','Выбери сообщение этого разговора.')
            row=app.store.one('SELECT data,meta FROM messages WHERE session_id=? AND id=?',(sid,reply_to))
            if not row:raise HumanError('quote','Сообщение цитаты не найдено','Можно ответить только на сообщение этого разговора.',status=404)
            original=json.loads(row['data']);metadata=json.loads(row['meta'])
            if original.get('role') not in ('user','assistant') or metadata.get('service_event') or metadata.get('tool_media'):
                raise HumanError('quote','Это служебное сообщение','Выбери реплику хозяина или агента.')
            from ..mind.quotes import append
            quoted=bool(metadata.get('untrusted') or metadata.get('tainted'))
            text=append(text,original.get('content'),channel='пульта',reference=reply_to,trusted=not quoted)
        from ..channels import media
        items=[media.get(app.context(),key) for key in keys]
        if sum(i['size'] for i in items)>media.DOWNLOAD_LIMIT:raise HumanError('attachment_size','Вложения больше 20 МБ суммарно','Отправь их отдельно.')
        parts=media.content(text or 'Прислано вложение.',items,vision=app.settings.get('attachments','vision',True),audio=app.settings.get('voice','enabled',False) and app.settings.get('voice','stt_mode') in ('native','both')) if items else None
        await app.mind.say(sid, text or 'Прислано вложение.', content=parts, channel="pult", wait=False,tainted=bool(items) or quoted,message_meta={'attachments':items,**({'reply_to':reply_to} if reply_to else {})})
        return {"ok": True, "session": sid}

    @r.route("POST", "/api/stop")
    @J
    async def stop(req):
        return {"stopped": app.mind.stop(body(req).get("session"))}

    @r.route("POST", "/api/halt")
    @J
    async def halt(req):
        app.halt()
        return {"halted": True}

    @r.route("POST", "/api/resume")
    @J
    async def resume(req):
        app.guard.resume()
        return {"halted": False}

    @r.route("POST", "/api/approvals/{pid}")
    @J
    async def approve(req):
        ok = app.guard.respond(req.params["pid"], str(body(req).get("choice") or "deny"))
        if not ok:
            raise HumanError("gone", "Эта карточка уже закрыта", "Возможно, истекло время ожидания.", status=410)
        return {"ok": True}

    @r.route("POST", "/api/questions/{qid}")
    @J
    async def answer(req):
        if not app.answer(req.params["qid"], str(body(req).get("answer") or "")):
            raise HumanError("gone", "Вопрос уже закрыт", "", status=410)
        return {"ok": True}

    @r.route("GET", "/api/calls/{cid}")
    @J
    async def call(req):
        c = app.store.call(req.params["cid"])
        if not c:
            raise HumanError("not_found", "Нет такого вызова", "", status=404)
        return c

    @r.route("GET", "/api/session/{sid}/calls")
    @J
    async def calls(req):
        rows = app.store.q("SELECT id, created_at, role, provider, model, usage, cost, seconds, notice, error FROM calls WHERE session_id = ? ORDER BY id DESC LIMIT 100", (req.params["sid"],))
        return {"calls": [dict(x) for x in rows]}

    # --- модели ---------------------------------------------------------------------------

    @r.route("GET", "/api/models")
    @J
    async def models(req):
        from ..router.effort import LEVELS,MODES,snapshot
        from ..router.roles import broken
        from ..router.capabilities import description,KEYS
        from ..router.probes import audio_available
        providers = app.models.public_providers()
        configured=app.models.roles();raw=app.models.providers()
        for p in providers:
            ids={entry['model'] for entries in configured.values() for entry in entries if isinstance(entry,dict) and entry.get('provider')==p['id'] and isinstance(entry.get('model'),str)}
            p['media_capabilities']={model:{key:description(raw[p['id']],model,key) for key in KEYS} for model in ids}
        return {"providers": providers, "roles": app.models.roles(), "role_titles": {k: {"title": v[0], "hint": v[1]} for k, v in ROLES.items()},
                'broken_roles':broken(app.settings.group('roles'),app.models.providers()),'role_notice':app.store.meta_get('models:role_repair_notice'),
                'local_voice_installed':(app.host.dirs.local/'voice'/'installed.json').is_file(),
                'audio_probe_available':audio_available(app.context()),
                'reasoning':snapshot(app),'effort_levels':LEVELS,'effort_modes':MODES,
                'agents_count':sum(1 for a in host_app.agents.public() if not a['archived']),
                "presets": PRESETS, "health": app.router.health.snapshot(), "fuel": app.fuel.snapshot()}

    @r.route('GET','/api/models/effort')
    @J
    async def model_effort(req):
        from ..router.effort import configured,describe,validate
        provider=app.models.providers().get(req.q('provider'))
        model=req.q('model') or ''
        if not provider or not model or len(model)>500:
            raise HumanError('effort_model','Нужны подключённый провайдер и модель','Выбери модель в роли.')
        params={'effort_mode':req.q('mode') or 'auto'}
        if req.q('effort'):params['effort']=req.q('effort')
        validate(params)
        return describe(provider,model,configured(params,app.settings.group('reasoning')))

    @r.route('POST','/api/providers/{pid}/capability')
    @J
    async def model_capability(req):
        from ..tools.registry import ToolContext
        from ..router.capabilities import description,KEYS
        b=body(req)
        await ToolContext(app,actor='owner').check('settings','providers','изменить признак зрения/слуха модели')
        p=app.models.set_capability(req.params['pid'],b.get('model'),b.get('feature'),b.get('value'))
        app.journal.write('models','Хозяин изменил возможность модели',actor='owner',data={'provider':p['id'],'model':b['model'],'feature':b['feature'],'value':b.get('value')})
        return {'capabilities':{key:description(p,b['model'],key) for key in KEYS}}

    @r.route('POST','/api/model/probe')
    @J
    async def model_probe(req):
        from ..router.probes import run
        from ..tools.registry import ToolContext
        b=body(req)
        return await run(ToolContext(app.context(),actor='owner'),b.get('provider'),b.get('model'),b.get('feature'))

    @r.route("POST", "/api/fuel/refresh")
    @J
    async def fuel_refresh(req):
        return await app.fuel.refresh()

    @r.route("POST", "/api/providers")
    @J
    async def add_provider(req):
        b = body(req)
        extra = {}
        if b.get("preset") == "claude_code":
            if b.get("command"):
                extra["command"] = str(b["command"]).strip()
            if b.get("billing") in ("api", "subscription"):
                extra["billing"] = b["billing"]
                extra["subscription"] = b["billing"] == "subscription"
        if b.get("preset") == "antigravity":
            extra["risk_accepted"] = b.get("risk_accepted") is True
            if b.get("command"):
                extra["command"] = str(b["command"]).strip()
        p = app.models.add_provider(preset=b.get("preset") or None, name=b.get("name") or "", kind=b.get("kind") or "openai",
                                    base_url=b.get("base_url") or "", api_key=b.get("api_key") or None, extra=extra or None)
        app.journal.write("models", f"Подключён провайдер {p['name']}", actor="owner", data={"id": p["id"], "base_url": p["base_url"]})
        public = {k: v for k, v in p.items() if k != "keys"}
        return {"provider": public}

    @r.route('POST', '/api/models/notice/ack')
    @J
    async def model_notice_ack(req):
        app.store.meta_set('models:removed_notice', None)
        return {'ok': True}

    @r.route("POST", "/api/cli/{kind}/check")
    @J
    async def cli_check(req):
        import shlex

        from ..router import codex_probe
        from ..router.cli_common import command
        from ..tools.registry import ToolContext

        kind = req.params["kind"]
        if kind != "codex":
            raise HumanError("cli_kind", "Неизвестный CLI", "Доступна проверка codex; движок остаётся выключенным.")
        provider = {"command": body(req).get("command") or kind}
        await ToolContext(app, actor="owner", session_id="cli-check").check("exec", shlex.join(command(provider)), "проверить официальный CLI: help и version")
        return await codex_probe.check(provider)

    @r.route("GET", "/api/claude-code/check")
    @J
    async def claude_code_check(req):
        from ..router import cli

        cmd = (req.q("command") or "").strip()
        return await cli.check({"command": cmd} if cmd else {})

    @r.route("PATCH", "/api/providers/{pid}")
    @J
    async def patch_provider(req):
        p = app.models.update_provider(req.params["pid"], body(req))
        return {"provider": {k: v for k, v in p.items() if k != "keys"}}

    @r.route("DELETE", "/api/providers/{pid}")
    @J
    async def del_provider(req):
        app.models.remove_provider(req.params["pid"])
        app.journal.write("models", "Провайдер удалён", actor="owner", data={"id": req.params["pid"]})
        return {"ok": True}

    @r.route("POST", "/api/providers/{pid}/catalog")
    @J
    async def catalog(req):
        from ..router.catalog import list_models

        p = app.models.providers().get(req.params["pid"])
        if not p:
            raise HumanError("no_provider", "Нет такого провайдера", "", status=404)
        keys = [app.secrets.get(n) for n in p.get("keys") or []]
        ms = await list_models(app.http, p, keys[0] if keys else None)
        app.models.apply_catalog(p['id'],ms)
        return {"models": ms}

    @r.route('POST','/api/roles/repair')
    @J
    async def roles_repair(req):
        from ..tools.registry import ToolContext
        await ToolContext(app,actor='owner').check('settings','roles','убрать повреждённые ссылки в ролях этого агента')
        diff=app.settings.set('roles',{},by='owner')
        return {'roles':app.models.roles(),'notice':app.store.meta_get('models:role_repair_notice'),'changed':bool(diff)}

    @r.route("POST", "/api/roles/{role}")
    @J
    async def set_role(req):
        app.models.set_role(req.params["role"], body(req).get("entries") or [])
        app.journal.write("models", f"Роль «{ROLES.get(req.params['role'], (req.params['role'],))[0]}» изменена", actor="owner", data=body(req))
        return {"roles": app.models.roles()}

    @r.route("POST", "/api/roles/{role}/all")
    @J
    async def set_role_all(req):
        """Та же цепочка роли — всем агентам стаи (не в архиве); потом каждого можно поправить отдельно."""
        role = req.params["role"]
        if role not in ROLES:
            raise HumanError("bad_role", "Нет такой роли", "", status=404)
        entries = body(req).get("entries") or []
        done, skipped = [], []
        for a in host_app.agents.public():
            if a["archived"]:
                continue
            try:
                ctx = host_app.agents.get(a["id"])
            except HumanError as e:
                skipped.append({"id": a["id"], "name": a["name"], "reason": e.title})
                continue
            ctx.models.set_role(role, entries)
            done.append(a["id"])
        app.journal.write("models", f"Роль «{ROLES[role][0]}» применена ко всем агентам", actor="owner", data={"role": role, "agents": done, "entries": entries})
        return {"agents": done, "skipped": skipped, "roles": app.models.roles()}

    @r.route("POST", "/api/providers/{pid}/forget-model")
    @J
    async def forget_model(req):
        app.models.forget_model(req.params["pid"], str(body(req).get("model") or ""))
        return {"ok": True}

    @r.route("POST", "/api/try/model")
    @J
    async def try_model(req):
        b = body(req)
        target = (b["provider"], b["model"]) if b.get("provider") and b.get("model") else None
        res = await app.router.call([{"role": "user", "content": str(b.get("text") or "Привет! Кто ты, одной фразой?")}], role=b.get("role") or "main", target=target)
        return {"text": res.text, "reasoning": res.reasoning, "provider": res.provider_name, "model": res.model, "usage": res.usage.to_dict(),
                "cost": res.cost, "seconds": round(res.seconds, 3), "notice": res.notice, "finish": res.finish}

    # --- настройки и права -------------------------------------------------------------------

    @r.route('POST', '/api/try/model-tools')
    @J
    async def try_model_tools(req):
        from ..router.types import ToolSpec
        from ..router.errors import ProviderError
        from ..mind.passport import clean
        from ..tools.registry import ToolContext

        b = body(req)
        p = app.models.providers().get(b.get('provider'))
        if not p or not b.get('model'):
            raise HumanError('no_provider', 'Выбери провайдера и модель', 'Сохрани модель в цепочке и повтори проверку.')
        await ToolContext(app, actor='owner').check('net.read', p.get('base_url') or p['name'], 'проверить модель с одним тулом без личных данных')
        spec = ToolSpec('check_connection', 'Проверка подключения; не меняет данные.', {'type': 'object', 'properties': {'ok': {'type': 'boolean'}}, 'required': ['ok']})
        try:
            res = await app.router.call([{'role': 'system', 'content': 'Проверка Бендера. Не выполняй других действий.'},
                                         {'role': 'user', 'content': 'Вызови check_connection с ok=true.'}],
                                        target=(p['id'], b['model']), tools=[spec], params={**(b.get('params') or {}), 'max_tokens': 64})
            text_mode = b.get('params', {}).get('tool_protocol') == 'text' or (p['id'], b['model']) in app.router.text_tools
            app.store.add_call(session_id=None, role='probe', provider=res.provider_name, model=res.model, usage=res.usage.to_dict(), cost=res.cost, seconds=res.seconds, notice=res.notice, request=None)
            return {'state': 'без тулов через API' if text_mode else 'работает с тулами', 'notice': res.notice, 'cost': res.cost}
        except ProviderError as e:
            return clean(app, {'state': 'не принимает', 'http': e.http_status, 'body': (e.raw or e.message)[:2048], 'error': e.to_dict()})

    @r.route("GET", "/api/settings")
    @J
    async def settings(req):
        return {"groups": app.settings.describe()}

    @r.route("GET", "/api/find")
    @J
    async def find_index(req):
        from .finder import index
        return {"items": index(app.settings.describe(), STATIC)}

    @r.route("POST", "/api/settings/{group}")
    @J
    async def set_settings(req):
        diff = app.settings.set(req.params["group"], body(req), by="owner")
        if diff:
            app.journal.write("settings", f"Настройки «{req.params['group']}»: " + ", ".join(f"{k}: {a} → {b}" for k, (a, b) in diff.items()), actor="owner",
                              data={"group": req.params["group"], "diff": {k: list(v) for k, v in diff.items()}})
        return {"diff": {k: list(v) for k, v in diff.items()}, "group": app.settings.group(req.params["group"])}

    @r.route("GET", "/api/guard")
    @J
    async def guard_info(req):
        rights=app.agents.rights.effective(app)
        return {"settings": app.settings.group("guard"), 'effective':app.guard.policy.get_settings(), 'rights':rights,
                'profile_source':app.agents.rights.guard_source(app),
                "rules": app.guard.rules(), "caps": CAPS, "sandbox": sandbox.for_app(app),
                "pending": [p["card"] for p in app.guard.pending.values()], "halted": app.guard.halted,
                "checkpoints": app.guard.checkpoints.list(30)}

    @r.route("POST", "/api/guard/rules")
    @J
    async def add_rule(req):
        b = body(req)
        return {"rule": app.guard.add_rule(b.get("capability", ""), b.get("target", "*"), b.get("verdict", "allow"), b.get("note", ""))}

    @r.route("DELETE", "/api/guard/rules/{rid}")
    @J
    async def del_rule(req):
        app.guard.remove_rule(req.params["rid"])
        return {"ok": True}

    @r.route("POST", "/api/guard/check")
    @J
    async def guard_check(req):
        b = body(req)
        a = Action(capability=b.get("capability", ""), target=str(b.get("target", "")), tainted=bool(b.get("tainted")), unattended=bool(b.get("unattended")),
                   to_owner=bool(b.get("to_owner")))
        if a.capability.startswith("fs."):
            a.target = str(app.guard.policy.resolve(a.target))
        d = app.guard.policy.decide(a)
        return {"verdict": d.verdict, "reason": d.reason, "rule": d.rule, "target": a.target}

    @r.route("POST", "/api/checkpoints/{cid}/restore")
    @J
    async def restore(req):
        e = app.guard.checkpoints.restore(req.params["cid"])
        app.journal.write("file", f"Откат файла {e['path']} из снимка", actor="owner", data=e)
        return {"restored": e}

    # --- бортжурнал, тело, тулы, личность ------------------------------------------------------

    @r.route("GET", "/api/journal")
    @J
    async def journal(req):
        before = int(req.q("before")) if req.q("before") else None
        return {"entries": app.journal.recent(int(req.q("limit") or 200), before=before, kind=req.q("kind") or None, **{k:req.q(k) or "" for k in ("actor","capability","verdict","channel","session")})}

    @r.route("GET", "/api/journal/export")
    @J
    async def journal_export(req):
        if not app.settings.get("inspectors","enabled",True):
            raise HumanError("inspector_off","Инспектор выключен","Включи его в настройках.")
        rows=app.journal.recent(5000,kind=req.q("kind") or None,**{k:req.q(k) or "" for k in ("actor","capability","verdict","channel","session")})
        return Response(json.dumps({"entries":rows,"limit":5000},ensure_ascii=False,indent=2).encode(),headers={"Content-Type":"application/json; charset=utf-8","Content-Disposition":"attachment; filename=journal.json"})

    @r.route("GET", "/api/inspectors/memory/{ident}")
    @J
    async def inspector_memory(req):
        return app.inspector.detail(req.params["ident"])

    @r.route("POST", "/api/inspectors/memory")
    @J
    async def inspector_change(req):
        data=body(req)
        return await app.inspector.change(data.get("ids",[]),data.get("action"),data.get("text",""),data.get("reason",""),data.get("hash"))

    @r.route("GET", "/api/journal/verify")
    @J
    async def journal_verify(req):
        return app.journal.verify()

    @r.route("GET", "/api/status")
    @J
    async def status(req):
        from ..body import sensors

        s = sensors.snapshot(app.dirs.root)
        return {"sensors": s, "text": sensors.describe(s), "cost_today": app.cost_today()}

    @r.route("GET", "/api/tools")
    @J
    async def tools(req):
        return {"tools": app.tools.describe()}

    @r.route("POST", "/api/tools/{name}")
    @J
    async def toggle_tool(req):
        on = bool(body(req).get("enabled"))
        app.settings.set("tools", {req.params["name"]: "on" if on else "off"}, by="owner")
        return {"tools": app.tools.describe()}

    @r.route("POST", "/api/try/tool/{name}")
    @J
    async def try_tool(req):
        from ..tools.registry import ToolContext

        res = await app.tools.run(req.params["name"], body(req), ToolContext(app=app, session_id="polygon", actor="agent"))
        return {"ok": res.ok, "content": res.content, "summary": res.summary, "tainted": res.tainted}

    @r.route("GET", "/api/prompt")
    @J
    async def prompt(req):
        from ..mind.context import estimate_tokens, system_prompt

        text = system_prompt(app)
        return {"text": text, "tokens": estimate_tokens(text)}

    SELF_FILES = {"soul": "SOUL.md", "identity": "IDENTITY.md", "agents": "AGENTS.md", "user": "USER.md", "memory": "MEMORY.md", "heartbeat": "HEARTBEAT.md", "dreams": "DREAMS.md"}

    @r.route("GET", "/api/self")
    @J
    async def self_files(req):
        return {"files": {k: app.selfdir.read(v) for k, v in SELF_FILES.items()}, "identity": app.selfdir.identity(), "born": app.selfdir.born(),
                "relocating": app.selfdir.relocating(), "last_import": app.store.meta_get("import:openclaw:last")}

    @r.route("PUT", "/api/self/{name}")
    @J
    async def put_self(req):
        name = SELF_FILES.get(req.params["name"])
        if not name:
            raise HumanError("not_found", "Нет такого файла личности", "", status=404)
        app.selfdir.write(name, str(body(req).get("text") or ""))
        app.journal.write("self", f"Хозяин поправил {name}", actor="owner")
        return {"ok": True}

    @r.route("GET", "/api/secrets")
    @J
    async def secrets(req):
        return {"secrets": app.secrets.public()}

    @r.route("POST", "/api/secrets")
    @J
    async def add_secret(req):
        b = body(req)
        name = str(b.get("name") or "").strip()
        if not name or not b.get("value"):
            raise HumanError("bad_secret", "Нужны имя и значение", "")
        hosts = [h.strip() for h in str(b.get("hosts") or "").split(",") if h.strip()]
        tools = [t.strip() for t in str(b.get("tools") or "").split(",") if t.strip()]
        app.secrets.set(name, str(b["value"]), note=str(b.get("note") or ""), allow={"hosts": hosts, "tools": tools})
        app.journal.write("secrets", f"Секрет «{name}» сохранён", actor="owner", data={"hosts": hosts, "tools": tools})
        return {"secrets": app.secrets.public()}

    @r.route("DELETE", "/api/secrets/{name}")
    @J
    async def del_secret(req):
        app.secrets.delete(req.params["name"])
        app.journal.write("secrets", f"Секрет «{req.params['name']}» удалён", actor="owner")
        return {"ok": True}

    # --- память -------------------------------------------------------------------------------

    @r.route("GET", "/api/memory/search")
    @J
    async def memory_search(req):
        q = (req.q("q") or "").strip()
        if not q:
            return {"hits": []}
        kinds = tuple(k for k in (req.q("kinds") or "").split(",") if k) or None
        qvec = await app.vectors.embed_query(q)
        hits = await asyncio.to_thread(app.index.search, q, limit=int(req.q("limit") or 20), kinds=kinds, qvec=qvec)
        return {"hits": [h.public() for h in hits]}

    @r.route("GET", "/api/memory/stats")
    @J
    async def memory_stats(req):
        await asyncio.to_thread(app.index.sync)
        return {**app.index.stats(), "vectors": app.vectors.status()}

    @r.route("POST", "/api/memory/reindex")
    @J
    async def memory_reindex(req):
        n = await asyncio.to_thread(app.index.rebuild)
        return {"added": n, **app.index.stats()}

    def diary_file(rel: str) -> Path:
        root = app.dirs.diary.resolve()
        p = (root / rel).resolve()
        if p.suffix != ".md" or not within(p, root) or not p.is_file():
            raise HumanError("not_found", "Нет такой записи дневника", "", status=404)
        return p

    @r.route("GET", "/api/memory/overview")
    @J
    async def memory_overview(req):
        from ..mind import compact, review
        from ..mind import context as ctxmod

        await asyncio.to_thread(app.index.sync)
        core = {}
        for key, name, limit_key in (("memory", "MEMORY.md", "core_limit"), ("user", "USER.md", "user_limit")):
            text = app.selfdir.read(name)
            core[key] = {"file": name, "chars": len(text), "limit": int(app.settings.get("memory", limit_key)),
                         "entries": len(review.entries(text)), "template": app.selfdir.is_template(name)}
        days = []
        if app.dirs.diary.is_dir():
            for p in sorted(app.dirs.diary.rglob("*.md"), reverse=True):
                st = p.stat()
                days.append({"path": p.relative_to(app.dirs.diary).as_posix(), "size": st.st_size, "mtime": st.st_mtime})
        sid = app.main_session()
        budget = ctxmod.history_budget(app, ctxmod.estimate_tokens(ctxmod.system_prompt(app)))
        win = compact.window(app, sid, budget)
        cst = compact.state(app, sid)
        rst = ((app.store.session(sid) or {}).get("meta") or {}).get("review") or {}
        return {"core": core, "index": app.index.stats(), "vectors": app.vectors.status(), "facts": app.facts.count(), "diary": days[:400],
                "diary_total": len(days),
                "compact": {"enabled": compact.enabled(app), "budget": budget, "tail_tokens": win.tail_tokens, "in_request": win.tokens,
                            "dropped": win.dropped, "trigger": int(budget * compact.TRIGGER), "summary": cst.get("summary", ""), "count": cst.get("count", 0),
                            "parts": cst.get("parts", 0), "ts": cst.get("ts"), "error": cst.get("error")},
                "review": {"enabled": bool(app.settings.get("memory", "review", True)), "every": review.every(app), "pending": len(rst.get("pending") or []),
                           "ts": rst.get("ts")},
                "settings": app.settings.group("memory")}

    @r.route("GET", "/api/memory/diary")
    @J
    async def memory_diary(req):
        p = diary_file(req.q("path") or "")
        return {"path": p.relative_to(app.dirs.diary.resolve()).as_posix(), "text": p.read_text(encoding="utf-8", errors="replace")}

    @r.route("GET", "/api/memory/facts")
    @J
    async def memory_facts(req):
        rows = app.store.q("SELECT * FROM facts" + ("" if req.q("all") else " WHERE valid_to IS NULL") + " ORDER BY subject, relation, valid_from DESC LIMIT 2000")
        return {"facts": [{k: f[k] for k in f.keys()} for f in rows], **app.facts.count()}

    @r.route("POST", "/api/memory/facts/{fid}/close")
    @J
    async def memory_fact_close(req):
        f = app.store.one("SELECT * FROM facts WHERE id = ?", (int(req.params["fid"]),))
        if not f:
            raise HumanError("not_found", "Нет такого факта", "", status=404)
        n = app.facts.close(f["subject"], f["relation"], f["object"])
        app.journal.write("memory", f"Хозяин закрыл факт: {f['subject']} — {f['relation']} — {f['object']}", actor="owner")
        return {"closed": n}

    @r.route("POST", "/api/memory/vectors")
    @J
    async def memory_vectors(req):
        if not app.vectors.available():
            raise HumanError("no_embed", "Поиск по смыслу выключен", "Нужна роль «Эмбеддинги» в «Моделях» (и включённый «Поиск по смыслу» в настройках памяти).")
        n = await app.vectors.fill(max_items=5000)
        return {"done": n, **app.vectors.status()}

    @r.route("POST", "/api/memory/compact")
    @J
    async def memory_compact(req):
        from ..mind import compact

        b = body(req)
        sid = str(b.get("session") or app.main_session())
        session=app.store.session(sid)
        if not session:raise HumanError('session','Такого разговора у агента нет','Выбери свой разговор.',status=404)
        if session['kind']=='correspondence':raise HumanError('correspondence_readonly','Переписка агентов — только для чтения','Открой обычный чат.',status=403)
        if b.get("action") == "page":
            if app.mind.running(sid):
                raise HumanError("busy", "Агент сейчас отвечает", "Начни новую страницу, когда ход закончится.")
            if not compact.new_page(app, sid, "хозяин попросил"):
                raise HumanError("page", "Страница и так чистая", "После прошлой границы ещё не было сообщений.")
            return {"ok": True}
        if b.get("action") == "reset":
            compact.reset(app, sid)
            app.journal.write("memory", "Хозяин сбросил пересказ разговора", actor="owner", data={"session": sid})
            return {"ok": True}
        return await compact.manual(app.context(),sid,str(b.get("instructions") or ""))

    # --- сердце: задачи по времени ------------------------------------------------------------

    def job_or_404(jid: str):
        j = app.heart.jobs.get(jid)
        if not j:
            raise HumanError("not_found", "Нет такой задачи", "", status=404)
        return j

    @r.route("GET", "/api/jobs")
    @J
    async def jobs_list(req):
        return {"jobs": [j.public() for j in app.heart.jobs.all()], "enabled": app.heart.enabled(), "running": bool(app.heart.task and not app.heart.task.done()),
                "firing": app.heart.firing, "settings": app.settings.group("heart")}

    @r.route("POST", "/api/jobs")
    @J
    async def jobs_add(req):
        b = body(req)
        j = app.heart.add(str(b.get("name") or ""), kind=str(b.get("kind") or "remind"), text=str(b.get("text") or ""), prompt=str(b.get("prompt") or ""),
                          at=b.get("at") or None, every=b.get("every") or None, cron=b.get("cron") or None, session=str(b.get("session") or "main"),
                          window=str(b.get("window") or ""), idle_only=bool(b.get("idle_only")), authority=b.get("authority") or None,
                          created_by="owner", check=b.get("check") or None, deliver=str(b.get("deliver") or "chat"))
        return j.public()

    @r.route("PATCH", "/api/jobs/{jid}")
    @J
    async def jobs_patch(req):
        from ..heart.jobs import make_schedule

        j = job_or_404(req.params["jid"])
        b = body(req)
        if "enabled" in b and len(b) == 1:
            return app.heart.set_enabled(j.id, bool(b["enabled"])).public()
        fields: dict = {}
        if "name" in b:
            fields["name"] = str(b["name"]).strip()[:120] or j.name
        if any(k in b for k in ("at", "every", "cron")):
            fields["schedule"] = make_schedule(at=b.get("at") or None, every=b.get("every") or None, cron=b.get("cron") or None)
        if "window" in b:
            fields["window"] = str(b["window"] or "")
        if "idle_only" in b:
            fields["idle_only"] = bool(b["idle_only"])
        if "authority" in b:
            fields["authority"] = list(b["authority"] or [])
        if "enabled" in b:
            fields["enabled"] = bool(b["enabled"])
        action = dict(j.action)
        for k in ("text", "prompt", "session", "deliver", "check"):
            if k in b:
                action[k] = b[k]
        fields["action"] = action
        app.heart._quiet.pop(j.id, None)
        j = app.heart.jobs.update(j.id, **fields)
        app.journal.write("heart", f"Хозяин поправил задачу «{j.name}»", actor="owner", data={"job": j.id, "fields": sorted(fields)})
        app.bus.publish({"type": "job", "job": j.public(), "status": "updated"})
        app.heart.poke()
        return j.public()

    @r.route("DELETE", "/api/jobs/{jid}")
    @J
    async def jobs_delete(req):
        j = job_or_404(req.params["jid"])
        return {"ok": app.heart.remove(j.id)}

    @r.route("POST", "/api/jobs/{jid}/run")
    @J
    async def jobs_run(req):
        j = job_or_404(req.params["jid"])
        if j.kind == "agent" and j.action.get("session", "main") == "main" and app.mind.running(app.main_session()):
            raise HumanError("busy", "Агент сейчас отвечает", "Запусти, когда ход закончится.")
        return await app.heart.fire(j, manual=True)

    @r.route("GET", "/api/jobs/{jid}/runs")
    @J
    async def jobs_runs(req):
        j = job_or_404(req.params["jid"])
        return {"runs": app.heart.jobs.history(j.id, int(req.q("limit") or 20))}

    @r.route("GET", "/api/intents")
    @J
    async def intents_list(req):
        return {"intents": app.heart.intents.all()}

    @r.route("POST", "/api/intents")
    @J
    async def intents_add(req):
        b = body(req)
        return app.heart.intents.add(str(b.get("text") or ""), b.get("keywords") or "", days=float(b.get("days") or 90), budget=int(b.get("times") or 3),
                                     created_by="owner")

    @r.route("DELETE", "/api/intents/{iid}")
    @J
    async def intents_cancel(req):
        return {"ok": app.heart.intents.cancel(req.params["iid"])}

    @r.route("GET", "/api/body")
    @J
    async def body_state(req):
        return await asyncio.to_thread(app.body.snapshot, full=True)

    # --- связь ---------------------------------------------------------------------------

    @r.route("GET", "/api/channels")
    @J
    async def channels(req):
        return {"telegram": app.telegram.public(), "mail": app.mail.public()}

    @r.route("POST", "/api/channels/telegram/token")
    @J
    async def tg_token(req):
        from ..channels.telegram import TOKEN_SECRET

        token = str(body(req).get("token") or "").strip()
        if not re.fullmatch(r"\d{5,}:[\w-]{20,}", token) and not token.endswith(":FAKE"):
            raise HumanError("tg_token", "Это не похоже на токен бота", "Токен выглядит как 123456789:AAE… — его выдаёт @BotFather командой /newbot или /token.")
        old = app.secrets.get(TOKEN_SECRET)
        app.secrets.set(TOKEN_SECRET, token, note="бот Telegram", allow={"hosts": ["api.telegram.org"], "tools": []})
        try:
            me = await app.telegram.api("getMe")
        except HumanError:
            if old:
                app.secrets.set(TOKEN_SECRET, old, note="бот Telegram")
            else:
                app.secrets.delete(TOKEN_SECRET)
            raise
        app.journal.write("channel", f"Telegram: подключён бот @{(me or {}).get('username')}", actor="owner")
        app.settings.set("telegram", {"enabled": True}, by="owner")
        await app.telegram.restart()
        return {**app.telegram.public(), "code": app.telegram.new_code() if not app.telegram.owner() else None}

    @r.route('GET','/api/channels/telegram/topics')
    @J
    async def telegram_topics(req):
        return {'enabled':host_app.telegram.topics.enabled(),'topics':[row for row in host_app.telegram.topics.rows().values() if not row.get('replacement')]}

    @r.route('GET','/api/speakers')
    @J
    async def speakers_state(req):
        return host_app.speakers.public()

    @r.route('POST','/api/speakers/discover')
    @J
    async def speakers_discover(req):
        from ..speakers.discover import find
        known={(s['host'],s['port']) for s in host_app.speakers.public()['stations']}
        return {'found':[{**row,'known':(row['host'],row['port']) in known} for row in await asyncio.to_thread(find,2.0)]}

    @r.route('GET','/api/channels/telegram/chats')
    @J
    async def telegram_chats(req):
        return {'chats':host_app.telegram.groups.rows(),'enabled':host_app.settings.get('telegram','groups_enabled',True)}

    @r.route('PATCH','/api/channels/telegram/chats/{chat}')
    @J
    async def telegram_chat_policy(req):
        try:chat=int(req.params['chat'])
        except ValueError:raise HumanError('telegram_group','Неверный id чата','Выбери чат из списка.')
        return await host_app.telegram.groups.configure(chat,body(req))

    @r.route('GET','/api/channels/telegram/profile')
    @J
    async def telegram_profile_plan(req):
        return {'values':host_app.telegram.profile.defaults(),'last':host_app.store.meta_get('telegram:profile',{}),'owner_agent':'main'}

    @r.route('POST','/api/channels/telegram/profile')
    @J
    async def telegram_profile_apply(req):
        from ..channels import media
        from ..tools.registry import ToolContext
        data=body(req)
        avatar=media.get(app.context(),data['avatar'])['path'] if data.get('avatar') else ''
        return await host_app.telegram.profile.apply(ToolContext(app.context(),actor='owner'),data.get('values'),avatar,reviewed=data.get('reviewed') is True)

    @r.route("POST", "/api/channels/telegram/code")
    @J
    async def tg_code(req):
        return {"code": app.telegram.new_code(), "bot": (app.telegram.me or {}).get("username")}

    @r.route("DELETE", "/api/channels/telegram/owner")
    @J
    async def tg_unbind(req):
        app.telegram.unbind()
        return app.telegram.public()

    @r.route("DELETE", "/api/channels/telegram/token")
    @J
    async def tg_forget(req):
        from ..channels.telegram import TOKEN_SECRET

        app.secrets.delete(TOKEN_SECRET)
        app.settings.set("telegram", {"enabled": False}, by="owner")
        await app.telegram.stop()
        app.journal.write("channel", "Telegram: токен бота удалён", actor="owner")
        return app.telegram.public()

    # --- команда ---------------------------------------------------------------------------

    @r.route("GET", "/api/tasks")
    @J
    async def tasks_list(req):
        from ..team import lane_size
        from ..team.roles import public, MODEL_ROLES
        from ..guard.policy import CAPS

        rows = app.team.all(limit=int(req.q("limit") or 100))
        return {"tasks": [{k: v for k, v in t.items() if k not in ("context",)} | {"role_title": app.team.title(t)} for t in rows],
                "roles": {k: public(v) for k, v in app.team.roles().items()},
                "capabilities": CAPS, "model_roles": MODEL_ROLES,
                "model_labels": {role: " → ".join(c.label for c in app.router.chain(role)) or "не выбрана" for role in MODEL_ROLES},
                "parallel": app.settings.get("team", "parallel") or lane_size()}

    @r.route("POST", "/api/team/roles/{name}")
    @J
    async def team_role_save(req):
        from ..tools.registry import ToolContext

        await ToolContext(app, actor="owner").check("settings", "team_roles", "изменить роль команды")
        app.team.save_role(req.params["name"], body(req))
        return {"ok": True}

    @r.route("DELETE", "/api/team/roles/{name}")
    @J
    async def team_role_remove(req):
        from ..tools.registry import ToolContext

        await ToolContext(app, actor="owner").check("settings", "team_roles", "удалить свою роль команды")
        app.team.remove_role(req.params["name"])
        return {"ok": True}

    @r.route("GET", "/api/tasks/{tid}")
    @J
    async def task_detail(req):
        t = app.team.get(req.params["tid"])
        if not t:
            raise HumanError("not_found", "Нет такой задачи", "", status=404)
        msgs = app.store.messages(t["session_id"], limit=200) if t.get("session_id") else []
        return {"task": t, "messages": msgs, "timeline": app.team.timeline(t)}

    @r.route("POST", "/api/tasks/{tid}/cancel")
    @J
    async def task_cancel(req):
        return {"ok": app.team.cancel(req.params["tid"])}

    @r.route('POST','/api/tasks/{tid}/keep')
    @J
    async def task_keep(req):
        from ..tools.registry import ToolContext
        from ..agents.temporary import keep
        await ToolContext(app,actor='owner').check('settings','agents','оставить временного агента задачи')
        return await keep(app.team,req.params['tid'])

    @r.route("GET", "/api/sleep")
    @J
    async def sleep_state(req):
        sl = app.sleep
        return {"enabled": sl.enabled(), "next_at": sl.next_at, "last_at": sl.last_sleep() or None, "running": sl.running, "last": sl.last,
                "has_new": await asyncio.to_thread(sl.has_new), "dreams": app.selfdir.read("DREAMS.md"), "role": sl.role(),
                "heart": app.heart.enabled(), "tried": app.store.meta_get("sleep:last_try"),
                "tidy": sl.tidy_pending(), "fat": [n for n in ("MEMORY.md", "USER.md") if sl.fat(n)]}

    @r.route("POST", "/api/sleep/tidy")
    @J
    async def sleep_tidy(req):
        """Уборка ядра: propose — собрать предложение, apply {id, keys?} — применить выбранное, reject — отклонить."""
        from ..tools.registry import ToolContext

        b = body(req)
        action = str(b.get("action") or "")
        owner = ToolContext(app, actor="owner", session_id="sleep-panel")
        if action == "propose":
            prop = await app.sleep.propose_tidy(reason="хозяин попросил в пульте")
            return {"tidy": app.sleep.tidy_pending(), "count": prop["count"] if prop else 0}
        if action == "apply":
            await owner.check("settings", "memory", "убрать записи из ядра памяти по предложению сна")
            keys = b.get("keys")
            return app.sleep.apply_tidy(str(b.get("id") or ""), [int(k) for k in keys] if isinstance(keys, list) else None)
        if action == "reject":
            app.sleep.reject_tidy()
            return {"ok": True}
        raise HumanError("tidy_action", "Неизвестное действие", "propose, apply или reject.")

    @r.route("POST", "/api/sleep")
    @J
    async def sleep_now(req):
        b = body(req)
        if any(app.mind.running(s) for s in list(app.mind.turns)):
            raise HumanError("busy", "Агент сейчас занят", "Уснуть можно, когда ход закончится.")
        return await app.sleep.run(dry=bool(b.get("dry")), force=bool(b.get("force", True)))

    @r.route("GET", "/api/pulse")
    @J
    async def pulse_state(req):
        from ..heart import pulse as pulsemod

        p = app.heart.pulse
        return {"mode": app.settings.get("heart", "mode"), "enabled": app.heart.enabled(), "next_at": p.next_at, "last_at": p.last_at,
                "last_judged": p.last_judged, "beats": p.beats, "calls": p.calls, "last_result": p.last_result, "quiet": pulsemod.in_quiet(app),
                "checklist": pulsemod.checklist(app.selfdir.read("HEARTBEAT.md"))}

    @r.route("POST", "/api/pulse")
    @J
    async def pulse_now(req):
        return await app.heart.beat(force=bool(body(req).get("force", True)))

    @r.route("POST", "/api/when")
    @J
    async def when_preview(req):
        """Подсказка в форме: как Бендер понял время / cron."""
        from ..heart import cron as cronmod
        from ..heart import when as whenmod
        from ..heart.jobs import describe, make_schedule, next_fire

        b = body(req)
        sch = make_schedule(at=b.get("at") or None, every=b.get("every") or None, cron=b.get("cron") or None)
        t, nxt = time.time(), []
        for _ in range(3):
            t = next_fire(sch, t, window=str(b.get("window") or ""))
            if t is None:
                break
            nxt.append({"ts": t, "human": whenmod.human(t)})
            if sch["kind"] == "at":
                break
        _ = cronmod
        return {"schedule": sch, "describe": describe(sch), "next": nxt}

    # --- вход через ChatGPT --------------------------------------------------------------------

    flows: dict[str, dict] = {}

    def flow_public(f: dict) -> dict:
        return {k: v for k, v in f.items() if not k.startswith("_")}

    @r.route("POST", "/api/auth/chatgpt/start")
    @J
    async def chatgpt_start(req):
        import secrets as _secrets

        from ..router import chatgpt

        method = "browser" if body(req).get("method") == "browser" else "device"
        for f in list(flows.values()):  # новый вход заменяет незаконченный
            if f["state"] == "waiting":
                f["_task"].cancel()
        flows.clear()
        fid = _secrets.token_hex(8)
        flow: dict = {"id": fid, "method": method, "state": "waiting", "started": time.time()}
        if method == "device":
            device = await chatgpt.device_start(app.http)
            flow.update(user_code=device["user_code"], verification_url=device["verification_url"], expires_in=chatgpt.DEVICE_TIMEOUT)

            async def obtain():
                return await chatgpt.device_wait(app.http, device)
        else:
            login = chatgpt.BrowserLogin(app.http)
            await login.start()
            flow.update(url=login.url, listening=login.listening, _login=login)

            async def obtain():
                return await login.wait()

        async def runner():
            try:
                tokens = await obtain()
                p = app.models.connect_chatgpt(tokens)
                app.journal.write("models", f"Вход через ChatGPT: {tokens.get('email') or tokens.get('account_id')}", actor="owner")
                flow.update(state="done", provider=p["id"], email=tokens.get("email"))
            except HumanError as e:
                flow.update(state="error", error=e.to_dict())
            except asyncio.CancelledError:
                flow.update(state="cancelled")
                if flow.get("_login"):
                    await flow["_login"].close()
                raise
            main = (app.models.roles().get("main") or [{}])[0]
            app.bus.publish({"type": "chatgpt_login", "flow": fid, "state": flow["state"], "email": flow.get("email"),
                             "main_model": main.get("model") if main.get("provider") == flow.get("provider") else None,
                             "error": (flow.get("error") or {}).get("title")})

        flow["_task"] = asyncio.ensure_future(runner())
        flows[fid] = flow
        return flow_public(flow)

    def get_flow(req) -> dict:
        f = flows.get(req.params["fid"])
        if not f:
            raise HumanError("gone", "Этот вход уже закрыт", "Начни вход заново.", status=410)
        return f

    @r.route("GET", "/api/auth/chatgpt/{fid}")
    @J
    async def chatgpt_flow(req):
        f = get_flow(req)
        out = flow_public(f)
        if f["state"] == "done":
            out["provider"] = next((p for p in app.models.public_providers() if p["id"] == f.get("provider")), None)
            main = (app.models.roles().get("main") or [{}])[0]
            out["main_model"] = main.get("model") if main.get("provider") == f.get("provider") else None
        return out

    @r.route("POST", "/api/auth/chatgpt/{fid}/paste")
    @J
    async def chatgpt_paste(req):
        f = get_flow(req)
        login = f.get("_login")
        if not login or not login.paste(str(body(req).get("text") or "")):
            raise HumanError("bad_paste", "Не похоже на адрес возврата из входа OpenAI", "Скопируй адрес из адресной строки целиком — он начинается с http://localhost:1455/auth/callback?code=…")
        return {"ok": True}

    @r.route("POST", "/api/auth/chatgpt/{fid}/cancel")
    @J
    async def chatgpt_cancel(req):
        f = get_flow(req)
        if f["state"] == "waiting":
            f["_task"].cancel()
        return {"ok": True}

    # --- переезд из OpenClaw ------------------------------------------------------------------

    @r.route("GET", "/api/import/openclaw")
    @J
    async def oc_find(req):
        from ..importers import openclaw as oc

        installs = await asyncio.to_thread(oc.find_installs, req.q("path") or None, app=app.context())
        return {"installs": [i.public() for i in installs], "last": app.store.meta_get("import:openclaw:last")}

    @r.route("POST", "/api/import/openclaw/plan")
    @J
    async def oc_plan(req):
        from ..importers import flock

        b = body(req)
        return await asyncio.to_thread(flock.make_plan,app.context(),'openclaw',b.get('path'),b.get('agent',''),b.get('mode','new'),b.get('profiles'))

    @r.route("POST", "/api/import/openclaw/apply")
    @J
    async def oc_apply(req):
        from ..importers import flock
        from ..tools.registry import ToolContext
        b = body(req)
        await ToolContext(app.context(),actor='owner').check('settings','migration','перенести выбранные пункты OpenClaw')
        return await flock.apply(app.context(),'openclaw',b.get('path'),agent=b.get('agent',''),destination=b.get('mode','new'),
                                 expected=b.get('hash'),items=b.get('items'),secrets=b.get('secrets',True),confirmed=b.get('confirmed') is True,profiles=b.get('profiles'))

    @r.route("GET", "/api/import/{kind}")
    @J
    async def portable_find(req):
        from ..importers import portable

        return await asyncio.to_thread(portable.discover, app, req.params["kind"], req.q("path") or None)

    @r.route("POST", "/api/import/{kind}/plan")
    @J
    async def portable_plan(req):
        from ..importers import portable,flock
        from ..tools.registry import ToolContext

        data = body(req)
        root = portable.source(app, req.params["kind"], data.get("path"))
        await ToolContext(app, actor="owner", session_id="migration-panel").check("fs.read", str(root), "прочитать источник переезда")
        return await asyncio.to_thread(flock.make_plan,app.context(),req.params['kind'],str(root),'',data.get('mode','new'))

    @r.route("POST", "/api/import/{kind}/apply")
    @J
    async def portable_apply(req):
        from ..importers import portable,flock
        from ..tools.registry import ToolContext

        data = body(req)
        root = portable.source(app, req.params["kind"], data.get("path"))
        ctx = ToolContext(app, actor="owner", session_id="migration-panel")
        await ctx.check("fs.read", str(root), "прочитать выбранный источник переезда")
        await ctx.check("settings", "migration", "перенести выбранные пункты плана")
        return await flock.apply(app.context(),req.params['kind'],str(root),destination=data.get('mode','new'),expected=data.get('hash'),
                                 items=data.get('items'),confirmed=data.get('confirmed') is True)

    @r.route("GET", "/api/workshop")
    @J
    async def workshop_list(req):
        return app.workshop.public()

    @r.route("GET", "/api/workshop/metrics")
    @J
    async def workshop_metrics(req):
        from ..workshop.metrics import snapshot

        return snapshot(app)

    @r.route("GET", "/api/workshop/{name}")
    @J
    async def workshop_detail(req):
        record = app.workshop.get(req.params["name"])
        if record["kind"] == "tool":
            folder = app.workshop.root / record["id"] / "package"
            record["files"] = {name: (folder / name).read_text(encoding="utf-8") for name in ("server.py", "test_tool.py", "bender-module.json")}
        elif record["kind"] == "core":
            record["diff"] = app.workshop.get(record["id"])["diff"]
        return record

    @r.route("POST", "/api/workshop/{name}/approve")
    @J
    async def workshop_approve(req):
        from ..tools.registry import ToolContext
        if app.workshop.get(req.params["name"])["kind"] != "tool":
            raise HumanError("core_removed", "Агент больше не правит своё ядро", "Старое предложение доступно только как история.")
        from ..workshop.tools import approve

        record = await approve(app.workshop, req.params["name"], body(req).get("hash"), ToolContext(app, actor="owner", session_id="workshop-panel"))
        return record

    @r.route("POST", "/api/workshop/{name}/reject")
    @J
    async def workshop_reject(req):
        from ..tools.registry import ToolContext

        record = app.workshop.get(req.params["name"])
        if record.get("kind") != "tool":
            raise HumanError("core_removed", "Старая правка доступна только как история", "Агент создаёт навыки и отдельные модули.")
        if record["status"] not in ("waiting", "failed"):
            raise HumanError("proposal_status", "Сейчас предложение нельзя отклонить", "Дождись проверки или выключи установленный тул.")
        await ToolContext(app, actor="owner", session_id="workshop-panel").check("workshop.propose", record["id"], "отклонить предложение")
        record["status"] = "rejected"
        app.workshop.save(record)
        app.journal.write("workshop", "Хозяин отклонил предложение", actor="owner", data={"id": record["id"]})
        return record

    @r.route("POST", "/api/workshop/{name}/definitions")
    @J
    async def workshop_definitions(req):
        from ..tools.registry import ToolContext
        from ..workshop.tools import definitions

        return await definitions(app.workshop, req.params["name"], ToolContext(app, actor="owner", session_id="workshop-panel"), body(req).get("review"))

    @r.route("POST", "/api/workshop/{name}/enabled")
    @J
    async def workshop_enabled(req):
        from ..tools.registry import ToolContext

        record = app.workshop.get(req.params["name"])
        if record.get("kind") != "tool" or record.get("status") not in ("approved", "applied", "disabled"):
            raise HumanError("proposal_status", "Этот тул ещё не включён", "Сначала просмотри и одобри предложение.")
        enabled = body(req).get("enabled") is True
        await ToolContext(app, actor="owner", session_id="workshop-panel").check("settings", record["module"], "включить или выключить одобренный тул")
        await app.modules.set_enabled(record["module"], enabled)
        record["status"] = "approved" if enabled else "disabled"
        app.workshop.save(record)
        return record

    @r.route("POST", "/api/workshop/live")
    @J
    async def workshop_live(req):
        from ..tools.registry import ToolContext
        from ..workshop.live import run

        return await run(app, ToolContext(app, actor="owner", session_id="workshop-panel"))

    @r.route("POST", "/api/workshop/exams")
    @J
    async def workshop_exams(req):
        from ..tools.registry import ToolContext

        return await app.workshop.run_exams(ToolContext(app, actor="owner", session_id="workshop-panel"))

    @r.route("GET", "/api/versions")
    @J
    async def versions_list(req):
        return app.versions.public()

    @r.route('POST', '/api/update/check')
    @J
    async def update_check(req):
        return await app.updater.check()

    @r.route('GET', '/api/update/progress')
    @J
    async def update_progress(req):
        return app.updater.progress()

    @r.route('POST', '/api/update/install')
    @J
    async def update_install(req):
        if app.mind.turns or app.team.running:
            raise HumanError('update_busy', 'Сначала дождись завершения ходов', 'Обновление перезапустит программу.')
        plan = dict(body(req))
        skip = plan.pop('skip_backup', False) is True  # только хозяин из пульта: один раз, без записи в настройки
        result = await app.updater.install(plan, skip_backup=skip)
        if result['updated']: result['restart'] = await app.updater.restart()
        app.store.meta_set('updates:available', None)
        return result

    @r.route('POST', '/api/update/realign')
    @J
    async def update_realign(req):
        if app.mind.turns or app.team.running:
            raise HumanError('update_busy', 'Сначала дождись завершения ходов', 'Выравнивание заменит программу и потребует перезапуска.')
        result = await app.updater.realign(skip_backup=body(req).get('skip_backup') is True)
        if result['realigned']: result['restart'] = await app.updater.restart()
        return result

    @r.route("POST", "/api/versions/stage")
    @J
    async def versions_stage(req):
        from ..tools.registry import ToolContext

        data = body(req)
        ctx = ToolContext(app, actor="owner", session_id="versions-panel")
        if data.get("git"):
            result = await app.versions.stage_git(str(data["git"]), ctx, str(data.get("ref") or ""))
        else:
            source = Path(str(data.get("path") or "")).expanduser().resolve()
            await ctx.check("fs.read", str(source), "прочитать пакет версии")
            result = await asyncio.to_thread(app.versions.stage, source, development=data.get("development") is True)
        return result

    @r.route("GET", "/api/versions/{name}/file")
    @J
    async def versions_file(req):
        from ..skills.manager import file_path

        record = app.versions.inspect(req.params["name"])
        rel = str(file_path(req.q("path") or "bender/__init__.py"))
        if rel not in record["manifest"]:
            raise HumanError("update_file", "Нет такого файла версии", "Выбери файл из списка.")
        raw = (app.versions.staging / record["id"] / rel).read_bytes()
        text = app.guard.scrub(raw.decode("utf-8", "replace"))
        offset = max(0, int(req.q("offset") or 0))
        return {"text":text[offset:offset+40000], "next":offset+40000 if len(text)>offset+40000 else None}

    @r.route("POST", "/api/versions/{name}/install")
    @J
    async def versions_install(req):
        from ..tools.registry import ToolContext

        data = body(req)
        record = app.versions.inspect(req.params["name"])
        if not getattr(app, "watched", False) or not hasattr(app, "request_restart"):
            raise HumanError("update_watchdog", "Применение требует Сторожа", "Запусти обычный bender run, без --no-watchdog.")
        await ToolContext(app, actor="owner", session_id="versions-panel").check("self.code", record["id"], "применить просмотренную версию с автооткатом", detail={"hash":record["hash"],"signed":record["signed"]})
        result = await asyncio.to_thread(app.versions.install, record["id"], data.get("hash"))
        app.request_restart()
        return result

    @r.route("POST", "/api/versions/rollback")
    @J
    async def versions_rollback(req):
        from ..tools.registry import ToolContext

        if not getattr(app, "watched", False) or not hasattr(app, "request_restart"):
            raise HumanError("update_watchdog", "Откат требует Сторожа", "Запусти обычный bender run.")
        await ToolContext(app, actor="owner", session_id="versions-panel").check("self.code", "rollback", "вернуться на прежний слот")
        result = app.versions.rollback()
        app.request_restart()
        return result

    @r.route("GET", "/api/backups")
    @J
    async def backups_status(req):
        return app.backups.status()

    @r.route("POST", "/api/backups/{action}")
    @J
    async def backups_action(req):
        body = req.json()
        action = req.params["action"]
        if action == "now":
            return await app.backups.now()
        if action == "verify":
            return await app.backups.verify(full=bool(body.get("full")))
        if action == "repair":
            return await app.backups.repair()
        if action in ("preview", "restore"):
            if not body.get("target"):
                raise HumanError("backup_target", "Укажи новую папку данных", "Сначала предпросмотр, затем восстановление.")
            args = (body.get("id"), body["target"])
            kw = {"destination": int(body.get("destination", 0))}
            return app.backups.preview(*args, **kw) if action == "preview" else await app.backups.restore(*args, **kw)
        raise HumanError("backup_action", "Неизвестное действие бекапа", "Используй кнопки раздела Бекапы.")

    @r.route("GET", "/api/avatar")
    @J
    async def avatar_get(req):
        from ..memory.avatar import read
        return {"avatar": read(app.dirs.self_dir), "imported": [p.name for p in (app.dirs.self_dir / "avatars").glob("*") if p.is_file()]}

    @r.route("POST", "/api/avatar")
    @J
    async def avatar_set(req):
        from ..tools.registry import ToolContext
        from ..memory.avatar import validate, write
        value = validate(req.json())
        await ToolContext(app, actor="owner").check("memory.core", "AVATAR.json", "изменить внешний вид визора", detail=value)
        value = write(app.dirs.self_dir, value)
        app.bus.publish({"type": "identity"})
        return {"avatar": value}

    @r.route('GET','/api/voice/local')
    @J
    async def local_voice_status(req):
        from ..voice.cli import status
        return {**status(app.context()),'agent':app.context().agent_id}

    @r.route('POST','/api/voice/local/connect')
    @J
    async def local_voice_connect(req):
        from ..voice.connect import connect
        from ..tools.registry import ToolContext
        return await connect(ToolContext(app.context(),actor='owner'))

    @r.route('POST','/api/voice/local/bench')
    @J
    async def local_voice_bench(req):
        from ..voice.cli import bench
        return await bench(app.context())

    @r.route('POST','/api/voice/local/stop')
    @J
    async def local_voice_stop(req):
        from ..voice.cli import stop
        return await stop(app.context())

    @r.route("GET", "/api/voice")
    @J
    async def voice_status(req):
        return {"settings": app.settings.group("voice"), "roles": {k:app.models.roles().get(k,[]) for k in ("ears","voice")}}

    @r.route("POST", "/api/voice/transcribe")
    @J
    async def voice_transcribe(req):
        import base64
        data=body(req)
        try:
            audio=base64.b64decode(data.get("audio",""),validate=True)
        except (ValueError,TypeError):
            raise HumanError("voice_data", "Аудио повреждено", "Запиши сообщение заново.") from None
        from ..voice.input import prepare,attachment
        sid=data.get('session') or app.main_session()
        if not app.store.session(sid):raise HumanError('voice_session','Разговор для голосового уже закрыт','Выбери действующий разговор и запиши сообщение заново.')
        result=await prepare(app.context(),audio,ext=data.get('ext','webm'),seconds=data.get('seconds',0),session=sid,from_owner=True)
        item=await attachment(app.context(),result,session=sid,source='pult_voice')
        result.pop('audio',None)
        if item:result['attachment']=item
        return result

    @r.route("POST", "/api/voice/speech")
    @J
    async def voice_speech(req):
        import base64
        result=await app.voice.speak(body(req).get("text"),session_id=app.main_session())
        result["audio"]=base64.b64encode(result["audio"]).decode()
        return result

    # --- интерфейс ----------------------------------------------------------------------------

    @r.route("GET", "/")
    async def index(req):
        return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})

    @r.route("GET", "/static/{path:rest}")
    async def static(req):
        p = (STATIC / req.params["path"]).resolve()
        if not within(p, STATIC) or not p.is_file():
            return JSONResponse({"error": {"code": "not_found", "title": "Нет такого файла"}}, 404)
        return FileResponse(p, headers={"Cache-Control": "no-cache"})

    # --- почта ---------------------------------------------------------------------------------

    MAIL_FIELDS = ("address", "login", "name", "imap_host", "imap_port", "imap_security", "smtp_host", "smtp_port", "smtp_security", "folder",
                   "poll_minutes", "triage", "on_new", "known", "owner_addresses", "enabled", "images")

    def mail_state() -> dict:
        out = app.mail.public()
        out["letters"] = [{k: L.get(k) for k in ("id", "dir", "from_addr", "from_name", "to_addr", "subject", "date", "verdict", "summary", "status", "task")}
                          for L in app.mail.recent(30)]
        return out

    @r.route("GET", "/api/channels/mail")
    @J
    async def mail_get(req):
        return mail_state()

    @r.route("GET", "/api/channels/mail/guess")
    @J
    async def mail_guess(req):
        from ..channels.mail import guess

        return guess(req.q("address") or "")

    @r.route("POST", "/api/channels/mail")
    @J
    async def mail_save(req):
        from ..channels.mail import SECRET

        b = body(req)
        if b.get("password"):
            app.secrets.set(SECRET, str(b["password"]), note="пароль почты")
        changes = {k: b[k] for k in MAIL_FIELDS if k in b}
        if changes:
            app.settings.set("mail", changes, by="owner")
        if b.get("check"):
            await app.mail.check()  # не вошёл — ошибка по-человечески, настройки уже сохранены
        app.journal.write("channel", "Почта: настройки изменены", actor="owner", data={k: v for k, v in changes.items() if k != "known"})
        if app.mail.enabled() and not (app.mail.task and not app.mail.task.done()):
            app.mail.start()
        return mail_state()

    @r.route("POST", "/api/channels/mail/check")
    @J
    async def mail_check(req):
        return await app.mail.check()

    @r.route("POST", "/api/channels/mail/poll")
    @J
    async def mail_poll(req):
        got = await app.mail.poll()
        return {"new": len(got), **mail_state()}

    @r.route("DELETE", "/api/channels/mail")
    @J
    async def mail_forget(req):
        from ..channels.mail import SECRET

        app.settings.set("mail", {"enabled": False}, by="owner")
        app.secrets.delete(SECRET)
        await app.mail.stop()
        app.journal.write("channel", "Почта отключена, пароль удалён", actor="owner")
        return mail_state()

    @r.route("GET", "/api/mail/{mid}")
    @J
    async def mail_letter(req):
        L = app.mail.get(req.params["mid"])
        if not L:
            raise HumanError("no_mail", "Нет такого письма", "", status=404)
        return {"letter": L}

    # --- вебхуки -------------------------------------------------------------------------------

    @r.route("GET", "/api/hooks")
    @J
    async def hooks_list(req):
        from ..channels.hooks import ACTIONS

        agents = [{"id": a["id"], "name": a["name"]} for a in app.agents.public() if not a["archived"]]
        return {"hooks": [app.hooks.public(h) for h in app.hooks.all()], "actions": ACTIONS, "agents": agents}

    @r.route("POST", "/api/hooks")
    @J
    async def hooks_create(req):
        b = body(req)
        h = app.hooks.create(str(b.get("name") or ""), str(b.get("action") or "notify"), str(b.get("instruction") or ""), int(b.get("per_hour") or 30),
                             agent=str(b.get("agent") or "main"))
        return {"hook": app.hooks.public(h)}

    @r.route("PATCH", "/api/hooks/{hid}")
    @J
    async def hooks_patch(req):
        return {"hook": app.hooks.public(app.hooks.update(req.params["hid"], body(req)))}

    @r.route("POST", "/api/hooks/{hid}/secret")
    @J
    async def hooks_secret(req):
        app.hooks.new_secret(req.params["hid"])
        return {"hook": app.hooks.public(app.hooks.get(req.params["hid"]))}

    @r.route("DELETE", "/api/hooks/{hid}")
    @J
    async def hooks_delete(req):
        app.hooks.remove(req.params["hid"])
        app.journal.write("channel", "Вебхук удалён", actor="owner", data={"hook": req.params["hid"]})
        return {"ok": True}

    @r.route("GET", "/api/hooks/{hid}/events")
    @J
    async def hooks_events(req):
        return {"events": app.hooks.events(req.params["hid"])}

    @r.route("POST", "/api/hook-events/{eid}/retry")
    @J
    async def hooks_event_retry(req):
        return {"event": app.hooks.retry(req.params["eid"])}

    @r.route("GET", "/hook/{hid}/events/{eid}")
    async def hook_event_status(req):  # судьба события — тем же секретом вебхука
        code, out = app.hooks.event_status(req.params["hid"], req.params["eid"], req)
        return JSONResponse(out, code)

    @r.route("POST", "/hook/{hid}")
    async def hook_in(req):
        code, out = await app.hooks.receive(req.params["hid"], req)
        return JSONResponse(out, code)

    @r.route("GET", "/hook/{hid}")
    async def hook_in_get(req):  # для служб, которые умеют только GET (параметры — в адресе)
        code, out = await app.hooks.receive(req.params["hid"], req)
        return JSONResponse(out, code)

    # --- Бендер как модель: OpenAI-совместимый API (свой вход — ключом) -----------------------

    from ..channels import openai_api

    openai_api.register(r, app)

    @r.route("GET", "/api/channels/api")
    @J
    async def api_state(req):
        key = app.secrets.get(openai_api.KEY_SECRET)
        return {"enabled": bool(app.settings.get("api", "enabled", False)), "key": key, "models": openai_api.catalog(app.host),"agent_id":app.agent_id,"api_rights":app.settings.group('api_rights')}

    @r.route("POST", "/api/channels/api/key")
    @J
    async def api_new_key(req):
        import secrets as _secrets

        key = "bender-" + _secrets.token_urlsafe(24)
        app.secrets.set(openai_api.KEY_SECRET, key, note="ключ OpenAI-совместимого API Бендера")
        if app.host._api_runtime is not None:app.host._api_runtime.invalidate()
        app.journal.write("channel", "API: новый ключ (старый больше не действует)", actor="owner")
        return {"key": key}

    # --- внешние MCP-серверы: конфигурация и одобрение только из пульта хозяина -----------------

    @r.route("GET", "/api/mcp")
    @J
    async def mcp_list(req):
        return app.mcp.public()

    @r.route("POST", "/api/mcp")
    @J
    async def mcp_save(req):
        return await app.mcp.save(body(req))

    @r.route('POST','/api/mcp/tasks/{key}/{action}')
    @J
    async def mcp_task_action(req):
        from ..tools.registry import ToolContext
        manager=app.mcp
        await manager.tasks.request(req.params['action'],req.params['key'],ToolContext(manager.app,actor='owner',session_id='mcp-panel'))
        return manager.public()

    @r.route('POST','/api/mcp/{sid}/subscriptions')
    @J
    async def mcp_subscribe(req):
        from ..tools.registry import ToolContext
        manager=app.mcp;data=body(req)
        if data.get('stop') is True:
            await manager.subscriptions.stop(req.params['sid']);return manager.public()
        return await manager.subscriptions.start(req.params['sid'],data.get('notifications'),ToolContext(manager.app,actor='owner',session_id='mcp-panel'))

    @r.route("POST", "/api/mcp/import")
    @J
    async def mcp_import(req):
        return app.mcp.import_servers(body(req).get("config"), source="claude", secrets=body(req).get("secrets") is True)

    @r.route('POST','/api/mcp/inputs/{pid}')
    @J
    async def mcp_input_answer(req):
        pending=getattr(app.host,'_mcp_input_broker',None)
        if pending is None:raise HumanError('mcp_input','Нет открытой формы MCP','Дождись запроса сервера.')
        data=body(req)
        return pending.answer(req.params['pid'],data.get('action'),data.get('content'))

    @r.route("POST", "/api/mcp/{sid}/discover")
    @J
    async def mcp_discover(req):
        from ..tools.registry import ToolContext

        return await app.mcp.discover(req.params["sid"], ToolContext(app=app, actor="owner", session_id="mcp-panel"))

    @r.route('POST','/api/mcp/{sid}/content/discover')
    @J
    async def mcp_content_discover(req):
        from ..tools.registry import ToolContext
        return await app.mcp.catalog.discover(req.params['sid'],ToolContext(app=app,actor='owner',session_id='mcp-panel'))

    @r.route('POST','/api/mcp/{sid}/content/approve')
    @J
    async def mcp_content_approve(req):
        return app.mcp.catalog.approve(req.params['sid'],body(req).get('review'))

    @r.route("POST", "/api/mcp/{sid}/approve")
    @J
    async def mcp_approve(req):
        return app.mcp.approve(req.params["sid"], body(req).get("review"))

    @r.route("DELETE", "/api/mcp/{sid}")
    @J
    async def mcp_delete(req):
        await app.mcp.remove(req.params["sid"])
        return app.mcp.public()

    # --- навыки -------------------------------------------------------------------------------

    @r.route("GET", "/api/modules")
    @J
    async def modules_list(req):
        return app.modules.public()

    @r.route("POST", "/api/modules/import")
    @J
    async def modules_import(req):
        from ..tools.registry import ToolContext

        data = body(req)
        ctx = ToolContext(app, actor="owner", session_id="modules-panel")
        if data.get("git"):
            result = await app.modules.import_git(str(data["git"]), ctx, ref=str(data.get("ref") or ""), subdir=str(data.get("subdir") or ""))
        else:
            if not data.get("path"):
                raise HumanError("module_path", "Укажи папку модуля", "Внутри нужен bender-module.json.")
            path = app.guard.policy.resolve(str(data["path"]))
            await ctx.check("fs.read", str(path), "прочитать пакет модуля " + str(path))
            result = await asyncio.to_thread(app.modules.import_path, path)
        return {**result, **app.modules.public()}

    @r.route("GET", "/api/modules/{name}/file")
    @J
    async def modules_file(req):
        item, content = app.modules.read(req.params["name"], req.q("path") or "bender-module.json", candidate=req.q("candidate") == "1")
        try:
            text = app.guard.scrub(content.decode("utf-8-sig"))
        except UnicodeError:
            raise HumanError("module_binary", "Это двоичный файл", "Содержимое включено в хэш пакета.") from None
        offset = max(0, int(req.q("offset") or 0))
        return {"name": item["id"], "path": req.q("path") or "bender-module.json", "hash": item["hash"], "text": text[offset:offset+24000],
                "next_offset": offset+24000 if offset+24000 < len(text) else None}

    @r.route("POST", "/api/modules/{name}/approve")
    @J
    async def modules_approve(req):
        data = body(req)
        return await app.modules.approve(req.params["name"], data.get("hash"), candidate=data.get("candidate") is True)

    @r.route("POST", "/api/modules/{name}/enabled")
    @J
    async def modules_enabled(req):
        await app.modules.set_enabled(req.params["name"], body(req).get("enabled") is True)
        return app.modules.public()

    @r.route("DELETE", "/api/modules/{name}")
    @J
    async def modules_remove(req):
        return await app.modules.remove(req.params["name"])

    @r.route("POST", "/api/modules/{name}/discover")
    @J
    async def modules_discover(req):
        from ..tools.registry import ToolContext

        sid = app.modules.trust(req.params["name"]).get("server")
        if not sid:
            raise HumanError("module_mcp", "У модуля нет одобренной точки MCP", "Сначала просмотри пакет.")
        await app.mcp.discover(sid, ToolContext(app, actor="owner", session_id="modules-panel"))
        return app.mcp.public()

    @r.route("POST", "/api/modules/{name}/channel")
    @J
    async def modules_channel(req):
        data = body(req)
        if app.modules.package(req.params["name"])["hash"] != data.get("hash"):
            raise HumanError("module_changed", "Пакет изменился после просмотра", "Обнови список модулей.")
        return app.modules.add_channel(req.params["name"], str(data.get("channel") or ""))

    @r.route("POST", "/api/modules/{name}/provider")
    @J
    async def modules_provider(req):
        from ..tools.registry import ToolContext

        item = app.modules.package(req.params["name"])
        if not app.modules.active(item):
            raise HumanError("module_off", "Модуль выключен или не одобрен", "Просмотри пакет.")
        data = body(req)
        if item["hash"] != data.get("hash"):
            raise HumanError("module_changed", "Пакет изменился после просмотра", "Обнови список модулей.")
        providers = item["metadata"].get("providers", [])
        index = int(data.get("index", -1))
        if not 0 <= index < len(providers):
            raise HumanError("module_provider", "Нет такого предложения провайдера", "Обнови список.")
        proposal = providers[index]
        await ToolContext(app, actor="owner").check("settings", "providers", "подключить описанный модулем API " + proposal["base_url"])
        if app.modules.package(item["id"])["hash"] != item["hash"]:
            raise HumanError("module_changed", "Пакет изменился во время одобрения", "Повтори просмотр.")
        provider = app.models.add_provider(name=str(proposal.get("name") or "API модуля " + item["id"]), kind=proposal["kind"],
                                           base_url=proposal["base_url"], api_key=data.get("api_key") or None)
        return {"id": provider["id"], "name": provider["name"]}

    @r.route("GET", "/api/modules/{name}/page/{hash}/{path:rest}")
    async def modules_page(req):
        import mimetypes

        item, content = app.modules.read(req.params["name"], req.params["path"])
        if not app.modules.active(item) or item["hash"] != req.params["hash"]:
            raise HumanError("module_page", "Страница модуля выключена или изменилась", "Обнови модуль.", status=409)
        content_type = mimetypes.guess_type(req.params["path"])[0] or "application/octet-stream"
        return Response(content, headers={"Content-Type": content_type, "X-Content-Type-Options": "nosniff", "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; script-src 'none'; object-src 'none'; frame-ancestors 'self'; base-uri 'none'; form-action 'none'; sandbox"})

    @r.route("POST", "/api/search/test")
    @J
    async def search_test(req):
        from ..tools.registry import ToolContext

        data = body(req)
        return await app.search.search(str(data.get("query") or "Бендер"), 3, "", ToolContext(app, actor="owner", session_id="search-panel", tool="web_search"), provider=str(data.get("provider") or ""))

    @r.route("GET", "/api/skills")
    @J
    async def skills_list(req):
        return app.skills.public()

    @r.route("POST", "/api/skills/import")
    @J
    async def skills_import(req):
        from ..tools.registry import ToolContext

        data = body(req)
        path = app.guard.policy.resolve(str(data.get("path") or ""))
        if not data.get("path"):
            raise HumanError("skill_path", "Укажи папку навыков", "Папку с SKILL.md или коллекцию навыков.")
        await ToolContext(app, actor="owner", session_id="skills-panel").check("fs.read", str(path), f"прочитать и импортировать папку навыков {path}")
        result = await asyncio.to_thread(app.skills.import_path, path, source=str(data.get("source") or "local")[:40])
        return {**result, **app.skills.public()}

    @r.route("GET", "/api/skills/{name}/file")
    @J
    async def skills_file(req):
        return app.skills.read(req.params["name"], req.q("path") or "SKILL.md", offset=int(req.q("offset") or 0), max_chars=24000, review=True)

    @r.route("GET", "/api/skills/{name}/history")
    @J
    async def skills_history(req):
        from ..skills.experience import history

        return {"versions": history(app.skills, req.params["name"])}

    @r.route("POST", "/api/skills/{name}/edit")
    @J
    async def skills_edit(req):
        from ..skills.experience import edit
        from ..tools.registry import ToolContext

        data = body(req)
        return await edit(app.skills, req.params["name"], data.get("text"), data.get("hash"), ToolContext(app, actor="owner", session_id="skills-panel"))

    @r.route("POST", "/api/skills/{name}/rollback")
    @J
    async def skills_rollback(req):
        from ..skills.experience import rollback
        from ..tools.registry import ToolContext

        data = body(req)
        return await rollback(app.skills, req.params["name"], data.get("version"), data.get("hash"), ToolContext(app, actor="owner", session_id="skills-panel"))

    @r.route("POST", "/api/skills/{name}/state")
    @J
    async def skills_state(req):
        from ..skills.experience import update
        from ..tools.registry import ToolContext

        name = req.params["name"]
        app.skills.package(name)
        await ToolContext(app, actor="owner", session_id="skills-panel").check("skill.learn", name, "изменить закрепление или архив навыка")
        data = body(req)
        changes = {key: data[key] for key in ("pinned", "archived") if type(data.get(key)) is bool}
        update(app.skills, name, **changes)
        app.journal.write("skill", f"Состояние навыка {name} изменено хозяином", actor="owner", data=changes)
        return app.skills.public()

    @r.route("POST", "/api/skills/{name}/approve")
    @J
    async def skills_approve(req):
        return app.skills.approve(req.params["name"], body(req).get("hash"))

    @r.route("POST", "/api/skills/{name}/enabled")
    @J
    async def skills_enabled(req):
        app.skills.set_enabled(req.params["name"], body(req).get("enabled") is True)
        return app.skills.public()

    # --- защита -------------------------------------------------------------------------------

    async def handler(req: Request) -> Response:
        if re.fullmatch(r"/hook/[A-Za-z0-9_-]{1,40}(?:/events/[A-Za-z0-9_-]{1,64})?", req.path) and req.method in ("GET", "POST"):
            # вебхуки: вход — секретом вебхука (проверяется в обработчике). Имя сервера не проверяется (службы приходят через
            # туннель со своим именем), кука и CSRF неприменимы — без секрета запрос ничего не сделает
            try:
                return await r(req)
            except Exception as e:  # noqa: BLE001
                return error_response(unexpected(e, f"{req.method} {req.path}"))
        host = (req.host.rsplit(":", 1)[0] if not req.host.startswith("[") else req.host.split("]")[0] + "]").lower()
        if host not in hosts:
            return JSONResponse({"error": {"code": "bad_host", "title": "Чужое имя сервера", "hint": "Открой пульт по адресу 127.0.0.1."}}, 421)
        if req.method=='GET' and (req.path=='/api/mcp/oauth/callback' or re.fullmatch(r'/mcp-client/[0-9a-f]{32}\.json',req.path)):
            from .mcp_oauth_api import public as oauth_public
            return await oauth_public(host_app,req)
        if req.path.startswith("/v1/"):
            # OpenAI-совместимый API: свой вход ключом (проверяется в обработчике), CSRF неприменим —
            # браузер не подставит заголовок Authorization сам
            try:
                return await r(req)
            except HumanError as e:
                return error_response(e)
            except Exception as e:  # noqa: BLE001
                return error_response(unexpected(e, f"{req.method} {req.path}"))
        if token:
            cookie = _cookie(req, TOKEN_COOKIE)
            given = req.q("token")
            if req.method == 'GET' and req.path == '/' and given and hmac.compare_digest(given, token):
                resp = Response(b"", 302, {"Location": "/", 'Cache-Control':'no-store', 'Referrer-Policy':'no-referrer'})
                resp.headers.add("Set-Cookie", f"{TOKEN_COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/; Max-Age=31536000")
                return resp
            if not (cookie and hmac.compare_digest(cookie, token)):
                return JSONResponse({"error": {"code": "token", "title": "Нужна ссылка с токеном", "hint": "В терминале этой установки выполни bender link и открой полученную ссылку."}}, 401)
        if req.method not in ("GET", "HEAD"):
            if req.headers.get("x-bender") != "1":
                return JSONResponse({"error": {"code": "csrf", "title": "Запрос без заголовка пульта", "hint": "Меняющие запросы принимаются только от пульта."}}, 403)
            origin = req.headers.get("origin")
            if origin and origin.split("://", 1)[-1].lower() != req.host.lower():
                return JSONResponse({"error": {"code": "csrf", "title": "Запрос с чужой страницы", "hint": ""}}, 403)
        active_token = None
        try:
            agent_id = req.headers.get('x-bender-agent') or req.q('agent') or 'main'
            ctx = host_app.agents.get(agent_id)
            active_token = ACTIVE.set(ctx)
            return await r(req)
        except HumanError as e:
            return error_response(e)
        except Exception as e:  # noqa: BLE001
            return error_response(unexpected(e, f"{req.method} {req.path}"))
        finally:
            if active_token is not None: ACTIVE.reset(active_token)

    return handler


def _boot_info(app) -> dict:
    from ..core.watchdog import load_boot

    boot = load_boot(app.dirs.root)
    crash = app.dirs.local / "last_crash.txt"
    tail = ""
    if app.safe and crash.is_file():
        try:
            lines = [ln for ln in crash.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
            tail = lines[-1][:300] if lines else ""
        except OSError:
            pass
    return {"watched": bool(getattr(app, "watched", False)), "safe": boot.get("safe"), "starts": boot.get("starts", 0),
            "last_crash": boot.get("last_crash"), "crash_line": tail}


def _cookie(req: Request, name: str) -> str | None:
    raw = req.headers.get("cookie") or ""
    for part in raw.split(";"):
        k, _, v = part.strip().partition("=")
        if k == name:
            return v
    return None


_ = time
