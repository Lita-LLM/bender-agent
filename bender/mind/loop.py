"""Разум: агентный ход. Контекст → модель → тулы (через Предохранитель) → … → ответ.

Ход живёт независимо от того, кто его смотрит: вкладка может
закрыться, ответ всё равно допишется и сохранится. Остановка и ошибка сохраняют то, что
успело прийти.

Сообщения хозяина, пришедшие во время хода, не ждут его конца: они вставляются в идущий
ход на ближайшей границе тула («стой, не туда»).

Лимиты: шаги с тулами, время, деньги на ход. Детектор петель: один и тот же вызов с теми
же аргументами и результатами трижды, включая чередование двух — остановка с объяснением.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field

from ..core.errors import HumanError, unexpected
from ..guard import Halted
from ..router import cli as clipkg
from ..router.errors import ProviderError
from ..tools.registry import ToolContext
from . import compact
from . import context as ctxmod
from . import review as reviewmod


# Кто может начать ход, не заражая его: хозяин и задача по расписанию (её текст поставил хозяин или
# агент в чистом ходе — тул schedule в заражённом ходе требует одобрения).
TRUSTED_ACTORS = ("owner", "schedule", "team", "mail")  # team — задача от самого агента, mail — строка, собранная кодом из безвредных полей (заражение — явно)


@dataclass
class Turn:
    session_id: str
    channel: str
    actor: str = "owner"
    unattended: bool = False
    tainted: bool = False
    steps: int = 0
    cost: float = 0.0
    started: float = field(default_factory=time.monotonic)
    state: dict = field(default_factory=dict)
    seen_calls: list = field(default_factory=list)
    inbox: list = field(default_factory=list)
    task: asyncio.Task | None = None
    stopped: bool = False
    hints: str | None = None  # «Вспомнилось» — считается один раз на ход, чтобы начало запроса не менялось между шагами
    first_id: str = ""         # id сообщения, с которого начался ход (для разбора после хода)
    # для субагентов: свой системный промпт, своя роль модели, урезанный набор тулов, свои лимиты
    system: str | None = None
    model_role: str = "main"
    tool_names: tuple | None = None
    limits: dict = field(default_factory=dict)
    context: object | None = None  # исполняющий контекст; API имеет свой потолок на тех же данных
    skill: dict | None = None  # текущий выбор хозяина; тело навыка не сохраняется в истории


def home_channel(agent_ctx, sid: str) -> str:
    """Куда отвечать хозяину; внутренний источник не меняет адрес разговора."""
    return (agent_ctx.store.session(sid) or {}).get("meta", {}).get("home_channel", "pult")


class Mind:
    def __init__(self, agent_ctx):
        self.app = agent_ctx
        self.turns: dict[str, Turn] = {}
        self.background: set[asyncio.Task] = set()  # сжатие и разбор после ходов
        self._after_lock = asyncio.Lock()

    def running(self, session_id: str) -> bool:
        t = self.turns.get(session_id)
        return bool(t and t.task and not t.task.done())

    def publish(self, session_id: str, event: dict) -> None:
        event = {"session": session_id, **event}
        turn=self.turns.get(session_id)
        if turn and turn.state.get('_telegram_route'):event['telegram_route']=dict(turn.state['_telegram_route'])
        self.app.bus.publish(event)

    async def say(self, session_id: str, text: str, *, channel: str = "pult", actor: str = "owner", content=None, unattended: bool = False,
                  wait: bool = True, tainted: bool | None = None, system: str | None = None, model_role: str = "main",
                  tool_names: tuple | None = None, limits: dict | None = None, label: str = "", agent_chain=None, agent_letter='', message_meta=None, attended: bool = False, effort: str | None = None) -> dict | None:
        """Сообщение в сессию. Идёт ход — вставится в него; нет — начнётся новый ход."""
        if isinstance(content,list) and any(isinstance(part,dict) and part.get('type') in ('image','audio') for part in content):tainted=True
        if getattr(self.app.host,'_draining',False):raise HumanError('closing','Бендер завершает работу','Дождись перезапуска или запусти его снова; новый ход не начат.',status=409)
        if getattr(self.app,'_importing',False):raise HumanError('import_busy','Агент сейчас переезжает','Дождись завершения переноса.')
        if actor=='owner' and (self.app.store.session(session_id) or {}).get('kind')=='correspondence':
            raise HumanError('correspondence_readonly','Переписка агентов — только для чтения','Напиши агенту в его главном или отдельном чате.',status=403)
        if self.app.agent_id != 'main':
            self.app.host.agents.get(self.app.agent_id)  # Архив и выключатель действуют и для уже загруженного контекста.
        t = self.turns.get(session_id)
        if t and t.task and not t.task.done() and t.context is not self.app and (getattr(self.app,'_api_channel',False) or getattr(t.context,'_api_channel',False)):
            raise HumanError('api_busy','Этот чат занят в другом канале','Дождись окончания хода или останови его в пульте; сообщения не смешиваются между потолками прав.',status=409)
        home = {"терминал":"terminal", "пульт":"pult"}.get(channel,channel)
        if actor == "owner" and home in ("telegram", "matrix", "userbot", "pult", "terminal"):
            if (self.app.store.session(session_id) or {}).get("meta", {}).get("home_channel") != home:
                self.app.store.update_session(session_id, meta={"home_channel": home})
        if actor == "owner" and home in ("telegram", "speaker") and getattr(self.app.host, '_handoffs', None):
            from ..agents.handoff import take
            text = take(self.app.host, self.app.agent_id, home) + text  # первая реплика после hand_over
        route=(message_meta or {}).get('telegram_route') if channel=='telegram' else None
        requested=(message_meta or {}).get('telegram_owner_requested') if channel=='telegram' and actor=='owner' else None
        if t and t.task and not t.task.done() and route:
            t.state['_telegram_route']=dict(route)
            t.state['_telegram_owner_requested']=requested
        if t and t.task and not t.task.done() and label:
            t.state["delivery_label"] = label
        if actor=='owner' and not tainted and home in ('pult','terminal','matrix') and text.strip().split(maxsplit=1)[0:1]==['/effort']:
            from ..router.effort import command
            parts=text.strip().split(maxsplit=1)
            line=await command(self.app,session_id,parts[1] if len(parts)>1 else '')
            self._save_event(session_id,'notice',line)
            return {'text':line,'error':None,'steps':0,'cost':0}
        if actor == "owner" and not tainted and text.strip().split(maxsplit=1)[0:1] == ["/убрать-навык"]:
            from ..skills.notices import archive
            from ..tools.registry import ToolContext
            parts=text.strip().split(maxsplit=1)
            if len(parts)!=2:
                raise HumanError("skill_name","Укажи имя навыка","/убрать-навык имя")
            line=await archive(self.app,parts[1].strip(),ToolContext(self.app,actor="owner",session_id=session_id))
            self._save_event(session_id,"notice",line)
            return {"text":line,"error":None,"steps":0,"cost":0}
        if actor == "owner" and not tainted and text.strip().split(maxsplit=1)[0:1] == ["/научись"]:
            if t and t.task and not t.task.done():
                raise HumanError("busy", "Сначала дождись завершения работы", "После хода повтори /научись.")
            from ..skills.experience import enabled, from_conversation

            if not enabled(self.app):
                raise HumanError("experience_off", "Навыки из опыта выключены", "Включи их в настройках Мастерской.")
            learned = await from_conversation(self.app, session_id, text.partition(" ")[2])
            line = "💾 Навык: " + learned["name"] + ("" if learned["enabled"] else " — черновик в карантине")
            self.app.store.add_message(session_id, {"role": "user", "content": text}, {"actor": actor, "channel": channel})
            self._save_event(session_id, "skill", line, learned)
            return {"text": line, "error": None, "steps": 0, "cost": 0}
        msg = {"role": "user", "content": content if content is not None else text}
        meta = {**(message_meta or {}), "channel": channel, "actor": actor}
        if actor == "owner" and not tainted and text.lstrip().startswith("/"):
            selected = self.app.skills.command(text)
            if selected:
                meta["skill"] = selected
        clean_letter = actor.startswith("agent:") and tainted is False  # письмо агента стаи не заражает ход
        if clean_letter:
            meta["clean_letter"] = True
        if (actor not in TRUSTED_ACTORS and not clean_letter) or tainted:
            meta["untrusted"] = True
        if t and t.task and not t.task.done():
            if agent_chain and len(agent_chain)>=len(t.state.get('agent_chain') or []):
                t.state.update(agent_chain=list(agent_chain),agent_letter=agent_letter)
            # идёт ход: сообщение ляжет в историю на ближайшей границе тула (между вызовом тула и его
            # результатом вставлять нельзя — провайдеры требуют, чтобы результат шёл сразу за вызовом)
            t.inbox.append((msg, meta))
            self.publish(session_id, {"type": "message", "message": {**msg, "meta": {**meta, "queued": True}}})
            self.publish(session_id, {"type": "steer", "text": text[:200]})
            return None
        if actor == "owner" and session_id == self.app.main_session():
            from . import compact

            why = compact.page_due(self.app, session_id)
            if why:
                compact.new_page(self.app, session_id, why)
        mid = self.app.store.add_message(session_id, msg, meta)
        self.publish(session_id, {"type": "message", "message": {**msg, "meta": meta}})
        # attended: письмо агента в цепочке, которую начал присутствующий хозяин, — карточки и ask_owner доходят до него
        unattended = (unattended or (system is None and actor != "owner")) and not attended
        turn = Turn(session_id, channel, actor, unattended, tainted=(actor not in TRUSTED_ACTORS) if tainted is None else tainted, first_id=mid,
                    system=system, model_role=model_role, tool_names=tuple(tool_names) if tool_names is not None else None, limits=dict(limits or {}), skill=meta.get("skill"),context=self.app)
        self.app.store.update_session(session_id,meta={"active_turn":{"first":mid,"channel":channel}})
        self.turns[session_id] = turn
        turn.state["delivery_label"] = label
        if effort: turn.state["_effort"] = effort  # усилие только этого хода (например, ответ в колонку)
        if route:turn.state['_telegram_route']=dict(route)
        if requested is not None:turn.state['_telegram_owner_requested']=requested
        if agent_chain:turn.state.update(agent_chain=list(agent_chain),agent_letter=agent_letter)
        self._launch(turn)
        if wait:
            async with self.app.host.agents.pause_background():return await turn.task
        return None

    def _launch(self, turn: Turn) -> None:
        session_id = turn.session_id
        turn.task = asyncio.ensure_future(self._run(turn))
        def release(_task):
            if self.turns.get(session_id) is turn:
                self.turns.pop(session_id, None)
        turn.task.add_done_callback(release)

    async def _resume(self, turn: Turn, after_id, meta: dict) -> None:
        """Слово хозяина пришло, когда ход уже заканчивался (лимит шагов, срока, петля): оно в истории — отвечаем новым ходом."""
        await asyncio.sleep(0)
        sid = turn.session_id
        if self.running(sid) or self.app.guard.halted or getattr(self.app.host, '_draining', False) or getattr(self.app, '_closed', False):
            return
        rows = self.app.store.q("SELECT id, meta FROM messages WHERE session_id=? AND id>? AND role='user' ORDER BY id", (sid, after_id or ''))
        if not rows:
            return
        tainted = any(json.loads(r['meta'] or '{}').get('untrusted') for r in rows)
        new = Turn(sid, meta.get('channel') or turn.channel, 'owner', False, tainted=tainted, first_id=rows[0]['id'], skill=meta.get('skill'), context=self.app)
        for key in ('delivery_label', '_telegram_route', '_telegram_owner_requested'):
            if key in turn.state:new.state[key] = turn.state[key]
        self.app.store.update_session(sid, meta={"active_turn": {"first": new.first_id, "channel": new.channel}})
        self.turns[sid] = new
        self._launch(new)

    def recover(self):
        count=0
        for row in self.app.store.q("SELECT id FROM sessions WHERE json_extract(meta,'$.active_turn') IS NOT NULL"):
            sid=row['id']
            self._save_event(sid,'interrupted','Прежний ход прерван перезапуском. Проверь результат перед повтором: действие могло успеть выполниться.')
            self.app.store.update_session(sid,meta={'active_turn':None})
            count+=1
        notices=(('backups.in_progress','Бекап прерван перезапуском. Проверь последнюю завершённую копию и запусти новый снимок.'),
                 ('updates.in_progress','Обновление прервано перезапуском. Проверь текущий слот в «Версиях и откате» перед повтором; автоматического повторения нет.'))
        for key,text in notices:
            marker = self.app.store.meta_get(key)
            if marker:
                if key == 'updates.in_progress' and isinstance(marker, dict) and marker.get('git'):
                    text = 'Обновление git прервано перезапуском. Проверь текущий коммит и bender doctor перед повтором; автоматического повторения нет.'
                self._save_event(self.app.main_session(),'interrupted',text)
                self.app.store.meta_set(key,None)
                count+=1
        if count:self.app.journal.write('system','После перезапуска отмечены прерванные ходы',data={'count':count})
        return count

    def stop(self, session_id: str | None = None) -> int:
        n = 0
        for sid, t in list(self.turns.items()):
            if session_id and sid != session_id:
                continue
            if t.task and not t.task.done():
                t.stopped = True
                t.task.cancel()
                n += 1
        if session_id is None:
            for task in list(self.background):
                task.cancel()
        return n

    async def _run(self, turn: Turn) -> dict:
        from ..agents.runtime import ACTIVE
        token=ACTIVE.set(self.app)
        try:return await self._run_turn(turn)
        finally:ACTIVE.reset(token)

    async def _run_turn(self, turn: Turn) -> dict:
        agent_ctx = self.app
        sid = turn.session_id
        self.publish(sid, {"type": "turn_start"})
        result = {"text": "", "error": None, "steps": 0, "cost": 0.0}
        slot = None
        try:
            if turn.unattended:
                lane = agent_ctx.host.agents.background()
                await lane.__aenter__()
                slot = lane
                from ..agents.manager import BACKGROUND
                turn.state['_background_lease']=BACKGROUND.get()
            seconds = self._limits(turn)[1]
            result = await asyncio.wait_for(self._loop(turn), seconds) if seconds else await self._loop(turn)
        except asyncio.TimeoutError:
            self._save_event(sid, 'limit', 'Достигнут срок хода. Останавливаюсь.')
            result['text'] = turn.state.get('last_text', '')
            result['error'] = {'code': 'turn_timeout', 'title': 'Достигнут срок хода'}
        except asyncio.CancelledError:
            self._save_event(sid, "stopped", "Ход остановлен.")
            result["error"] = {"code": "stopped", "title": "Ход остановлен"}
        except Halted as e:
            self._save_event(sid, "halt", e.title)
            result["error"] = e.to_dict()
        except HumanError as e:
            from .passport import clean
            error = clean(agent_ctx, e.to_dict())
            self._save_event(sid, "error", error['title'], error)
            result["error"] = error
        except Exception as e:  # noqa: BLE001
            err = unexpected(e, "ход агента")
            self._save_event(sid, "error", err.title, err.to_dict())
            result["error"] = err.to_dict()
        finally:
            if slot is not None: await slot.__aexit__(None, None, None)
            turn.state.pop('_background_lease',None)
            self._finish_pending(turn)
            self._flush_inbox(turn)
            pending = turn.state.pop('_owner_pending', None)  # слово хозяина, которое модель ещё не видела
            resume = (pending['after'], pending['meta']) if pending and not turn.stopped and turn.system is None and not agent_ctx.guard.halted else None
            agent_ctx.guard.end_turn(sid)
            result["cost"] = round(turn.cost, 6)
            result["steps"] = turn.steps
            if turn.state.get("used_skills"):
                from ..skills.experience import outcome

                outcome(agent_ctx.skills, turn.state, not bool(result.get("error") or turn.state.get("tool_failed")))
            result["tainted"] = turn.tainted
            result['telegram_route']=turn.state.get('_telegram_route')
            result['telegram_owner_requested']=turn.state.get('_telegram_owner_requested')
            result["sent_all"] = bool(turn.state.get("sent_all"))
            result["channel_sent"] = turn.state.get("channel_sent",[])[-3:]
            if turn.state.get('_denied') and turn.system is None:
                # Без хозяина отказ видит только модель; строка хозяину не даёт принять ход за выполненный.
                result['denied'] = turn.state['_denied'][:5]
                self._spawn(self._report_denied(turn, result['denied'], len(turn.state['_denied'])))
            last = agent_ctx.store.one("SELECT MAX(id) AS id FROM messages WHERE session_id=?", (sid,))
            agent_ctx.store.update_session(sid, meta={"active_turn":None, "last_turn": {"first": turn.first_id, "last": last["id"], "tainted": turn.tainted}})
            if turn.actor == "owner" and agent_ctx.settings.get("workshop", "enabled", True) and agent_ctx.settings.get("workshop", "metrics", True):
                error = result.get("error") or {}
                agent_ctx.journal.write("turn", "Ход хозяина: " + ("без ошибки" if not error and not turn.state.get("tool_failed") else "ошибка или отмена"), actor="owner",
                    data={"session": sid, "channel": turn.channel, "first": turn.first_id, "ok": not bool(error or turn.state.get("tool_failed")), "error": error.get("code"),
                          "tool_failed": bool(turn.state.get("tool_failed")), "cost": result.get("cost", 0), "tainted": turn.tainted, "skills": turn.state.get("used_skills", [])})
            self.publish(sid, {"type": "turn_end", "result": {k: v for k, v in result.items() if k != "text"}})
            session = agent_ctx.store.session(sid)
            if session and session["kind"] in ("main","chat","imported"):
                self.publish(sid, {"type": "answer", "channel": home_channel(agent_ctx, sid), "source": turn.channel,
                                   "label": turn.state.get("delivery_label", ""), "result": result})
            self._spawn(self._after(turn, result))
            if resume:
                asyncio.ensure_future(self._resume(turn, *resume))
        return result

    async def _report_denied(self, turn: Turn, denied: list, total: int) -> None:
        from .notices import home
        where = {'heart': 'по расписанию', 'schedule': 'по расписанию', 'hook': 'по вебхуку', 'mail': 'по письму'}.get(turn.actor, turn.channel)
        lines = '\n'.join('• ' + d['tool'] + ': ' + d['reason'] for d in denied) + (f'\n… и ещё {total - len(denied)}' if total > len(denied) else '')
        text = (f'⚠️ Ход без хозяина ({where}): Предохранитель не дал сделать —\n{lines}\n'
                'Эта часть не выполнена. Повтори в разговоре со мной или разреши правилом хозяина.')
        try:await home(self.app, text, 'denied')
        except Exception as error:  # noqa: BLE001 — уведомление не должно ронять фон
            self.app.journal.write('turn', 'Не удалось сообщить хозяину об отказах', data={'error': type(error).__name__})

    # после хода: сжатие, разбор, векторы — в фоне, ход их не ждёт

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.ensure_future(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    async def idle(self, timeout: float = 30.0) -> None:
        """Дождаться фоновой работы после ходов (для тестов, Полигона и выключения)."""
        deadline = time.monotonic() + timeout
        while self.background and time.monotonic() < deadline:
            await asyncio.wait(list(self.background), timeout=max(0.01, deadline - time.monotonic()))

    async def _after(self, turn: Turn, result: dict) -> None:
        if getattr(self.app,'_closed',False) or getattr(self.app.host,'_closed',False):return
        async with self.app.host.agents.background():
            if not getattr(self.app,'_closed',False) and not getattr(self.app.host,'_closed',False):await self._after_turn(turn,result)

    async def _after_turn(self, turn: Turn, result: dict) -> None:
        agent_ctx = self.app
        sid = turn.session_id
        async with self._after_lock:  # по одному: две модели одновременно в одну память не пишут
            if agent_ctx.guard.halted:
                return
            try:
                saved = None
                if reviewmod.should_review(agent_ctx, turn, result) and turn.first_id:
                    pending = reviewmod.queue(agent_ctx, sid, turn.first_id)
                    if len(pending) >= reviewmod.every(agent_ctx):
                        error = result.get("error") or {}
                        saved = await reviewmod.review(agent_ctx, sid, pending, outcome={"error": error.get("code"), "title": error.get("title"), "tool_failed": bool(turn.state.get("tool_failed"))})
                        reviewmod.done(agent_ctx, sid)
                    mode = agent_ctx.settings.get("agent", "memory_notices", "on")
                    if saved and mode != "off":
                        if saved.get("skill"):
                            skill = saved["skill"]
                            self._save_event(sid, "skill", "💾 Навык: " + skill["name"] + ("" if skill["enabled"] else " — черновик в карантине"), skill)
                        line = reviewmod.summary(saved, verbose=(mode == "verbose"))
                        if line:
                            self._save_event(sid, "memory", "💾 Запомнил: " + line, {"saved": saved})
            except Exception as e:  # noqa: BLE001 — разбор не обязателен; ошибка — в бортжурнал
                agent_ctx.journal.write("memory", f"Разбор после хода не удался: {str(getattr(e, 'title', '') or e)[:200]}", actor="review", data={"session": sid})
            try:
                budget = ctxmod.history_budget(agent_ctx, ctxmod.estimate_tokens(ctxmod.system_prompt(agent_ctx)))
                await compact.maybe_compact(agent_ctx, sid, budget)
            except Exception as e:  # noqa: BLE001
                agent_ctx.journal.write("memory", f"Сжатие разговора упало: {e}"[:300], actor="memory", data={"session": sid})
            if agent_ctx.vectors.available():
                await agent_ctx.vectors.fill()  # векторы для нового (в том числе только что запомненного)

    def observe(self,sid,text,*,channel='telegram',tainted=False,continue_turn=False,meta=None):
        """Служебный факт без нового хода; вставляется только между полными пакетами тулов."""
        msg={'role':'user','content':text}
        info={**(meta or {}),'service_event':True,'actor':'system','channel':channel,'untrusted':bool(tainted),'continue_turn':bool(continue_turn)}
        turn=self.turns.get(sid)
        if turn and turn.task and not turn.task.done():
            turn.inbox.append((msg,info))
            if tainted:turn.tainted=True
        else:self.app.store.add_message(sid,msg,info)
        self.publish(sid,{'type':'message','message':{**msg,'meta':info}})

    def _flush_inbox(self, turn: Turn) -> bool:
        if not turn.inbox:
            return False
        work=False
        for msg, meta in turn.inbox:
            work=work or not meta.get('service_event') or bool(meta.get('continue_turn'))
            if meta.get("untrusted") or (not meta.get('service_event') and not meta.get('clean_letter') and meta.get("actor") not in TRUSTED_ACTORS):
                turn.tainted = True
            if meta.get("skill"):
                turn.skill = meta["skill"]
            if meta.get('actor') == 'owner' and not meta.get('service_event'):
                pending = turn.state.setdefault('_owner_pending', {'after': (self.app.store.one("SELECT MAX(id) AS id FROM messages WHERE session_id=?", (turn.session_id,)) or {})['id']})
                pending['meta'] = meta
            self.app.store.add_message(turn.session_id, msg, meta)
        turn.inbox.clear()
        if work:
            turn.seen_calls.clear()
            turn.state.pop('loop_detected', None)
        return work

    def _save_event(self, sid: str, kind: str, text: str, data: dict | None = None) -> None:
        msg = {"role": "event", "content": text, "kind": kind}
        if data:
            msg["data"] = data
        self.app.store.add_message(sid, msg)
        self.publish(sid, {"type": "message", "message": msg})

    def _finish_pending(self, turn):
        """Стоп/срок посреди тула закрывает оставшиеся пары перед новым user."""
        agent_ctx, sid = self.app, turn.session_id
        row = agent_ctx.store.one("SELECT id, data FROM messages WHERE session_id=? AND id>=? AND role='assistant' ORDER BY id DESC LIMIT 1", (sid, turn.first_id))
        if not row: return
        calls = json.loads(row['data']).get('tool_calls') or []
        if not calls: return
        done = {json.loads(r['data']).get('tool_call_id') for r in agent_ctx.store.q("SELECT data FROM messages WHERE session_id=? AND id>? AND role='tool'", (sid, row['id']))}
        for call in calls:
            if call.get('id') in done: continue
            msg = {'role':'tool', 'tool_call_id':call.get('id'), 'name':call.get('name'), 'content':'Вызов прерван остановкой или сроком хода.', 'is_error':True}
            agent_ctx.store.add_message(sid, msg, {'ok':False, 'summary':'прервано'})
            self.publish(sid, {'type':'tool_end', 'id':call.get('id'), 'name':call.get('name'), 'ok':False, 'summary':'прервано'})

    def _limits(self, turn):
        s = self.app.settings.group('agent')
        unattended = turn.unattended and turn.system is None
        steps = s['unattended_max_steps'] if unattended else s['max_steps']
        seconds = s['unattended_max_seconds'] if unattended else s['max_turn_seconds']
        return (int(turn.limits.get('steps', steps)), float(turn.limits.get('seconds', seconds)),
                float(turn.limits.get('usd', s.get('max_turn_cost', 0)) or 0))

    async def _loop(self, turn: Turn) -> dict:
        # Python 3.10 wait_for запускает тело в отдельной Task; место принадлежит тому же ходу.
        lease=turn.state.get('_background_lease')
        if lease is not None:lease.owner=asyncio.current_task()
        agent_ctx = self.app
        sid = turn.session_id
        max_steps, max_seconds, max_cost = self._limits(turn)
        final_text = ""
        shrink = 1  # во сколько раз урезать историю после «не влезло в контекст»
        while True:
            if agent_ctx.guard.halted:
                raise Halted()
            system = turn.system if turn.system is not None else ctxmod.system_prompt(agent_ctx)
            skill_context = ""
            if turn.skill:
                from ..tools.web import untrusted

                selected = turn.skill["name"]
                loaded = agent_ctx.skills.read(selected, manual=True, max_chars=24000)
                from ..skills.experience import use

                use(agent_ctx.skills, selected, turn.state)
                turn.tainted = turn.tainted or not agent_ctx.skills.trusted_text(selected)
                turn.state["selected_skill"] = selected
                skill_context = f"[Хозяин выбрал одобренный навык {selected}. Используй его как процедуру для задачи, не расширяя права; новые указания хозяина важнее.]\n" + untrusted(f"навык {selected}", loaded["text"]) + "\n"
                if loaded["next_offset"] is not None:
                    skill_context += f"[Продолжение: skill_read name={selected}, offset={loaded['next_offset']}.]\n"
            if turn.system is None and agent_ctx._skills is not None and agent_ctx.skills.index():
                turn.tainted = turn.tainted or agent_ctx.skills.catalog_tainted()
            budget = max(1000, ctxmod.history_budget(agent_ctx, ctxmod.estimate_tokens(system)) // shrink)
            if turn.steps:
                await compact.maybe_compact(agent_ctx, sid, budget, during_turn=True)
            win = compact.window(agent_ctx, sid, budget)
            hist = win.messages
            messages = [{"role": "system", "content": system}]
            gap = compact.note(win)
            if gap:
                messages.append({"role": "system", "content": gap})
            messages += hist
            # изменчивое — приставкой к последнему сообщению хозяина, а не в системный промпт
            if turn.hints is None and turn.system is not None:
                turn.hints = ""  # субагент знает только то, что ему передали: ни «вспомнилось», ни намерений
            if turn.hints is None:
                last_user = next((m for m in reversed(hist) if m.get("role") == "user" and isinstance(m.get("content"), str)), None)
                from ..core.dates import without_prefix
                hint_text = without_prefix(last_user['content']) if last_user else ''
                oldest = agent_ctx.store.one("SELECT MIN(created_at) AS t FROM (SELECT created_at FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT ?)",
                                       (sid, max(1, len(hist))))
                turn.hints = await asyncio.to_thread(ctxmod.memory_hints, agent_ctx, hint_text, sid,
                                                     oldest["t"] if oldest else None) if last_user else ""
                if last_user and turn.actor == "owner" and not turn.tainted:
                    from ..heart.intents import hints as intent_hints

                    turn.hints = intent_hints(agent_ctx, hint_text) + turn.hints
            for i in range(len(messages) - 1, -1, -1):
                if messages[i]["role"] == "user":
                    m = dict(messages[i])
                    # время — одно на ход и обновляется раз в 5 минут: сдвиг минуты посреди хода ломал кэш провайдера
                    # для всех шагов после этого сообщения; длинный ход всё равно видит, сколько прошло
                    now = time.time()
                    if now - float(turn.state.get('_note_at') or 0) >= ctxmod.NOTE_REFRESH:
                        turn.state['_note_at'] = now
                    note = skill_context + ctxmod.runtime_note(agent_ctx, turn.channel, turn.tainted, turn.state['_note_at']) + (turn.hints or "")
                    turn.state['_note'] = note  # CLI-движку (Claude Code): он берёт реплики из базы, без этой приставки
                    if isinstance(m.get("content"), str):
                        m["content"] = note + m["content"]
                    messages[i] = m
                    break
            tools = agent_ctx.tools.specs()
            if turn.tool_names is not None:
                tools = [t for t in tools if t.name in turn.tool_names]
            if any(agent_ctx.tools.tools[t.name].external for t in tools):
                # Описание/схема внешнего тула тоже могут содержать инъекцию, ещё до вызова.
                turn.tainted = True
            stream = {"text": ""}

            def on_delta(kind: str, text: str, _s=stream):
                if kind == "text":
                    _s["text"] += text
                self.publish(sid, {"type": "delta", "kind": kind, "text": text})

            def on_status(st: dict):
                if st.get('type') == 'provider_error':
                    from .passport import clean
                    st = clean(agent_ctx, {**st, 'step': turn.steps + 1})
                    self._save_event(sid, 'model_error', f"Шаг {st['step']}: {st['provider']}/{st['model']}, HTTP {st['http']}: {st['body'][:250]}", st)
                self.publish(sid, {**st, "type": "router", "kind": st.get("type")})
                if st.get("type") == "tool_protocol":
                    # запомнить насовсем: в следующий раз сразу текстом, без пробного отказа
                    prov = agent_ctx.models.providers().get(st.get("provider_id") or "")
                    if prov is not None and st.get("model") not in (prov.get("text_tools") or []):
                        agent_ctx.models.update_provider(prov["id"], {"text_tools": list(prov.get("text_tools") or []) + [st.get("model")]})
                    self._save_event(sid, "notice", f"{st.get('candidate')}: модель не умеет тулы через API — дальше даю их текстом.")

            # CLI-движку (Claude Code) — ход, к которому привязать тулы MCP. Ход идёт в своей задаче asyncio,
            # поэтому значение живёт только в её контексте и сбрасывать его не нужно.
            clipkg.TURN.set(turn)
            from . import media as model_media
            try:
                turn.state['_doing'] = {'what': 'ждёт ответа модели', 'since': time.monotonic()}  # чёрный ящик Telegram
                res = await agent_ctx.router.call(messages, role=turn.model_role, tools=tools or None, on_delta=on_delta, on_status=on_status,
                    params={'effort': turn.state['_effort']} if turn.state.get('_effort') else None,
                    prepare=lambda cand,source:model_media.prepare(agent_ctx,turn,cand,source),reject_fallback=turn.system is not None,
                    on_media_error=lambda cand,features,error:model_media.remember(agent_ctx,cand,features,error))
            except ProviderError as e:
                if e.klass == "context" and shrink < 8 and not stream["text"] and not (e.partial or {}).get("text"):
                    shrink *= 2  # не влезло — урезать историю и повторить шаг
                    self._save_event(sid, "notice", f"История не влезла в контекст модели — беру вдвое меньше (≈{budget // 2} токенов).")
                    continue
                partial = (e.partial or {}).get("text") or stream["text"]
                from .passport import clean
                call_id = agent_ctx.store.add_call(session_id=sid, role=turn.model_role, provider=e.provider, model=e.model, usage={}, cost=0.0, seconds=0.0, notice=None,
                                             request={"messages": messages, "tools": [t.name for t in tools]}, error=clean(agent_ctx, e.to_dict()))
                if partial:
                    agent_ctx.store.add_message(sid, {"role": "assistant", "content": partial}, {"partial": True, "error": e.klass, "call": call_id})
                    self.publish(sid, {"type": "message", "message": {"role": "assistant", "content": partial, "meta": {"partial": True}}})
                raise
            turn.steps += 1
            turn.cost += res.cost
            call_id = agent_ctx.store.add_call(session_id=sid, role=turn.model_role, provider=res.provider_name, model=res.model, usage=res.usage.to_dict(), cost=res.cost,
                                         seconds=res.seconds, notice=res.notice, request={"messages": messages, "tools": [t.name for t in tools]})
            if res.notice:
                self._save_event(sid, "notice", res.notice)
            amsg = res.assistant_message()
            meta = {"model": res.model, "provider": res.provider_name, "cost": round(res.cost, 6), "usage": res.usage.to_dict(), "finish": res.finish,
                    "call": call_id, "seconds": round(res.seconds, 2), "tainted": turn.tainted,'channel':turn.channel}
            if turn.state.get('_telegram_route'):meta['telegram_route']=turn.state['_telegram_route']
            if turn.state.get('_telegram_owner_requested') is not None:meta['telegram_owner_requested']=turn.state['_telegram_owner_requested']
            agent_ctx.store.add_message(sid, amsg, meta)
            turn.state.pop('_owner_pending', None)  # всё, что было в истории до запроса, модель видела
            self.publish(sid, {"type": "message", "message": {**amsg, "meta": meta}})
            final_text = res.text or final_text
            turn.state["last_text"] = final_text
            if not res.tool_calls:
                final_text = res.text or ""
                if self._flush_inbox(turn):
                    continue  # пришло новое сообщение, пока модель отвечала — продолжаем ход с ним
                break
            # тулы
            media_parts=[]
            for call in res.tool_calls:
                if agent_ctx.guard.halted:
                    raise Halted()
                key = (call.name, json.dumps(call.arguments, sort_keys=True, ensure_ascii=False))
                if turn.state.get('loop_detected'):
                    out_text, ok, summary, tainted, data = ('Вызов отменён: обнаружена петля в этом пакете.', False, 'петля', False, {})
                elif turn.tool_names is not None and call.name not in turn.tool_names:
                    out_text, ok, summary, tainted, data = (f"Ошибка: тул «{call.name}» тебе не выдан. Доступны: {', '.join(turn.tool_names) or 'никакие'}.", False, "тул не выдан", False, {})
                elif call.bad_json:
                    out_text, ok, summary, tainted, data = (f"Ошибка: аргументы не разобрались как JSON: {call.raw_arguments[:300]}", False, "сломанные аргументы", False, {})
                else:
                    self.publish(sid, {"type": "tool_start", "id": call.id, "name": call.name, "arguments": call.arguments})
                    tctx = ToolContext(app=agent_ctx, session_id=sid, actor="agent" if turn.actor in TRUSTED_ACTORS else turn.actor, tainted=turn.tainted,
                                       unattended=turn.unattended, turn=turn.state,
                                       progress=lambda s, _id=call.id: self.publish(sid, {"type": "tool_progress", "id": _id, "text": s}))
                    turn.state['_doing'] = {'what': 'выполняет тул ' + call.name, 'since': time.monotonic()}
                    r = await agent_ctx.tools.run(call.name, call.arguments, tctx)
                    out_text, ok, summary, tainted, data = r.content, r.ok, r.summary, r.tainted, r.data
                    if turn.unattended and (data or {}).get('denied'):
                        turn.state.setdefault('_denied', []).append({'tool': call.name, **data['denied']})
                    if ok and r.model_parts:
                        # read_file, MCP и любой тул: вложения идут модели отдельным сообщением после пакета
                        media_parts.extend(r.model_parts)
                    if ok and call.name in ('read_file','look'):
                        turn.cost+=data.get('aux_cost',0)
                digest = hashlib.sha256(json.dumps([key, ok, out_text], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
                turn.seen_calls.append(digest)
                del turn.seen_calls[:-12]
                for period in (1, 2):
                    n = period * 3
                    recent = turn.seen_calls[-n:]
                    if len(recent) == n and recent[:period] == recent[period:2*period] == recent[2*period:]:
                        turn.state['loop_detected'] = True
                if tainted and not turn.tainted:
                    turn.tainted = True
                tmsg = {"role": "tool", "tool_call_id": call.id, "name": call.name, "content": out_text}
                if not ok:
                    turn.state["tool_failed"] = True
                    tmsg["is_error"] = True
                tmeta = {"ok": ok, "summary": summary, "tainted": tainted, "data": {k: v for k, v in (data or {}).items() if k != "checkpoint"} if data else {}}
                agent_ctx.store.add_message(sid, tmsg, tmeta)
                if call.name == "learn_skill" and ok and data.get("skill"):
                    skill = data["skill"]
                    self._save_event(sid, "skill", "💾 Навык: " + skill["name"] + ("" if skill["enabled"] else " — черновик в карантине"), skill)
                self.publish(sid, {"type": "tool_end", "id": call.id, "name": call.name, "ok": ok, "summary": summary, "tainted": tainted,
                                   "content": out_text[:4000]})
            if media_parts:
                # Нельзя разрывать пакет assistant/tool сообщением user, даже при нескольких тулах с вложениями.
                mmsg={'role':'user','content':media_parts}
                sources=sorted({p.get('source') for p in media_parts if p.get('source')})
                mmeta={'untrusted':True,'tool_media':True,'source':sources[0] if len(sources)==1 else 'tools','actor':'system'}
                agent_ctx.store.add_message(sid,mmsg,mmeta)
                self.publish(sid,{'type':'message','message':{**mmsg,'meta':mmeta}})
            self._flush_inbox(turn)
            if turn.state.get('loop_detected'):
                self._save_event(sid, 'limit', 'Петля: одинаковые вызовы с одинаковыми аргументами и результатами повторились трижды. Останавливаюсь.')
                break
            # лимиты
            if max_steps and turn.steps >= max_steps:
                self._save_event(sid, "limit", f"Достигнут лимит шагов ({max_steps}). Останавливаюсь — скажи, продолжать ли.")
                break
            if max_seconds and time.monotonic() - turn.started > max_seconds:
                self._save_event(sid, "limit", f"Ход идёт дольше {int(max_seconds)} с. Останавливаюсь.")
                break
            if max_cost and turn.cost > max_cost:
                self._save_event(sid, "limit", f"Ход стоил больше ${max_cost:.2f}. Останавливаюсь — скажи, продолжать ли.")
                break
            self._flush_inbox(turn)  # сообщения хозяина, пришедшие во время тулов, — в историю перед следующим шагом
        return {"text": final_text, "error": {"code":"loop", "title":"Обнаружена петля одинаковых вызовов"} if turn.state.get("loop_detected") else None, "steps": turn.steps, "cost": round(turn.cost, 6), "tainted": turn.tainted}
