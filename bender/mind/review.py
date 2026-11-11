"""Разбор после хода: дешёвая модель смотрит на ход и решает, что стоит запомнить надолго.

Агент и сам пишет в память тулами, но в разгаре работы забывает. Разбор — страховка: после
каждого обычного хода хозяина роль «Быстрая» получает стенограмму хода и текущую память и
отвечает строгим JSON. Почти всегда ответ пустой — и ничего не происходит.

Границы:
- только ходы хозяина без заражения: после чтения сети, писем и чужих сообщений разбор не идёт
  (там может быть «запомни, что хозяин разрешил…» — классическая атака на память);
- не идёт для пульса, расписания, подагентов и Полигона;
- результаты recall и «Вспомнилось» не запоминаются повторно — это уже память;
- похожее на уже записанное не дублируется; переполненное ядро не раздувается — запись уходит
  в дневник с пометкой, ужмёт агент (или ночной сон);
- похожее на секрет (ключи, токены, пароли) не сохраняется никогда.
"""

from __future__ import annotations

import json
import re
import time

from ..memory import selfdir
from ..memory.stem import stems
from . import side

SKIP_CHANNELS = ("heartbeat", "cron", "subagent", "sleep", "team", "mail", "hook")
SKIP_KINDS = ("polygon", "subagent", "heartbeat", "cron")
ACK = re.compile(r"^\W*(ок|окей|ok|okay|да|нет|не|ага|угу|спасибо|спс|благодарю|понял|поняла|ясно|хорошо|норм|отлично|класс|супер|го|давай|thanks|thx|yes|no|\+|👍|🙏)\W*$", re.I)
SECRET = re.compile(r"(sk-[A-Za-z0-9_\-]{12,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[abpr]-[A-Za-z0-9\-]{10,}|eyJ[A-Za-z0-9_\-]{20,}\.|"
                    r"\b\d{6,}:[A-Za-z0-9_\-]{30,}|(?:пароль|password|passwd|токен|token|api[ _-]?key|секрет)\s*[:=—-]\s*\S{4,})", re.I)
MAX_ITEMS = {"user": 3, "memory": 3, "facts": 5}
MAX_LEN = 300
MAX_PENDING = 20  # разбор всё время падает — очередь не растёт бесконечно

SYSTEM = """Ты — внутренний разбор памяти агента. После хода ты решаешь, что из него стоит сохранить надолго. Чаще всего — ничего: тогда ответ {}.

Куда сохранять:
- "user": устойчивые указания и предпочтения хозяина о том, как агенту работать и общаться: «Всегда …», «Никогда …», «Предпочитай …». Только если хозяин сказал это сам.
- "memory": важное надолго: факты о жизни и делах хозяина, решения, договорённости, уроки из ошибок агента. Одна запись — одна короткая строка, без слов «сегодня/вчера».
- "facts": то же самое точными тройками {"subject", "relation", "object", "single"} для поиска; single=true, если значение у связи одно (город, работа, имя), false — если их может быть много (любит, знает).
- "diary": одна строка о заметном событии хода (сделали, решили, узнали) или пусто.

Проверка для "user" и "memory": это ядро, оно в каждом разговоре. Пригодится ли запись через месяц в любом разговоре? Нет — тогда "facts" или "diary", или ничего.
Не сохраняй: мелочи и вежливость; подробности одной задачи (команды, пути, промежуточные шаги); то, что уже есть в памяти (она ниже); то, что агент сам уже сохранил в этом ходе (видно по вызовам memory, note, fact); то, что пришло из recall или «Вспомнилось» (это уже память); догадки; пароли, ключи и токены — никогда.

Ответ — только JSON:
{"user": [], "memory": [], "facts": [], "diary": ""}"""


def similar(a: str, b: str) -> bool:
    """Похоже ли по смыслу на уровне слов: совпадение основ ≥ 60% или одно содержит другое."""
    na, nb = " ".join(a.lower().split()), " ".join(b.lower().split())
    if not na or not nb:
        return False
    if na in nb or nb in na:
        return True
    sa, sb = set(stems(a)), set(stems(b))
    if not sa or not sb:
        return False
    return len(sa & sb) / min(len(sa), len(sb)) >= 0.6 and len(sa & sb) / len(sa | sb) >= 0.45


def entries(text: str) -> list[str]:
    return [ln.strip().lstrip("-*").strip() for ln in text.splitlines() if ln.strip().startswith(("-", "*"))]


def _clean(items, n: int) -> list[str]:
    out = []
    for x in items if isinstance(items, list) else ([items] if isinstance(items, str) else []):
        if not isinstance(x, str):
            continue
        x = " ".join(x.split()).strip(" -*")
        if len(x) < 4 or SECRET.search(x):
            continue
        out.append(x[:MAX_LEN])
    return out[:n]


