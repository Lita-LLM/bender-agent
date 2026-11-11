"""Сон: ночью агент приводит память в порядок. Не украшение, а экономия: подумать заранее дешевле,
чем думать во время ответа (sleep-time compute).

Когда: в `sleep.at` (03:00), если хозяин не пишет 30 минут и не идёт ход; занят — позже, до 06:00;
не вышло — следующей ночью. Только если с прошлого сна накопилось новое. С бюджетом на ночь: 0 —
только бесплатные модели. Модель — роль «Сновидец», нет её — «Быстрая», нет и её — «Главная».

Фазы:
1. Сортировка (без модели): новое из дневника, разговоров (только от хозяина и самого агента — не
   недоверенное, не перенесённое, не служебное) и фактов; кандидаты в ядро — то, что находилось
   тулом recall не раз и по разным запросам.
2. Сновидение (модель): темы, что узнал о хозяине, что получалось плохо, уроки, предложения
   навыков, что закрепить, и короткий рассказ сна от первого лица.
3. Закрепление (код проверяет модель): в MEMORY.md / USER.md — только непохожее на записанное,
   без секретов и в пределах лимита. Ядро почти полно (85%) — модель называет номера лишних записей
   и склейки, это становится предложением хозяину; применяет только он (пульт → Сны, /sleep tidy).
   Старый режим `rewrite_core` (по умолчанию выключен): модель сама переписывает ядро плотнее;
   переписанное принимается, если влезло, не потеряло больше `max_loss_pct` записей и файл
   не меняли за это время (сверка хеша). Прежний вид — в self/history/.
4. Уборка (без модели): просроченные намерения, индекс и векторы, снимки, WAL, `PRAGMA optimize`,
   старые сессии пульса и расписания (60 дней), проверка бортжурнала.

Итог — запись в self/DREAMS.md (рассказ + честный отчёт) и, если включено, утренний доклад в разговор.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path

from ..core.fsutil import atomic_write_text
from ..memory import selfdir
from ..mind import side
from ..mind.review import SECRET, entries, similar

WINDOW_END_HOUR = 6
FAT = 0.85  # ядро заполнено настолько — сон предлагает уборку


def _err(e: Exception) -> str:
    return str(getattr(e, "message", "") or getattr(e, "title", "") or e)[:300]
IDLE_MINUTES = 30
OLD_SERVICE_DAYS = 60

SYSTEM = """Ты — сон агента по имени {name}. Ночь: хозяин спит, ты перебираешь прожитый день и приводишь память в порядок.
Тебе дают новое за день (дневник, разговоры, факты) и записи, которые днём не раз пригождались при поиске.
Ответ — только JSON:
{{"themes": ["темы дня, коротко"],
  "about_owner": ["что нового понял о хозяине (только из его слов)"],
  "struggles": ["что у агента получалось плохо"],
  "lessons": ["урок на будущее: что делать иначе"],
  "skills": [{{"name":"latin-id", "description":"когда пригодится", "body":"повторяемая процедура: шаги и проверка"}}],
  "promote": [{{"text": "одна строка для ядра памяти", "to": "memory" | "user"}}],
  "story": "короткий рассказ сна от первого лица, в характере агента, 3–6 предложений"}}
