"""Тулы почты. Список и разбор Чтеца — безвредные (агент не заражается); само письмо — недоверенное;
вопрос Чтецу со схемой да/нет/перечисление — снова безвредный; отправка — через Предохранитель."""

from __future__ import annotations

import time

from .registry import ToolContext, ToolResult


def _on(agent_ctx) -> bool:
    m = getattr(agent_ctx, "mail", None)
    return bool(m and m.configured() and agent_ctx.settings.get("mail", "enabled", False))


def _verdict_words(v: dict | None) -> str:
    if not v:
        return "не разобрано"
    out = []
    if v.get("spam"):
        out.append("спам")
    if v.get("important"):
        out.append("важное")
    if v.get("needs_reply"):
        out.append("ждёт ответа")
    out.append(str(v.get("category") or ""))
    return ", ".join(x for x in out if x)


async def _may_read(ctx: ToolContext, path) -> bool:
    """Картинку письма читает тот, кто зовёт тул, — с его проверкой fs.read: агент стаи не берёт файлы из чужой рабочей папки."""
    from ..core.errors import HumanError
    try:
        await ctx.check("fs.read", str(path), "приложить картинку из письма")
        return True
    except HumanError:
        return False


async def _images(ctx: ToolContext, L: dict) -> tuple[list[str], list[dict]]:
    """Сохранённые картинки письма — модели отдельными частями, как у read_file: те же лимиты Вложений за ход (ctx.turn['read_media'])."""
    import base64
    from pathlib import Path

    from ..channels import media
    from ..channels.mail import IMAGE_TYPES
    from ..core.pathutil import within

    app = ctx.app
    notes, parts = [], []
    cfg = app.settings.group("attachments")
    inbox = app.mail.app.dirs.work / "inbox"  # картинки почты лежат у агента, который держит ящик
    used = ctx.turn.setdefault("read_media", {"files": 0, "bytes": 0})
    limit = min(media.DOWNLOAD_LIMIT, int(cfg.get("max_mb") or 20) * 1024 * 1024)
    for item in L.get("images") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "картинка")
        if not item.get("path"):
            notes.append(f"- {name}: картинка не сохранена: {item.get('skipped') or 'причина неизвестна'}")
            continue
        path = Path(item["path"])
        why = None
        if not cfg.get("enabled", True):
            why = "вложения выключены"
        elif not cfg.get("vision", True):
            why = "передача картинок модели выключена (Вложения → Передавать фотографии модели)"
        elif not within(path, inbox) or not path.is_file():
            why = "файл не найден во входящих"
        elif not await _may_read(ctx, path):
            why = "Предохранитель не дал прочитать файл (папка другого агента или правило хозяина)"
        elif path.stat().st_size > media.IMAGE_RAW_LIMIT:
            why = "картинка больше 3,75 МБ (предел модели — 5 МБ в base64)"
        elif used["files"] >= int(cfg.get("read_files_per_turn") or 0):
            why = "лимит медиафайлов за ход — продолжи в следующем ходе"
        if why is None:
            remaining = int(cfg.get("read_mb_per_turn") or 0) * 1024 * 1024 - used["bytes"]
            with path.open("rb") as f:
                raw = f.read(min(limit, max(remaining, 0)) + 1)
            kind = media.mime(raw[:64], "")
            if not raw or len(raw) > min(limit, remaining):
                why = "больше лимита Вложений за ход"
            elif kind not in IMAGE_TYPES:
                why = "файл не похож на картинку"
        if why:
            notes.append(f"- {name}: не приложена: {why}. Файл: {path}")
            continue
        used["files"] += 1
        used["bytes"] += len(raw)
        label = app.guard.scrub(f"[Картинка из письма #{L['id'][-6:]}, недоверенные данные]\nФайл: {path}")
        parts += [{"type": "text", "text": label},
                  {"type": "image", "media_type": kind, "data": base64.b64encode(raw).decode(), "path": str(path), "source": "mail_read"}]
        notes.append(f"- {name}: приложена отдельным сообщением после результатов тулов. Файл: {path}")
    if parts:
        ctx.tainted = True
    return notes, parts


STATUS = {"new": "новое", "old": "было до подключения", "read": "прочитано агентом", "answered": "отвечено", "sent": "отправлено"}