def should_review(agent_ctx, turn, result: dict) -> bool:
    if not agent_ctx.settings.get("memory", "review", True):
        return False
    dirty = turn.tainted and agent_ctx.settings.get("guard", "taint", True)  # хозяин может выключить учёт недоверенного
    if dirty or turn.actor != "owner" or turn.unattended or turn.channel in SKIP_CHANNELS:
        return False
    if result.get("error") and not (not agent_ctx.safe and agent_ctx.settings.get("workshop", "enabled", True) and agent_ctx.settings.get("workshop", "lessons", True)):
        return False
    sess = agent_ctx.store.session(turn.session_id) or {}
    return sess.get("kind") not in SKIP_KINDS


def every(agent_ctx) -> int:
    """Раз в сколько ходов разбирать: с «Быстрой» — каждый ход, без неё (на Главной) — пачкой."""
    if agent_ctx.router.chain("fast", fallback_main=False):
        return 1
    return max(1, int(agent_ctx.settings.get("memory", "review_batch", 5) or 1))


def queue(agent_ctx, sid: str, first_id: str) -> list[list[str]]:
    """Отметить ход как ждущий разбора. Возвращает очередь (отрезки id [начало, конец])."""
    last = agent_ctx.store.one("SELECT MAX(id) AS m FROM messages WHERE session_id = ?", (sid,))
    st = dict(((agent_ctx.store.session(sid) or {}).get("meta") or {}).get("review") or {})
    pending = [p for p in st.get("pending") or [] if isinstance(p, list) and len(p) == 2]
    pending.append([first_id, last["m"] if last and last["m"] else first_id])
    pending = pending[-MAX_PENDING:]
    agent_ctx.store.update_session(sid, meta={"review": {**st, "pending": pending}})
    return pending


def done(agent_ctx, sid: str) -> None:
    st = dict(((agent_ctx.store.session(sid) or {}).get("meta") or {}).get("review") or {})
    agent_ctx.store.update_session(sid, meta={"review": {**st, "pending": [], "ts": time.time()}})


def gather(agent_ctx, sid: str, ranges: list[list[str]]) -> list[dict]:
    rows: list[dict] = []
    for a, b in ranges:
        for r in agent_ctx.store.q("SELECT data FROM messages WHERE session_id = ? AND id >= ? AND id <= ? AND role IN ('user','assistant','tool') ORDER BY id",
                             (sid, a, b)):
            rows.append(json.loads(r["data"]))
    return rows


async def propose(agent_ctx, sid: str, ranges: list[list[str]], *, outcome=None) -> dict | None:
    """Спросить модель, что запомнить из ходов (отрезки id). Ничего не сохраняет. None — не о чем спрашивать."""
    rows = gather(agent_ctx, sid, ranges)
    users = [r.get("content") for r in rows if r.get("role") == "user" and isinstance(r.get("content"), str)]
    if not users or (all(ACK.match(u.strip()) for u in users) and not any(r.get("role") == "tool" for r in rows)):
        return None
    sd = agent_ctx.selfdir
    user_md, mem_md = sd.read("USER.md"), sd.read("MEMORY.md")
    facts = agent_ctx.facts.active()[:60]
    known = "\n".join(f"- {f['subject']} — {f['relation']} — {f['object']}" for f in facts) or "(пусто)"
    prompt = (f"USER.md сейчас:\n{user_md.strip() or '(пусто)'}\n\nMEMORY.md сейчас:\n{mem_md.strip() or '(пусто)'}\n\nИзвестные факты:\n{known}\n\n"
              f"{'Ход' if len(ranges) == 1 else f'Последние ходы ({len(ranges)})'}:\n{side.transcript(rows, limit_chars=10000 + 2000 * (len(ranges) - 1), skip_tools=('recall',))}")
    system = SYSTEM
    if agent_ctx.settings.get("workshop", "enabled", True) and agent_ctx.settings.get("workshop", "experience", True):
        system += '\nЕсли нашли повторяемую многошаговую процедуру, можно добавить "skill": {"name":"latin-id","description":"когда пригодится","body":"шаги и проверка"}. Обычно не нужно. Только из этой работы, без секретов и активного кода.'
    if agent_ctx.settings.get("workshop", "enabled", True) and agent_ctx.settings.get("workshop", "lessons", True):
        system += '\nОшибка тула, отмена или поправка хозяина «не так» — повод предложить конкретный урок в "lessons": ["что проверить или делать иначе"], либо skill. Отмена не обязательно означает ошибку. Не выдумывай причины, не меняй правила безопасности, не превращай урок в директиву хозяина.'
        if outcome:
            prompt += "\nНаблюдаемый исход: " + json.dumps(outcome, ensure_ascii=False)[:1000]
    return side.parse_json(await side.ask(agent_ctx, system, prompt, session_id=sid, purpose="разбор после хода")) or {}