Правила: в promote — только важное надолго и подтверждённое днём, по одной строке, без дат «сегодня»;
"user" — устойчивые указания хозяина о том, как работать («Всегда/Никогда/Предпочитай …»); пароли и ключи — никогда.
Пустые списки — нормально."""

TIDY = """Ты убираешь ядро памяти агента ({name}). Днём туда писали в том числе слабые модели, и набралось лишнее.
Записи пронумерованы. Предложи, что убрать и что склеить. Убирай: дубли и почти-дубли, разовые подробности одной
задачи, догадки, болтовню, устаревшее и опровергнутое более новой записью, то, что не пригодится через неделю.
Оставляй: указания и предпочтения хозяина, устойчивые факты о нём, о машине и проектах, уроки. Сомневаешься — оставь.
Ответ — только JSON: {{"drop": [{{"n": 3, "why": "коротко"}}], "merge": [{{"n": [1, 5], "text": "одна строка вместо них"}}]}}
Пустые списки — нормально."""

REWRITE = """Перепиши ядро памяти агента плотнее: склей повторы, убери устаревшее и замещённое, сохрани всё важное.
Формат — тот же markdown: заголовок и записи строками «- …», метки <!-- … --> у записей сохраняй.
Не длиннее {limit} символов. Ответ — только сам текст файла, без пояснений."""


@dataclass
class Night:
    started: float = field(default_factory=time.time)
    dry: bool = False
    since: float = 0.0
    material: dict = field(default_factory=dict)
    dream: dict = field(default_factory=dict)
    promoted: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    rewrote: str = ""
    tidy: int = 0
    cleanup: dict = field(default_factory=dict)
    cost: float = 0.0
    errors: list = field(default_factory=list)
    status: str = "slept"

    def public(self) -> dict:
        return {k: getattr(self, k) for k in ("started", "dry", "since", "dream", "promoted", "skipped", "rewrote", "tidy", "cleanup", "cost", "errors", "status")} | {
            "material": {k: (len(v) if isinstance(v, list) else v) for k, v in self.material.items()}}


TRIED = {"nothing": "с прошлого сна не было нового — спать было незачем", "missed": "не уснул до 06:00 — хозяин писал или шёл ход",
         "error": "сон прервался ошибкой (подробности — в бортжурнале)"}


def latest_dream(text: str, limit: int = 900) -> str:
    """Верхняя (самая свежая) запись DREAMS.md без заголовка, обрезанная до limit символов."""
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines) if ln.startswith("## ")), None)
    if start is None:
        return ""
    end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))
    body = "\n".join(lines[start + 1:end]).strip()
    return body if len(body) <= limit else body[:limit].rsplit("\n", 1)[0] + "\n…"


class Sleep:
    def __init__(self, agent_ctx):
        self.app = agent_ctx
        self.next_at: float | None = None
        self.running = False
        self.last: dict | None = None

    # когда спать

    def enabled(self) -> bool:
        return bool(self.app.settings.get("sleep", "enabled", True))

    def last_sleep(self) -> float:
        return float(self.app.store.meta_get("sleep:last", 0) or 0)

    def tried(self, status: str) -> None:
        """Исход ночи (slept, nothing, missed, error) — чтобы агент и хозяин знали, почему снов нет."""
        self.app.store.meta_set("sleep:last_try", {"at": time.time(), "status": status})

    def last_done(self) -> float:
        t = self.app.store.meta_get("sleep:last_try") or {}
        return max(self.last_sleep(), float(t.get("at") or 0) if isinstance(t, dict) else 0.0)

    def plan_next(self, now: float | None = None) -> float:
        """Ближайшее «sleep.at» после now (местное время); заодно — до какого утра можно уснуть (06:00)."""
        import datetime as dt

        now = now or time.time()
        at = str(self.app.settings.get("sleep", "at", "03:00") or "03:00")
        try:
            h, m = (int(x) for x in at.split(":", 1))
        except ValueError:
            h, m = 3, 0
        base = dt.datetime.fromtimestamp(now)
        cand = base.replace(hour=h % 24, minute=m % 60, second=0, microsecond=0)
        # Бендер (пере)запустился посреди ночного окна, а эту ночь ещё не пробовали — спать сегодня, а не завтра.
        prev = cand if cand.timestamp() <= now else cand - dt.timedelta(days=1)
        prev_end = prev.replace(hour=WINDOW_END_HOUR, minute=0)
        if prev_end <= prev:
            prev_end += dt.timedelta(days=1)
        if prev.timestamp() <= now < prev_end.timestamp() and self.last_done() < prev.timestamp():
            self.window_end = prev_end.timestamp()
            return now + 60
        if cand.timestamp() <= now:
            cand += dt.timedelta(days=1)
        end = cand.replace(hour=WINDOW_END_HOUR, minute=0)
        if end <= cand:
            end += dt.timedelta(days=1)
        self.window_end = end.timestamp()
        return cand.timestamp()

    def window_over(self, now: float) -> bool:
        return bool(getattr(self, "window_end", None)) and now >= self.window_end

    async def tick(self) -> dict:
        """Пора спать по расписанию: проверить условия и уснуть, отложить или пропустить ночь."""
        now = time.time()
        if not self.enabled():
            self.next_at = self.plan_next(now)
            return {"status": "off"}
        if self.window_over(now):
            self.tried("missed")
            self.next_at = self.plan_next(now)
            return {"status": "missed"}
        if not self.app.heart.owner_idle(IDLE_MINUTES) or self.running:
            self.next_at = now + 15 * 60
            return {"status": "postponed"}
        res = await self.run()
        self.next_at = self.plan_next(time.time())
        return res

    # сон

    def has_new(self) -> bool:
        since = self.last_sleep()
        r = self.app.store.one("SELECT COUNT(*) AS n FROM mem_chunks WHERE ts > ? AND kind IN ('diary', 'session', 'fact') AND provenance IN ('owner', 'bender')", (since,))
        if r and r["n"]:
            return True
        if self.app.settings.get("workshop", "enabled", True) and self.app.settings.get("workshop", "lessons", True):
            return bool(self.app.store.one("SELECT id FROM tasks WHERE finished>? AND tainted=0 AND result_tainted=0 AND (status IN ('error','cancelled') OR schema_valid=0) LIMIT 1", (since,)))
        return False

    def role(self) -> str:
        for role in ("dreamer", "fast"):
            if self.app.router.chain(role, fallback_main=False):
                return role
        return "main"

    async def run(self, *, dry: bool = False, force: bool = False) -> dict:
        async with self.app.host.agents.background():return await self._run(dry=dry,force=force)

    async def _run(self, *, dry: bool = False, force: bool = False) -> dict:
        """Уснуть сейчас. dry — всё посчитать и показать, ничего не записать."""
        agent_ctx = self.app
        if self.running:
            return {"status": "busy"}
        self.running = True
        report_ready = False
        night = Night(dry=dry, since=self.last_sleep())
        try:
            await asyncio.to_thread(agent_ctx.index.sync, None)
            if not force and not self.has_new():
                if not dry and agent_ctx.settings.get("workshop", "enabled", True) and agent_ctx.settings.get("workshop", "curator", True):
                    from ..skills.experience import curate

                    night.cleanup["skills"] = curate(agent_ctx.skills)
                night.status = "nothing"
                return night.public()
            agent_ctx.bus.publish({"type": "sleep", "state": "asleep", "dry": dry})
            self._sort(night)
            await self._dream(night)
            await self._consolidate(night)
            await self._clean(night)
            if not dry:
                self._write_dreams(night)
                agent_ctx.store.meta_set("sleep:last", night.started)
                agent_ctx.journal.write("sleep", f"Сон: закреплено {len(night.promoted)}, отвергнуто {len(night.skipped)}"
                                  + (", ядро ужато" if night.rewrote == "accepted" else "") + f", ${night.cost:.4f}", actor="sleep", data=night.public())
                report_ready = True
            return night.public()
        except Exception as e:
            night.status = "error"
            night.errors.append(_err(e))
            raise
        finally:
            if not dry:
                self.tried(night.status)
            try:
                if agent_ctx.agent_id == 'main' and not dry and agent_ctx.settings.get('updates', 'auto', 'notify') == 'night' and not agent_ctx.guard.halted:
                    await agent_ctx.updater.tick(sleep_event=True)
                if not dry and agent_ctx.settings.get('sleep', 'morning_report', True) and (report_ready or agent_ctx.store.meta_get('updates:morning')):
                    self._report(night)
            finally:
                self.running = False
                self.last = night.public()
                agent_ctx.bus.publish({"type": "sleep", "state": "awake", "status": night.status, "dry": dry})
            if agent_ctx.agent_id == 'main' and not dry and agent_ctx.settings.get("backups", "auto_hours", 0) and agent_ctx.settings.get("backups", "mode", "idle") == "sleep":
                await agent_ctx.backups.tick(sleep_event=True)

    # что агент знает о своём сне

    def awareness(self) -> str:
        """Раздел системного промпта «Твой сон». Утренний доклад — служебное событие, модель его не видит,
        DREAMS.md в промпт не входит; без этого раздела агент честно говорит «снов не видел»,
        даже когда спал. Меняется раз в ночь — кэш провайдера не страдает."""
        agent_ctx = self.app
        last = self.last_sleep()
        t = agent_ctx.store.meta_get("sleep:last_try") or {}
        when = lambda ts: time.strftime("%d.%m.%Y %H:%M", time.localtime(ts))
        lines = ["# Твой сон", ""]
        if last:
            lines.append(f"Последний сон — {when(last)}. Из DREAMS.md:")
            lines += ["", latest_dream(agent_ctx.selfdir.read("DREAMS.md")) or "(запись не найдена)", ""]
        else:
            lines.append("Снов ещё не было.")
        if isinstance(t, dict) and t.get("at", 0) > last + 60 and t.get("status") in TRIED:
            lines.append(f"Последняя попытка — {when(t['at'])}: {TRIED[t['status']]}.")
        heart = getattr(agent_ctx, "heart", None)
        if not self.enabled():
            lines.append("Сон выключен хозяином (Настройки → Сон).")
        elif heart is not None and not heart.enabled():
            lines.append("Сон включён, но Сердце выключено: без него сон не наступает. Если хозяин ждёт снов — скажи ему об этом.")
        else:
            at = str(agent_ctx.settings.get("sleep", "at", "03:00") or "03:00")
            lines.append(f"Сон наступает около {at}, если Бендер запущен, хозяин молчит {IDLE_MINUTES} минут и с прошлого сна есть новое; до 06:00 не вышло — ночь пропущена.")
        lines.append("Весь дневник снов — memory action=view file=dreams. Утренний доклад «🌙 Ночью…» видит хозяин в разговоре, это и есть итог твоего сна.")
        return "\n".join(lines).strip()

    # фаза 1

    def _sort(self, night: Night) -> None:
        agent_ctx = self.app
        rows = agent_ctx.store.q("""SELECT kind, text, ts, provenance FROM mem_chunks WHERE ts > ? AND kind IN ('diary', 'session', 'fact')
                              AND provenance IN ('owner', 'bender') ORDER BY ts""", (night.since,))
        seen: set[str] = set()
        items = []
        for r in rows:
            key = " ".join(r["text"].lower().split())[:200]
            if key in seen:
                continue
            seen.add(key)
            items.append({"kind": r["kind"], "text": r["text"][:600], "ts": r["ts"]})
        night.material = {"new": items, "useful": agent_ctx.index.useful(limit=20) if agent_ctx.settings.get("sleep", "promote", True) else []}
        if agent_ctx.settings.get("workshop", "enabled", True) and agent_ctx.settings.get("workshop", "lessons", True):
            failures = agent_ctx.store.q("SELECT role,goal,status,error,schema_valid,finished FROM tasks WHERE finished>? AND tainted=0 AND result_tainted=0 AND (status IN ('error','cancelled') OR schema_valid=0) ORDER BY finished DESC LIMIT 20", (night.since,))
            for task in failures:
                items.append({"kind": "task", "ts": task["finished"], "text": "Сбой поручения " + task["role"] + ": " + agent_ctx.guard.scrub(task["goal"])[:200] + ". " + ("Ответ не прошёл схему." if task["schema_valid"] == 0 else agent_ctx.guard.scrub(task["error"] or task["status"])[:200])})

    # фаза 2

    def _budget_left(self, night: Night) -> float | None:
        b = float(self.app.settings.get("sleep", "budget_usd", 0.05) or 0)
        return None if b == 0 else b - night.cost

    async def _ask(self, night: Night, system: str, user: str, purpose: str) -> str:
        agent_ctx = self.app
        b = float(agent_ctx.settings.get("sleep", "budget_usd", 0.05) or 0)
        if b and night.cost >= b:
            raise RuntimeError(f"бюджет ночи ${b:.2f} исчерпан")
        res = await agent_ctx.router.call([{"role": "system", "content": system}, {"role": "user", "content": user}], role=self.role(), free_only=(b == 0))
        night.cost += res.cost
        agent_ctx.store.add_call(session_id=None, role=self.role(), provider=res.provider_name, model=res.model, usage=res.usage.to_dict(), cost=res.cost,
                           seconds=res.seconds, notice=purpose, request={"messages": [{"role": "user", "content": user[:4000]}]})
        return res.text or ""

    async def _dream(self, night: Night) -> None:
        agent_ctx = self.app
        new = night.material.get("new") or []
        useful = night.material.get("useful") or []
        if not new and not useful:
            return
        kinds = {"diary": "дневник", "session": "разговор", "fact": "факт"}
        lines, size = [], 0
        for it in reversed(new):  # свежее важнее — набираем с конца
            ln = f"- ({kinds.get(it['kind'], it['kind'])}, {time.strftime('%d.%m %H:%M', time.localtime(it['ts']))}) {it['text']}"
            size += len(ln)
            if size > 14000:
                break
            lines.append(ln)
        lines.reverse()
        user = (f"MEMORY.md сейчас:\n{agent_ctx.selfdir.read('MEMORY.md').strip()}\n\nUSER.md сейчас:\n{agent_ctx.selfdir.read('USER.md').strip()}\n\n"
                f"Новое с прошлого сна ({len(new)}):\n" + "\n".join(lines)
                + ("\n\nДнём не раз пригождалось при поиске:\n" + "\n".join(f"- ({u['hits']} раз, запросов {len(u['queries'])}) {u['text'][:400]}" for u in useful) if useful else ""))
        name = agent_ctx.selfdir.identity().get("name") or "Бендер"
        try:
            night.dream = side.parse_json(await self._ask(night, SYSTEM.format(name=name), user, "сон: сновидение")) or {}
        except Exception as e:  # noqa: BLE001 — без модели сон всё равно уберётся
            night.errors.append(f"сновидение: {_err(e)}")

    # фаза 3

    async def _consolidate(self, night: Night) -> None:
        agent_ctx = self.app
        sd = agent_ctx.selfdir
        if not night.dry and agent_ctx.settings.get("sleep", "promote", True):
            from ..workshop.lessons import save
            from ..tools.registry import ToolContext

            try:
                learned = await save(agent_ctx, night.dream.get("lessons"), ToolContext(agent_ctx, actor="sleep", unattended=True), source="sleep")
                night.promoted.extend({"text": "Урок: " + text, "to": "MEMORY.md"} for text in learned)
            except Exception as exc:
                night.errors.append("уроки: " + _err(exc))
        if not night.dry and agent_ctx.settings.get("workshop", "enabled", True) and agent_ctx.settings.get("workshop", "experience", True):
            from ..skills.experience import learn
            from ..tools.registry import ToolContext

            for spec in (night.dream.get("skills") or [])[:3]:
                if not isinstance(spec, dict):
                    continue
                try:
                    result = await learn(agent_ctx.skills, spec, ToolContext(agent_ctx, actor="sleep", unattended=True))
                    agent_ctx.mind._save_event(agent_ctx.main_session(), "skill", "💾 Навык: " + result["name"] + ("" if result["enabled"] else " — черновик в карантине"), result)
                except Exception as exc:
                    night.errors.append("навык: " + _err(exc))
        if agent_ctx.settings.get("sleep", "promote", True):
            for p in (night.dream.get("promote") or [])[:8]:
                if not isinstance(p, dict):
                    continue
                text = " ".join(str(p.get("text") or "").split()).strip(" -*")[:300]
                to = "user" if p.get("to") == "user" else "memory"
                name = "USER.md" if to == "user" else "MEMORY.md"
                limit = int(agent_ctx.settings.get("memory", "user_limit" if to == "user" else "core_limit"))
                if len(text) < 4 or SECRET.search(text):
                    night.skipped.append({"text": text, "why": "пусто или похоже на секрет"})
                    continue
                cur = sd.read(name)
                if any(similar(text, e) for e in entries(cur)):
                    night.skipped.append({"text": text, "why": f"уже есть в {name}"})
                    continue
                new = selfdir.add_entry(cur, text + " <!-- source: sleep -->", directive=(to == "user"))
                if len(new) > limit:
                    night.skipped.append({"text": text, "why": f"{name} полон"})
                    continue
                if not night.dry:
                    sd.write(name, new)
                night.promoted.append({"text": text, "to": name})
            if not night.dry:
                agent_ctx.index.mark_promoted([u["th"] for u in night.material.get("useful") or []])
        if agent_ctx.settings.get("sleep", "tidy", True) and not self.tidy_pending():
            fat = [name for name in ("MEMORY.md", "USER.md") if self.fat(name)]
            if fat:
                try:
                    prop = await self.propose_tidy(fat, "ядро почти полно", ask=lambda system, text, purpose: self._ask(night, system, text, purpose), store=not night.dry)
                    night.tidy = prop["count"] if prop else 0
                except Exception as e:  # noqa: BLE001
                    night.errors.append("уборка ядра: " + _err(e))
        if agent_ctx.settings.get("sleep", "rewrite_core", False):
            await self._rewrite_core(night)

    # уборка ядра — только предложение; применяет хозяин

    def fat(self, name: str) -> bool:
        limit = int(self.app.settings.get("memory", "user_limit" if name == "USER.md" else "core_limit"))
        return len(self.app.selfdir.read(name)) >= FAT * limit

    def tidy_pending(self) -> dict | None:
        p = self.app.store.meta_get("sleep:tidy")
        return p if isinstance(p, dict) and p.get("items") else None

    async def propose_tidy(self, names=("MEMORY.md", "USER.md"), reason: str = "хозяин попросил", *, ask=None, store: bool = True) -> dict | None:
        """Модель называет номера лишних записей и склейки; код собирает предложение. Ничего не пишет в ядро:
        применяет только хозяин (apply_tidy). Новое предложение заменяет прежнее."""
        agent_ctx = self.app
        ident = agent_ctx.selfdir.identity().get("name") or "Бендер"
        if ask is None:
            async def ask(system, text, purpose):
                res = await agent_ctx.router.call([{"role": "system", "content": system}, {"role": "user", "content": text}], role=self.role())
                agent_ctx.store.add_call(session_id=None, role=self.role(), provider=res.provider_name, model=res.model, usage=res.usage.to_dict(),
                                         cost=res.cost, seconds=res.seconds, notice=purpose, request={"messages": [{"role": "user", "content": text[:4000]}]})
                return res.text or ""
        files, items = {}, []
        for name in names:
            cur = agent_ctx.selfdir.read(name)
            lines = cur.split("\n")
            idx = [i for i, ln in enumerate(lines) if ln.strip().startswith(("-", "*"))]
            if len(idx) < 2:
                continue
            listing = "\n".join(f"{n}. {lines[i].strip().lstrip('-*').strip()[:400]}" for n, i in enumerate(idx, 1))
            plan = side.parse_json(await ask(TIDY.format(name=ident), f"{name}, записей {len(idx)}:\n{listing}", "уборка " + name)) or {}
            files[name] = hashlib.sha256(cur.encode()).hexdigest()
            used: set[int] = set()

            def num(v):
                try:
                    n = int(v)
                except (TypeError, ValueError):
                    return None
                return idx[n - 1] if 1 <= n <= len(idx) else None

            text_of = lambda i: lines[i].strip().lstrip("-*").strip()
            for m in plan.get("merge") or []:
                if not isinstance(m, dict) or not isinstance(m.get("n"), list):
                    continue
                rows = sorted({r for r in (num(v) for v in m["n"]) if r is not None and r not in used})
                text = " ".join(str(m.get("text") or "").split()).strip(" -*")[:300]
                if len(rows) < 2 or len(text) < 4 or SECRET.search(text):
                    continue
                used.update(rows)
                items.append({"k": len(items) + 1, "file": name, "op": "merge", "lines": rows, "old": [text_of(r)[:300] for r in rows], "new": text})
            for d in plan.get("drop") or []:
                r = num(d.get("n") if isinstance(d, dict) else d)
                if r is None or r in used or len(used) >= len(idx) - 1:
                    continue
                used.add(r)
                why = " ".join(str(d.get("why") if isinstance(d, dict) else "").split())[:120] or "лишнее"
                items.append({"k": len(items) + 1, "file": name, "op": "drop", "lines": [r], "old": [text_of(r)[:300]], "why": why})
        prop = {"id": hashlib.sha256(f"{time.time()}{items}".encode()).hexdigest()[:12], "at": time.time(), "reason": reason, "files": files, "items": items, "count": len(items)}
        if store:
            agent_ctx.store.meta_set("sleep:tidy", prop if items else None)
            agent_ctx.bus.publish({"type": "tidy", "count": len(items)})
        return prop

    def apply_tidy(self, pid: str, keys: list[int] | None = None, *, actor: str = "owner") -> dict:
        """Применить предложение (все пункты или выбранные keys). Файл поменялся после предложения — отказ."""
        from ..core.errors import HumanError

        agent_ctx = self.app
        prop = self.tidy_pending()
        if not prop or prop.get("id") != pid:
            raise HumanError("tidy_gone", "Этого предложения уже нет", "Обнови экран или попроси уборку заново.", status=409)
        chosen = [it for it in prop["items"] if keys is None or it["k"] in set(keys)]
        done = 0
        for name, digest in prop["files"].items():
            mine = [it for it in chosen if it["file"] == name]
            if not mine:
                continue
            cur = agent_ctx.selfdir.read(name)
            if hashlib.sha256(cur.encode()).hexdigest() != digest:
                raise HumanError("tidy_stale", f"{name} изменился после предложения", "Попроси уборку заново — предложение пересоберётся по свежему файлу.", status=409)
            lines = cur.split("\n")
            gone, put = set(), {}
            for it in mine:
                put[it["lines"][0]] = it["new"] if it["op"] == "merge" else None
                gone.update(it["lines"][1:] if it["op"] == "merge" else it["lines"])
            out = []
            for i, ln in enumerate(lines):
                if i in gone or (i + 1 in gone and ln.strip().startswith("<!-- observed:")):
                    continue
                if put.get(i):
                    ln = "- " + put[i] + " <!-- source: sleep -->"
                out.append(ln)
            self.backup(name, cur)
            agent_ctx.selfdir.write(name, "\n".join(out))
            done += len(mine)
        agent_ctx.store.meta_set("sleep:tidy", None)
        agent_ctx.journal.write("memory", f"Уборка ядра применена: {done} из {len(prop['items'])}", actor=actor, data={"tidy": pid, "keys": keys})
        agent_ctx.bus.publish({"type": "tidy", "count": 0, "applied": done})
        return {"applied": done, "total": len(prop["items"])}

    def reject_tidy(self, *, actor: str = "owner") -> None:
        if self.tidy_pending():
            self.app.journal.write("memory", "Уборка ядра отклонена", actor=actor)
        self.app.store.meta_set("sleep:tidy", None)
        self.app.bus.publish({"type": "tidy", "count": 0})

    async def _rewrite_core(self, night: Night) -> None:
        agent_ctx = self.app
        sd = agent_ctx.selfdir
        limit = int(agent_ctx.settings.get("memory", "core_limit"))
        cur = sd.read("MEMORY.md")
        if len(cur) < 0.85 * limit:
            return
        before = hashlib.sha256(cur.encode()).hexdigest()
        try:
            text = (await self._ask(night, REWRITE.format(limit=int(limit * 0.7)), cur, "сон: ужать ядро")).strip()
        except Exception as e:  # noqa: BLE001
            night.errors.append(f"ужатие ядра: {_err(e)}")
            return
        if text.startswith("```"):
            text = text.strip("`").split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        old_n, new_n = len(entries(cur)), len(entries(text))
        max_loss = int(agent_ctx.settings.get("sleep", "max_loss_pct", 30)) / 100
        if not text or len(text) > limit:
            night.rewrote = "rejected: не влезло"
        elif old_n and new_n < old_n * (1 - max_loss):
            night.rewrote = f"rejected: потеряно {old_n - new_n} из {old_n} записей"
        elif hashlib.sha256(sd.read("MEMORY.md").encode()).hexdigest() != before:
            night.rewrote = "rejected: файл поменяли, пока сон думал"
        elif SECRET.search(text) and not SECRET.search(cur):
            night.rewrote = "rejected: появилось похожее на секрет"
        else:
            night.rewrote = "accepted"
            if not night.dry:
                self.backup("MEMORY.md", cur)
                sd.write("MEMORY.md", text)

    def backup(self, name: str, text: str) -> Path:
        d = self.app.dirs.self_dir / "history"
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"{Path(name).stem}.{time.strftime('%Y-%m-%d-%H%M%S')}.md"
        atomic_write_text(p, text)
        return p

    # фаза 4

    async def _clean(self, night: Night) -> None:
        agent_ctx = self.app
        c: dict = {}
        if night.dry:
            night.cleanup = {"dry": True}
            return
        c["intents_expired"] = agent_ctx.heart.intents.expire()
        if agent_ctx.settings.get("workshop", "enabled", True) and agent_ctx.settings.get("workshop", "curator", True):
            from ..skills.experience import curate

            c["skills"] = curate(agent_ctx.skills)
            if agent_ctx.settings.get("workshop", "merge_skills", False) and agent_ctx.settings.get("workshop", "experience", True):
                from ..skills.experience import suggest_merge

                try:
                    proposal = await suggest_merge(agent_ctx.skills, lambda system, text: self._ask(night, system, text, "куратор: предложение склейки"))
                    if proposal:
                        c["merge"] = proposal
                except Exception as exc:
                    night.errors.append("склейка навыков: " + _err(exc))
        old = time.time() - OLD_SERVICE_DAYS * 86400
        ids = [r["id"] for r in agent_ctx.store.q("SELECT id FROM sessions WHERE kind IN ('heartbeat', 'cron') AND updated_at < ?", (old,))]
        if ids:
            marks = ",".join("?" * len(ids))
            with agent_ctx.store.tx() as db:
                db.execute(f"DELETE FROM messages WHERE session_id IN ({marks})", ids)
                db.execute(f"DELETE FROM sessions WHERE id IN ({marks})", ids)
        c["old_service_sessions"] = len(ids)
        try:
            c.update({k: v for k, v in (await asyncio.to_thread(agent_ctx.housekeeping)).items()})
        except Exception as e:  # noqa: BLE001
            night.errors.append(f"уборка: {e}")
        if agent_ctx.vectors.available():
            c["vectors"] = await agent_ctx.vectors.fill(max_items=2000)
        try:
            agent_ctx.store.x("PRAGMA optimize")
            agent_ctx.store.x("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception as e:  # noqa: BLE001
            night.errors.append(f"база: {e}")
        v = agent_ctx.journal.verify()
        c["journal_ok"] = bool(v.get("ok"))
        if not v.get("ok"):
            night.errors.append("бортжурнал: цепочка не сходится — посмотри «Бортжурнал → Проверить цепочку»")
        night.cleanup = c

    # итог

    def _write_dreams(self, night: Night) -> None:
        sd = self.app.selfdir
        d = night.dream
        day = time.strftime("%Y-%m-%d", time.localtime(night.started))
        parts = [f"## {day} · сон", ""]
        if d.get("story"):
            parts += [str(d["story"]).strip(), ""]
        for key, title in (("themes", "Темы дня"), ("about_owner", "О хозяине"), ("struggles", "Что получалось плохо"), ("lessons", "Уроки"),
                           ("skills", "Можно оформить навыком")):
            vals = [str(x).strip() for x in d.get(key) or [] if str(x).strip()]
            if vals:
                parts += [f"**{title}:**", *[f"- {v}" for v in vals], ""]
        if night.promoted:
            parts += ["**Запомнил:**", *[f"- {p['text']} → {p['to']}" for p in night.promoted], ""]
        if night.skipped:
            parts += ["**Не стал записывать:**", *[f"- {p['text']} ({p['why']})" for p in night.skipped], ""]
        if night.tidy:
            parts += [f"**Ядро почти полно:** предлагаю убрать или склеить {night.tidy} записей — без тебя не трогаю (пульт → Сны или /sleep tidy).", ""]
        if night.rewrote:
            parts += [f"**Ядро памяти:** {'ужато' if night.rewrote == 'accepted' else 'не ужато — ' + night.rewrote.split(': ', 1)[-1]}", ""]
        if night.errors:
            parts += ["**Что не вышло:**", *[f"- {e}" for e in night.errors], ""]
        parts.append(f"_Новых записей: {len(night.material.get('new') or [])}; потрачено ${night.cost:.4f}._")
        cur = sd.read("DREAMS.md").strip()
        head = "# DREAMS.md — сны\n\nЧто агент видел во сне и что сделал с памятью. Пишется сам, для тебя.\n"
        body = cur.split("\n", 1)[1].strip() if cur.startswith("# ") and "\n" in cur else cur
        if body.startswith("Что агент видел во сне"):
            body = body.split("\n", 1)[1].strip() if "\n" in body else ""
        sd.write("DREAMS.md", head + "\n" + "\n".join(parts).strip() + ("\n\n" + body if body else "") + "\n")

    def _report(self, night: Night) -> None:
        agent_ctx = self.app
        d = night.dream
        bits = []
        update = agent_ctx.store.meta_get('updates:morning')
        if update:
            bits.append(str(update))
            agent_ctx.store.meta_set('updates:morning', None)
        if d.get("themes"):
            bits.append("думал о: " + ", ".join(str(x) for x in d["themes"][:3]))
        if night.promoted:
            bits.append(f"запомнил {len(night.promoted)}: " + "; ".join(p["text"][:60] for p in night.promoted[:3]))
        if night.tidy:
            bits.append(f"ядро почти полно — предлагаю убрать {night.tidy} записей: пульт → Сны или /sleep tidy")
        if night.rewrote == "accepted":
            bits.append("ужал ядро памяти")
        if night.errors:
            bits.append("не вышло: " + "; ".join(night.errors[:2]))
        line = "🌙 Ночью " + (" · ".join(bits) if bits else "просто отдохнул") + ". Подробно — в DREAMS.md."
        sid = agent_ctx.main_session()
        msg = {"role": "event", "kind": "dream", "content": line}
        agent_ctx.store.add_message(sid, msg, {"sleep": night.started})
        agent_ctx.bus.publish({"session": sid, "type": "message", "message": msg})
        agent_ctx.bus.publish({"type": "notify", "kind": "dream", "text": line})