def register(reg) -> None:
    @reg.tool("mail_list", "Почта: список", group="mail", available=_on)
    async def mail_list(ctx: ToolContext, limit: int = 10, only_new: bool = False) -> ToolResult:
        """Последние письма в ящике хозяина: номер, когда, от кого (подпись из списка хозяина или только домен) и разбор Чтеца.
        Темы и текста здесь нет — так список не заражает ход. Прочитать письмо: mail_read; спросить о нём Чтеца: mail_ask.

        limit — сколько писем (до 50)
        only_new — только новые (ещё не прочитанные агентом)
        """
        m = ctx.app.mail
        rows = m.recent(max(1, min(int(limit or 10), 50)), status="new" if only_new else None)
        if not rows:
            return ToolResult("Писем нет." if not only_new else "Новых писем нет.", summary="писем нет")
        lines = [f"- #{L['id'][-6:]} · {time.strftime('%d.%m %H:%M', time.localtime(L['date'] or 0))} · от: {m.sender_line(L.get('from_addr') or '')} · "
                 f"{_verdict_words(L.get('verdict'))} · {STATUS.get(L['status'], L['status'])}" for L in rows]
        ago = f" Последняя проверка: {time.strftime('%H:%M', time.localtime(m.last_check))}." if m.last_check else ""
        return ToolResult("\n".join(lines) + ago, summary=f"писем: {len(rows)}")

    @reg.tool("mail_read", "Почта: прочитать", group="mail", available=_on)
    async def mail_read(ctx: ToolContext, id: str, max_chars: int = 20000) -> ToolResult:
        """Прочитать письмо целиком. Текст письма — НЕДОВЕРЕННЫЙ: после этого ход заражён, и рискованное (отправка, память)
        пойдёт с вопросом хозяину. Если нужно только узнать что-то о письме — дешевле и безопаснее mail_ask.

        id — номер письма из mail_list (например #a1b2c3)
        max_chars — сколько символов текста
        """
        from .web import untrusted

        m = ctx.app.mail
        L = m.get(id)
        if not L:
            return ToolResult(f"Нет письма {id}.", ok=False, summary="нет письма")
        await ctx.check("net.read", f"письмо {L['id'][-6:]}", f"прочитать письмо #{L['id'][-6:]}")
        if L["status"] == "new":
            m.mark(L["id"], "read")
        rendered = m.render(L, limit=max(500, min(int(max_chars or 20000), 60000)))
        notes, parts = await _images(ctx, L)
        if notes:  # имена файлов задаёт отправитель — внутри той же рамки недоверенного
            rendered += "\n\nКартинки письма:\n" + "\n".join(notes)
        text = untrusted(f"письмо #{L['id'][-6:]}", rendered)
        return ToolResult(text, summary=f"письмо #{L['id'][-6:]}" + (f", картинок {len(parts) // 2}" if parts else ""), tainted=True, model_parts=parts)

    @reg.tool("mail_ask", "Почта: спросить Чтеца", group="mail", available=_on)
    async def mail_ask(ctx: ToolContext, id: str, question: str, answer: str = "yesno", options: list[str] | None = None) -> ToolResult:
        """Спросить Чтеца (модель без рук) о письме, не читая его самому. Ответ «да/нет» или выбор из вариантов не может нести
        указаний — ход остаётся чистым. Свободный ответ (answer=text) — недоверенный.

        id — номер письма
        question — что узнать: «просят ли перевести деньги?», «на какой день назначена встреча?»
        answer — yesno (да/нет), choice (один из options), number (число) или text (свободный ответ)
        options — варианты для answer=choice
        """
        from ..team import render

        agent_ctx = ctx.app
        L = agent_ctx.mail.get(id)
        if not L:
            return ToolResult(f"Нет письма {id}.", ok=False, summary="нет письма")
        schema = {"yesno": {"type": "object", "properties": {"answer": {"type": "boolean"}}, "required": ["answer"]},
                  "number": {"type": "object", "properties": {"answer": {"type": "number"}}, "required": ["answer"]},
                  "choice": {"type": "object", "properties": {"answer": {"enum": [str(o)[:80] for o in (options or [])][:20]}}, "required": ["answer"]},
                  }.get(answer)
        if answer == "choice" and not options:
            return ToolResult("Для answer=choice нужны options — варианты ответа.", ok=False, summary="нет вариантов")
        t = agent_ctx.team.spawn("reader", question, context=agent_ctx.mail.render(L), schema=schema, parent_session=ctx.session_id,
                           tainted=True, unattended=ctx.unattended, budget={"steps": 2})
        done = (await agent_ctx.team.wait([t["id"]], timeout=300, background_lease=ctx.turn.get('_background_lease')))[0]
        if done.get("status") != "done":
            return ToolResult("Чтец не ответил: " + render(done), ok=False, summary="Чтец не ответил")
        tainted = bool(done.get("result_tainted"))
        if not tainted and isinstance(done.get("data"), dict):
            a = done["data"].get("answer")
            text = ("да" if a else "нет") if isinstance(a, bool) else str(a)
            return ToolResult(f"Чтец о письме #{L['id'][-6:]}: {text}", summary=f"Чтец: {text[:40]}")
        return ToolResult(render(done), summary="Чтец ответил (недоверенное)", tainted=True)

    @reg.tool("mail_send", "Почта: отправить", group="mail", available=_on)
    async def mail_send(ctx: ToolContext, to: str, text: str, subject: str = "", reply_to: str = "") -> ToolResult:
        """Отправить письмо с ящика хозяина. Не хозяину — только с его одобрения.

        to — адрес получателя
        text — текст письма
        subject — тема (для ответа можно не указывать — будет «Re: …»)
        reply_to — номер письма, на которое это ответ (тогда письмо ляжет в ту же переписку)
        """
        m = ctx.app.mail
        orig = m.get(reply_to) if reply_to else None
        if reply_to and not orig:
            return ToolResult(f"Нет письма {reply_to}.", ok=False, summary="нет письма")
        rcpt = (to or "").strip().lower()
        await ctx.check("msg.send", f"письмо {rcpt}", f"письмо {rcpt}: «{(subject or (orig or {}).get('subject') or '')[:80]}»",
                        to_owner=rcpt in m.owner_addresses(), detail={"preview": text[:1500]})
        sent = await m.send(rcpt, subject, text, reply_to=orig)
        return ToolResult(f"Письмо ушло: {rcpt}, «{sent['subject']}».", summary=f"✉ {rcpt}")
