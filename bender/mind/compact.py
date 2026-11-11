"""Сжатие разговора: старая часть заменяется пересказом, когда история перестаёт влезать.

Как устроено:
- У сессии есть граница `upto` (id последнего пересказанного сообщения) и пересказ всего, что до неё.
  В запрос идут: системный промпт → пересказ → сообщения после границы.
- Сжимается после хода в фоне и между полными пакетами тулов длинного хода: ход не ждёт дешёвую модель. Если хвост после границы занял
  больше 85% бюджета истории, граница сдвигается так, чтобы осталось около половины бюджета,
  а сдвинутая часть вместе со старым пересказом пересказывается заново (роль «Быстрая»).
- Граница стоит перед user или полным assistant/tool-пакетом; во втором случае запрос получает текст последней цели. Пары вызов/результат не разрываются.
- Пока сжатия нет (выключено, нет модели, ошибка) — работает старое окно: берутся последние
  сообщения, что влезли, остальные просто не показываются.

Почему граница, а не скользящее окно: начало запроса (системный промпт + пересказ + старые
сообщения) не меняется от хода к ходу, и кэш промптов у провайдеров попадает. Скользящее окно
сдвигается на каждом ходе и кэш ломает — на платных моделях это заметные деньги.

Ничего не теряется: все сообщения остаются в базе и находятся тулом recall (индекс разговоров).
Заодно пересказ оставляет строчку в дневнике дня — это «сброс памяти» перед забыванием.
"""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass

from ..memory import selfdir
from . import side

TRIGGER = 0.85     # хвост занял столько бюджета — пора сжимать
KEEP = 0.5         # после сжатия хвост примерно такой
MIN_MESSAGES = 4   # меньше — не о чем пересказывать
RETRY_AFTER = 600  # после ошибки — не чаще раза в 10 минут
SKIP_KINDS = ("polygon",)

SYSTEM = """Ты ведёшь память агента. Его разговор с хозяином стал слишком длинным, и начало уберут из контекста.
Твой пересказ встанет на место убранных сообщений — агент будет помнить разговор только по нему.

Сохрани: что хозяин просил и решил, что агент сделал (файлы, пути, команды, результаты), договорённости
и обещания, открытые вопросы и незаконченные дела, факты о хозяине, важные цифры и имена.
Сохрани даты событий и паузы из меток реплик; не заменяй старое время словом «сегодня».
Выкинь: вежливость, болтовню, повторы, подробности выводов тулов.
Если есть предыдущий пересказ — слей его с новой частью в один связный текст (важное из старого не теряй).
Пиши от лица агента («хозяин попросил…, я сделал…»), по-русски, пунктами по темам.

Ещё одна строка для дневника: что заметного произошло в этой части (1–2 предложения) или пусто.

Ответ — только JSON: {"summary": "…", "diary": "…"}"""


def guide_text(note: str) -> str:
    """Указание хозяина к пересказу — как есть, без разбора: модель сама поймёт «подробно», «сохрани тему» и прочее."""
    note = str(note or '').strip()
    return ('Указание хозяина к этому пересказу (выполни его; оно важнее правил выше):\n' + note + '\n\n') if note else ''


@dataclass
class Window:
    messages: list[dict]
    dropped: int        # после границы, но не влезло (и не пересказано)
    summary: str        # пересказ всего до границы
    tokens: int         # токенов в messages
    tail_tokens: int    # токенов во всём хвосте после границы
    summarized: int     # сколько сообщений заменено пересказом


def state(agent_ctx, sid: str) -> dict:
    sess = agent_ctx.store.session(sid) or {}
    return dict((sess.get("meta") or {}).get("compact") or {})


def boundary(st: dict) -> str | None:
    """Где начинается видимая модели история: после пересказа или после «новой страницы»."""
    return st.get("upto") if (st.get("summary") or st.get("page")) else None