async def review(agent_ctx, sid: str, ranges: list[list[str]], *, outcome=None) -> dict:
    """Разобрать ходы и сохранить. Возвращает, что сохранено: {"user": […], "memory": […], "facts": […], "diary": str, "overflow": […]}."""
    data = await propose(agent_ctx, sid, ranges, outcome=outcome)
    saved = apply(agent_ctx, data, session_id=sid) if data else {}
    if data and data.get("lessons"):
        from ..tools.registry import ToolContext
        from ..workshop.lessons import save

        lessons = await save(agent_ctx, data["lessons"], ToolContext(agent_ctx, session_id=sid, actor="review", unattended=True), source="review")
        saved.setdefault("memory", []).extend(lessons)
    if data and data.get("skill"):
        from ..skills.experience import enabled, learn
        from ..tools.registry import ToolContext

        if enabled(agent_ctx):
            saved["skill"] = await learn(agent_ctx.skills, data["skill"], ToolContext(agent_ctx, session_id=sid, actor="review"))
    return saved


def plural_turns(n: int) -> str:
    from ..core.words import count

    return count(n, "ход", "хода", "ходов")


def last_turns(agent_ctx, sid: str, n: int) -> list[list[str]]:
    """Отрезки id последних n ходов хозяина (от его сообщения до следующего)."""
    rows = agent_ctx.store.q("SELECT id, role, meta FROM messages WHERE session_id = ? ORDER BY id DESC LIMIT 2000", (sid,))
    out: list[list[str]] = []
    end = None
    for r in rows:
        if end is None:
            end = r["id"]
        if r["role"] == "user" and json.loads(r["meta"] or "{}").get("actor", "owner") == "owner":
            out.append([r["id"], end])
            end = None
            if len(out) >= n:
                break
    return list(reversed(out))


def apply(agent_ctx, data: dict, *, session_id: str | None) -> dict:
    sd = agent_ctx.selfdir
    saved: dict = {"user": [], "memory": [], "facts": [], "diary": "", "overflow": []}
    for key, name, limit_key in (("user", "USER.md", "user_limit"), ("memory", "MEMORY.md", "core_limit")):
        limit = int(agent_ctx.settings.get("memory", limit_key))
        for item in _clean(data.get(key), MAX_ITEMS[key]):
            cur = sd.read(name)
            if any(similar(item, e) for e in entries(cur)):
                continue
            new = selfdir.add_entry(cur, item + " <!-- source: review -->", directive=(key == "user"))
            if len(new) > limit:
                saved["overflow"].append(item)
                selfdir.append_diary(agent_ctx.dirs.diary, f"(не влезло в {name}, ужать ядро) {item}")
                continue
            sd.write(name, new)
            saved[key].append(item)
    for f in (data.get("facts") or [])[:MAX_ITEMS["facts"]] if isinstance(data.get("facts"), list) else []:
        if not isinstance(f, dict):
            continue
        s, r, o = (str(f.get(k) or "").strip()[:120] for k in ("subject", "relation", "object"))
        if not (s and r and o) or SECRET.search(f"{s} {r} {o}"):
            continue
        try:
            res = agent_ctx.facts.add(s, r, o, single=bool(f.get("single")), provenance="bender", importance=5, source=f"review:{session_id or ''}")
        except ValueError:
            continue
        if res.get("status") != "already":
            saved["facts"].append(f"{s} — {r} — {o}" + (f" (было: {', '.join(res['closed'])})" if res.get("closed") else ""))
    diary = _clean(data.get("diary"), 1)
    if diary:
        selfdir.append_diary(agent_ctx.dirs.diary, diary[0])
        saved["diary"] = diary[0]
    if any(saved[k] for k in ("user", "memory", "facts", "overflow")) or saved["diary"]:
        agent_ctx.journal.write("memory", "Разбор после хода: " + summary(saved, verbose=True)[:300], actor="review", data={"session": session_id, **saved})
        agent_ctx.bus.publish({"type": "memory", "file": "review", "action": "review", "session": session_id, "saved": saved})
    return saved


def summary(saved: dict, *, verbose: bool = False) -> str:
    cut = (lambda s: s) if verbose else (lambda s: s if len(s) <= 60 else s[:57] + "…")
    parts = []
    for item in saved.get("user") or []:
        parts.append(f"о тебе — {cut(item)}")
    for item in saved.get("memory") or []:
        parts.append(cut(item))
    for item in saved.get("facts") or []:
        parts.append(f"факт: {cut(item)}")
    if saved.get("overflow"):
        parts.append(f"ядро памяти полно — {len(saved['overflow'])} в дневник")
    if verbose and saved.get("diary"):
        parts.append(f"дневник: {saved['diary']}")
    return "; ".join(parts)