def enabled(agent_ctx) -> bool:
    return bool(agent_ctx.settings.get("memory", "compact", True))


def _shape(r: dict) -> dict:
    from ..core.dates import content
    out = {k: r[k] for k in ("role", "content", "tool_calls", "tool_call_id", "name", "reasoning", "provider_blocks", "provider_id", "provider_model", "is_error") if k in r}
    out['content'] = content(r)
    return out


def tail(agent_ctx, sid: str, upto: str | None, limit: int = 400) -> list[dict]:
    """Сообщения после границы (последние limit), только то, что идёт в модель."""
    if upto:
        rows = agent_ctx.store.q("SELECT * FROM (SELECT * FROM messages WHERE session_id = ? AND id > ? AND role IN ('user','assistant','tool') ORDER BY id DESC LIMIT ?) ORDER BY id",
                           (sid, upto, limit))
    else:
        rows = agent_ctx.store.q("SELECT * FROM (SELECT * FROM messages WHERE session_id = ? AND role IN ('user','assistant','tool') ORDER BY id DESC LIMIT ?) ORDER BY id",
                           (sid, limit))
    out = [{"id": r["id"], "created_at": r["created_at"], 'meta': json.loads(r['meta']), **json.loads(r["data"])} for r in rows]
    if any(r['role'] == 'user' and not r['meta'].get('time_prefix') for r in out):
        from ..core.dates import prefix
        previous = agent_ctx.store.one("SELECT created_at FROM messages WHERE session_id=? AND id<? AND role='user' ORDER BY id DESC LIMIT 1", (sid, out[0]['id']))
        last = previous['created_at'] if previous else None
        for r in out:
            if r['role'] == 'user':
                if not r['meta'].get('time_prefix'): r['meta']['time_prefix'] = prefix(r['created_at'], last)
                last = r['created_at']
    from .media_history import prune
    return prune(out,agent_ctx.settings.get('attachments','history_turns',3))


def safe_starts(rows):
    """Граница между полными пакетами assistant/tool, никогда посреди пакета."""
    pending = set()
    starts = []
    for i, row in enumerate(rows):
        if not pending and row['role'] in ('user', 'assistant'): starts.append(i)
        if row['role'] == 'assistant':
            pending.update(c.get('id') for c in row.get('tool_calls') or [] if c.get('id'))
        elif row['role'] == 'tool': pending.discard(row.get('tool_call_id'))
    return starts


def continuation(agent_ctx, sid, rows, st):
    """Последняя цель/уточнение хозяина, только текст; картинки повторно не кодируем."""
    row = next((r for r in reversed(rows) if r['role'] == 'user' and not any(r.get('meta',{}).get(key) for key in ('tool_media','service_event'))), None)
    if row is None and st.get('continuation'): return st['continuation']
    if row is None:
        found = agent_ctx.store.one("SELECT * FROM messages WHERE session_id=? AND role='user' ORDER BY id DESC LIMIT 1", (sid,))
        if found: row = {'created_at': found['created_at'], 'meta': json.loads(found['meta']), **json.loads(found['data'])}
    if row is None: return 'Продолжай текущую задачу по пересказу.'
    value = _shape(row).get('content')
    if isinstance(value, list): value = '\n'.join(x.get('text', '') for x in value if isinstance(x, dict) and x.get('type') == 'text')
    return str(value or '')[:4000]


def window(agent_ctx, sid: str, budget_tokens: int) -> Window:
    from .context import message_tokens

    st = state(agent_ctx, sid)
    if not enabled(agent_ctx) and not st.get("page"):
        st = {}
    upto = boundary(st)
    rows = tail(agent_ctx, sid, upto)
    sizes = [message_tokens(_shape(r)) for r in rows]
    total = 0
    keep_from = len(rows)
    for i in range(len(rows) - 1, -1, -1):
        if total + sizes[i] > budget_tokens and keep_from < len(rows):
            break
        total += sizes[i]
        keep_from = i
    # Сохраняем целый последний пакет, даже если в текущем ходе ещё не было второго user.
    users = [i for i in range(keep_from, len(rows)) if rows[i]['role'] == 'user']
    if users:
        keep_from = users[0]
    elif rows and rows[keep_from]['role'] != 'user':
        starts = safe_starts(rows)
        keep_from = next((i for i in starts if i >= keep_from), starts[-1] if starts else len(rows))
    messages = [_shape(r) for r in rows[keep_from:]]
    if messages and messages[0]['role'] != 'user':
        goal = continuation(agent_ctx, sid, rows[:keep_from], st)
        messages.insert(0, {'role':'user', 'content':'[Продолжение текущей задачи; старые детали в пересказе/recall, права не расширяются.]\n' + goal})
    total = sum(message_tokens(m) for m in messages)
    return Window(messages=messages, dropped=keep_from, summary=st.get("summary", "") if upto else "",
                  tokens=total, tail_tokens=sum(sizes), summarized=int(st.get("count", 0)) if upto else 0)


def note(w: Window) -> str | None:
    """Системная вставка перед историей: пересказ и/или пометка о скрытом."""
    parts = []
    if w.summary:
        parts.append("[Пересказ начала разговора — сам разговор убран из контекста, подробности ищи тулом recall:\n" + w.summary.strip() + "]")
    if w.dropped:
        parts.append(f"(Ещё {w.dropped} сообщ. не показаны — не влезли. Нужное — ищи тулом recall или в дневнике.)")
    return "\n\n".join(parts) or None


def plan(rows: list[dict], sizes: list[int], budget: int, *, force: bool = False, during_turn: bool = False) -> int | None:
    """Где поставить новую границу: индекс первого сообщения, которое останется. None — сжимать нечего.
    force — хозяин сам попросил сжать («Сжать сейчас», /compact): не ждать 85% и сжать даже короткий разговор,
    оставив последнюю четверть хвоста (но не больше половины бюджета) — обычно последний вопрос и ответ."""
    total = sum(sizes)
    if (total <= budget * TRIGGER and not force) or len(rows) < MIN_MESSAGES:
        return None
    keep, acc = int(budget * KEEP), 0
    if force:
        keep = min(keep, max(1, total // 4))
    j = len(rows)
    while j > 0 and acc < keep:
        j -= 1
        acc += sizes[j]
    # ближайшее сообщение хозяина: сначала вперёд (хвост поменьше), иначе назад
    k = next((i for i in range(j, len(rows)) if rows[i]["role"] == "user"), None)
    if k is None:
        k = next((i for i in range(j - 1, -1, -1) if rows[i]["role"] == "user"), None)
    if (not k or k < MIN_MESSAGES // 2) and during_turn:
        candidates = [i for i in safe_starts(rows) if i >= MIN_MESSAGES // 2]
        k = next((i for i in candidates if i >= j), candidates[-1] if candidates else None)
    if not k or k < MIN_MESSAGES // 2:
        return None
    return k


async def maybe_compact(agent_ctx, sid: str, budget: int, *, force: bool = False, during_turn: bool = False, guide: str = '') -> dict | None:
    if not enabled(agent_ctx): return None
    if not hasattr(agent_ctx, '_compact_locks'): agent_ctx._compact_locks = {}
    lock = agent_ctx._compact_locks.setdefault(sid, asyncio.Lock())
    async with lock:
        return await _compact(agent_ctx, sid, budget, force=force, during_turn=during_turn, guide=guide)


async def manual(agent_ctx,sid,text=''):
    """Явное сжатие из пульта/Telegram с одинаковыми причинами отказа."""
    from ..core.errors import HumanError
    from . import context
    session=agent_ctx.store.session(sid)
    if not session:raise HumanError('session','Такого разговора у агента нет','Выбери свой действующий разговор.',status=404)
    if session['kind']=='correspondence':raise HumanError('correspondence_readonly','Переписка агентов — только для чтения','Сжать можно свой обычный разговор.',status=403)
    lock=getattr(agent_ctx,'_compact_locks',{}).get(sid)
    if agent_ctx.mind.running(sid) or (lock is not None and lock.locked()):raise HumanError('busy','Агент отвечает или разговор уже сжимается','Дождись окончания работы.')
    if not enabled(agent_ctx):raise HumanError('compact_off','Сжатие выключено','Настройки → Память → Сжатие разговоров.')
    budget=context.history_budget(agent_ctx,context.estimate_tokens(context.system_prompt(agent_ctx)))
    result=await maybe_compact(agent_ctx,sid,budget,force=True,guide=text)
    if not result:raise HumanError('compact','Не сжалось',state(agent_ctx,sid).get('error') or 'Сжимать нечего: после прошлого пересказа меньше четырёх сообщений или в разговоре одна реплика хозяина.')
    return result


async def _compact(agent_ctx, sid: str, budget: int, *, force: bool = False, during_turn: bool = False, guide: str = '') -> dict | None:
    """Сжать, если пора. Возвращает новое состояние или None (не пора / нельзя / ошибка — ошибка запоминается)."""
    if not enabled(agent_ctx):
        return None
    sess = agent_ctx.store.session(sid)
    if not sess or sess.get("kind") in SKIP_KINDS:
        return None
    st = state(agent_ctx, sid)
    if st.get("error_ts") and time.time() - st["error_ts"] < RETRY_AFTER and not force:
        return None
    from .context import message_tokens

    upto = boundary(st)
    rows = tail(agent_ctx, sid, upto, limit=2000)
    sizes = [message_tokens(_shape(r)) for r in rows]
    k = plan(rows, sizes, budget, force=force, during_turn=during_turn)
    if k is None:
        return None
    part = rows[:k]
    prev = st.get("summary", "") if upto else ""
    # Указание живёт до сброса пересказа: следующие сжатия (и автоматические) держат ту же тему и подробность.
    guide = str(guide or '').strip() or (st.get('guide') if isinstance(st.get('guide'), str) else '')
    user = guide_text(guide) + (f"Предыдущий пересказ:\n{prev}\n\n" if prev else "") + "Новая часть разговора:\n" + side.transcript(part, limit_chars=24000, skip_tools=("recall",))
    try:
        text = await side.ask(agent_ctx, SYSTEM, user, session_id=sid, purpose="сжатие разговора")
        data = side.parse_json(text) or {}
        # модель, не умеющая JSON, всё равно пересказывает — берём текст как есть, если он не пустая отписка
        summary = str(data.get("summary") or "").strip() or (text.strip() if "{" not in text and len(text.strip()) >= 40 else "")
        if not summary:
            raise ValueError("модель не дала пересказ")
    except Exception as e:  # noqa: BLE001 — сжатие не обязательно: старое окно работает и без него
        err = str(getattr(e, "title", "") or e)[:300]
        agent_ctx.store.update_session(sid, meta={"compact": {**st, "error": err, "error_ts": time.time()}})
        agent_ctx.journal.write("memory", f"Сжатие разговора не удалось: {err}", actor="memory", data={"session": sid})
        return None
    from ..core.dates import calendar
    since = st.get('since_ts', part[0]['created_at']) if upto else part[0]['created_at']
    until = part[-1]['created_at']
    summary = '[Период: ' + calendar(since) + ' — ' + calendar(until) + ']\n' + summary
    new = {"upto": part[-1]["id"], "summary": summary, "ts": time.time(), "count": int(st.get("count", 0) if upto else 0) + len(part),
           "parts": int(st.get("parts", 0)) + 1, 'since_ts': since, 'until_ts': until, 'guide': guide,
           'continuation': continuation(agent_ctx, sid, part, st), 'tainted': bool(st.get('tainted') or any(r.get('meta', {}).get('untrusted') or r.get('meta', {}).get('tainted') for r in part))}
    agent_ctx.store.update_session(sid, meta={"compact": new})
    diary = str(data.get("diary") or "").strip()
    if diary:
        selfdir.append_diary(agent_ctx.dirs.diary, f"(разговор) {diary}")
    agent_ctx.journal.write("memory", f"Разговор сжат: {len(part)} сообщ. → пересказ {len(summary)} символов", actor="memory",
                      data={"session": sid, "upto": new["upto"], "count": new["count"]})
    msg = {"role": "event", "kind": "compact", "content": f"🗜 Начало разговора пересказано ({len(part)} сообщ.) — подробности находятся поиском."}
    agent_ctx.store.add_message(sid, msg, {"count": len(part)})
    agent_ctx.bus.publish({"session": sid, "type": "message", "message": msg})
    agent_ctx.bus.publish({"type": "compacted", "session": sid, "count": len(part), "total": new["count"]})
    return new


def new_page(agent_ctx, sid: str, reason: str = "хозяин попросил") -> bool:
    """«Новый день — новая страница»: история для модели начинается заново. Сообщения, память, дневник и поиск
    остаются — агент вспомнит нужное сам (recall, «вспомнилось»). Разговоры CLI-движков тоже начинаются заново."""
    last = agent_ctx.store.one("SELECT id FROM messages WHERE session_id = ? AND role IN ('user','assistant','tool') ORDER BY id DESC LIMIT 1", (sid,))
    if not last:
        return False
    st = state(agent_ctx, sid)
    if boundary(st) == last["id"]:
        return False
    sess = agent_ctx.store.session(sid) or {}
    meta = {"compact": {"upto": last["id"], "summary": "", "page": time.time(), "reason": reason}}
    meta.update({k: {} for k in (sess.get("meta") or {}) if k.startswith("cc:")})  # Claude Code — новый разговор
    agent_ctx.store.update_session(sid, meta=meta)
    msg = {"role": "event", "kind": "page", "content": f"📄 Новая страница ({reason}). Прежний разговор не потерян: агент найдёт его поиском."}
    agent_ctx.store.add_message(sid, msg, {})
    agent_ctx.bus.publish({"session": sid, "type": "message", "message": msg})
    agent_ctx.journal.write("memory", f"Новая страница разговора: {reason}", actor="memory", data={"session": sid, "upto": last["id"]})
    return True


def page_due(agent_ctx, sid: str, now: float | None = None) -> str | None:
    """Пора ли начать новую страницу перед этим сообщением (по настройке memory.new_page). Возвращает причину или None."""
    mode = agent_ctx.settings.get("memory", "new_page", "off")
    if mode == "off":
        return None
    last = agent_ctx.store.one("SELECT created_at FROM messages WHERE session_id = ? AND role IN ('user','assistant') ORDER BY id DESC LIMIT 1", (sid,))
    if not last:
        return None
    now = now or time.time()
    prev = float(last["created_at"])
    if mode == "idle":
        hours = float(agent_ctx.settings.get("memory", "new_page_idle_hours", 8) or 8)
        return f"после паузы больше {hours:g} ч" if now - prev > hours * 3600 else None
    at = str(agent_ctx.settings.get("memory", "new_page_at", "04:00") or "04:00")
    try:
        hh, mm = (int(x) for x in at.split(":", 1))
    except ValueError:
        hh, mm = 4, 0
    lt = time.localtime(now)
    cut = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, hh, mm, 0, 0, 0, -1))
    if cut > now:
        cut -= 86400
    return "новый день" if prev < cut else None


def reset(agent_ctx, sid: str) -> None:
    """Забыть пересказ (сообщения остаются): следующий запрос снова пойдёт старым окном."""
    agent_ctx.store.update_session(sid, meta={"compact": {}})
