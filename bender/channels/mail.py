"""Почта: входящие по IMAP, исходящие по SMTP — на стандартной библиотеке.

Входящее письмо — недоверенный текст от кого угодно, поэтому агент его сам не читает, пока не захочет:
- ящик открывается только для чтения (`SELECT … readonly`, `BODY.PEEK`) — у хозяина письма не становятся прочитанными;
- новое письмо разбирает Чтец (субагент без рук) по схеме: спам ли, важное ли, нужен ли ответ, категория.
  Эти поля — да/нет и значения из списка: указаний они нести не могут, поэтому агент видит их, не заражаясь.
  Короткий пересказ Чтеца — свободный текст: он идёт только хозяину (уведомлением), агенту — нет;
- агент читает само письмо тулом `mail_read` (ход заражается) или спрашивает Чтеца `mail_ask`;
- отправка — `mail_send` через Предохранитель (`msg.send`): не хозяину — с вопросом.

Опрос раз в `mail.poll_minutes`: короткое соединение, в простое ни записи, ни ЦП. Пароль — в сейфе (`mail:password`),
лучше пароль приложения: его можно отозвать, не меняя основного.
"""

from __future__ import annotations

import asyncio
import json
import re
import time

from ..core.errors import HumanError
from ..core.ids import new_id

SECRET = "mail:password"
STATE_META = "mail:state"
SCHEMA = [
    """CREATE TABLE IF NOT EXISTS mail (
        id TEXT PRIMARY KEY, dir TEXT NOT NULL DEFAULT 'in', uid INTEGER, msgid TEXT, from_addr TEXT, from_name TEXT, to_addr TEXT,
        subject TEXT, date REAL, body TEXT, attachments TEXT, verdict TEXT, summary TEXT, status TEXT NOT NULL DEFAULT 'new',
        task TEXT, created REAL NOT NULL)""",
    """CREATE UNIQUE INDEX IF NOT EXISTS mail_uid ON mail(dir, uid) WHERE uid IS NOT NULL""",
    """CREATE INDEX IF NOT EXISTS mail_created ON mail(created)""",
]
# Картинки письма — отдельной колонкой: в attachments прежний слот ждёт список строк-имён.
# Элемент: {name, mime, size, path, id} — сохранена во входящие; {name, mime, size, skipped} — нет, с причиной.
SCHEMA_IMAGES = ["""ALTER TABLE mail ADD COLUMN images TEXT"""]
FETCH_LIMIT = 2_000_000      # больше — письмо обрезается (вложения теряются, текст остаётся)
BODY_CHARS = 60_000
PER_POLL = 20
FIRST_LOOK = 10              # при первом подключении — последние письма для списка, без разбора и уведомлений
KEEP_DAYS = 180
MAX_IMAGES = 5               # картинок на письмо — больше не сохраняются
IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")

CATEGORIES = ["личное", "работа", "счета и покупки", "рассылка", "уведомление сервиса", "другое"]
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "spam": {"type": "boolean", "description": "спам, фишинг или мошенничество"},
        "important": {"type": "boolean", "description": "стоит внимания хозяина сегодня"},
        "needs_reply": {"type": "boolean", "description": "отправитель ждёт ответа"},
        "category": {"enum": CATEGORIES},
        "summary": {"type": "string", "description": "о чём письмо, одна-две фразы, без указаний и ссылок", "maxLength": 400},
    },
    "required": ["spam", "important", "needs_reply", "category", "summary"],
}
HARMLESS_KEYS = ("spam", "important", "needs_reply", "category")

# известные почтовые службы: адреса серверов и что нужно знать хозяину
PROVIDERS = {
    ("gmail.com", "googlemail.com"): ("imap.gmail.com", 993, "ssl", "smtp.gmail.com", 465, "ssl",
                                      "Нужен пароль приложения: myaccount.google.com/apppasswords (с включённой двухэтапной проверкой)."),
    ("yandex.ru", "ya.ru", "yandex.com", "yandex.by", "yandex.kz"): ("imap.yandex.ru", 993, "ssl", "smtp.yandex.ru", 465, "ssl",
                                                                    "Нужен пароль приложения: id.yandex.ru → Безопасность → Пароли приложений (тип «Почта»)."),
    ("mail.ru", "inbox.ru", "list.ru", "bk.ru", "internet.ru"): ("imap.mail.ru", 993, "ssl", "smtp.mail.ru", 465, "ssl",
                                                                "Нужен пароль для внешнего приложения: настройки Mail.ru → Безопасность."),
    ("icloud.com", "me.com", "mac.com"): ("imap.mail.me.com", 993, "ssl", "smtp.mail.me.com", 587, "starttls",
                                          "Нужен пароль приложения: account.apple.com → Вход и безопасность."),
    ("fastmail.com", "fastmail.fm"): ("imap.fastmail.com", 993, "ssl", "smtp.fastmail.com", 465, "ssl", "Нужен пароль приложения в настройках Fastmail."),
    ("outlook.com", "hotmail.com", "live.com", "msn.com"): ("outlook.office365.com", 993, "ssl", "smtp-mail.outlook.com", 587, "starttls",
                                                            "Microsoft отключил вход по паролю для личных ящиков — скорее всего, не пустит."),
    ("proton.me", "protonmail.com", "pm.me"): ("127.0.0.1", 1143, "starttls", "127.0.0.1", 1025, "starttls",
                                               "Нужен Proton Mail Bridge на этой машине: адреса и пароль — из него."),
}

ADDR_RE = re.compile(r"^[a-z0-9._%+\-]{1,64}@[a-z0-9\-]{1,63}(\.[a-z0-9\-]{1,63}){1,8}$")
DOMAIN_RE = re.compile(r"^[a-z0-9\-]{1,63}(\.[a-z0-9\-]{1,63}){1,8}$")


class MailError(HumanError):
    pass


def guess(address: str) -> dict:
    """Серверы по адресу: известная служба — её адреса и подсказка, иначе imap./smtp. домена."""
    dom = address.rsplit("@", 1)[-1].strip().lower()
    for doms, (ih, ip, isec, sh, sp, ssec, hint) in PROVIDERS.items():
        if dom in doms:
            return {"imap_host": ih, "imap_port": ip, "imap_security": isec, "smtp_host": sh, "smtp_port": sp, "smtp_security": ssec, "hint": hint, "known": True}
    return {"imap_host": f"imap.{dom}" if dom else "", "imap_port": 993, "imap_security": "ssl", "smtp_host": f"smtp.{dom}" if dom else "",
            "smtp_port": 465, "smtp_security": "ssl", "hint": "Серверы угаданы по домену — проверь у своей почтовой службы.", "known": False}


def _addr(raw: str) -> tuple[str, str]:
    from email.utils import parseaddr  # почтовые модули — лениво: в простое без почты они не нужны (≈2 МБ)

    name, addr = parseaddr(raw or "")
    return name.strip()[:200], addr.strip().lower()[:320]


def names(items) -> list[str]:
    """Имена вложений из записи любого слота: строки (как всегда) или объекты {name, …}."""
    out = []
    for a in items if isinstance(items, list) else []:
        n = a.get("name") if isinstance(a, dict) else a
        if n:
            out.append(str(n))
    return out


def _images_of(msg) -> list[dict]:
    """Картинки-вложения по содержимому (не по заявленному типу): {name, mime, raw}. Лишние тоже — чтобы сказать, что не сохранены."""
    from .media import mime

    out = []
    for part in msg.walk():
        if len(out) >= 30:
            break
        try:
            if part.is_multipart() or part.get_content_maintype() == "text":
                continue
            raw = part.get_payload(decode=True)
        except Exception:  # noqa: BLE001 — кривая часть письма
            continue
        if not isinstance(raw, bytes) or not raw:
            continue
        kind = mime(raw[:64], "")
        if kind not in IMAGE_TYPES:
            continue
        try:
            name = part.get_filename() or ""
        except Exception:  # noqa: BLE001
            name = ""
        out.append({"name": name or f"картинка-{len(out) + 1}.{kind.split('/')[1].replace('jpeg', 'jpg')}", "mime": kind, "raw": raw})
    return out


def _text_of(msg) -> str:
    from ..net.html2text import html_to_text

    try:
        part = msg.get_body(preferencelist=("plain", "html"))
    except Exception:  # noqa: BLE001 — кривые письма бывают
        part = None
    if part is None:
        return ""
    try:
        text = part.get_content()
    except Exception:  # noqa: BLE001 — неизвестная кодировка и т. п.
        raw = part.get_payload(decode=True) or b""
        if isinstance(raw,bytes):
            try:text=raw.decode(part.get_content_charset() or 'utf-8','replace')
            except LookupError:text=raw.decode('utf-8','replace')
        else:text=str(raw)
    if part.get_content_type() == "text/html":
        _, text = html_to_text(text, "")
    return re.sub(r"\n{3,}", "\n\n", text.replace("\r\n", "\n")).strip()


def parse(raw: bytes) -> dict:
    import email
    import email.policy
    from email.utils import parsedate_to_datetime

    if len(raw)>FETCH_LIMIT:raise MailError('mail_size','Письмо слишком велико','Проверь размер письма; предел разбора — 2 МБ.')
    try:msg = email.message_from_bytes(raw, policy=email.policy.default)
    except (ValueError,RecursionError) as exc:
        raise MailError('mail_format','Не удалось разобрать письмо','Сохрани исходник для проверки; другие письма продолжат обрабатываться.',detail=str(exc)) from None

    def h(name: str) -> str:
        try:
            return str(msg.get(name) or "")
        except Exception:  # noqa: BLE001
            return ""

    name, addr = _addr(h("From"))
    try:
        date = parsedate_to_datetime(h("Date")).timestamp()
    except Exception:  # noqa: BLE001
        date = time.time()
    atts = []
    try:
        atts = [p.get_filename() for p in msg.iter_attachments() if p.get_filename()][:30]
    except Exception:  # noqa: BLE001
        pass
    try:
        images = _images_of(msg)
    except Exception:  # noqa: BLE001
        images = []
    return {"images": images, "msgid": h("Message-ID").strip()[:500], "from_addr": addr, "from_name": name, "to_addr": h("To")[:500],
            "subject": re.sub(r"\s+", " ", h("Subject")).strip()[:500], "date": date, "body": _text_of(msg)[:BODY_CHARS], "attachments": atts}


def _quote(folder: str) -> str:
    return f'"{folder}"' if re.search(r'[\s"]', folder) and not folder.startswith('"') else folder


class Mail:
    name = "mail"

    def __init__(self, app):
        self.app = app
        app.store.migrate("mail", 1, SCHEMA)
        app.store.migrate("mail", 2, SCHEMA_IMAGES)
        self.task: asyncio.Task | None = None
        self.state = "off"              # off | ok | error | checking
        self.last_error: str | None = None
        self.last_check: float | None = None
        self._wake = asyncio.Event()
        self._listening = False
        self._bg: set[asyncio.Task] = set()

    # настройки

    def settings(self) -> dict:
        return self.app.settings.group("mail")

    def password(self) -> str | None:
        return self.app.secrets.get(SECRET)

    def login(self) -> str:
        s = self.settings()
        return (s.get("login") or s.get("address") or "").strip()

    def configured(self) -> bool:
        s = self.settings()
        return bool(s.get("address") and s.get("imap_host") and self.password())

    def enabled(self) -> bool:
        return bool(self.settings().get("enabled")) and self.configured() and not self.app.safe

    def owner_addresses(self) -> set[str]:
        s = self.settings()
        extra = s.get("owner_addresses") or []
        if isinstance(extra, str):
            extra = re.split(r"[\s,;]+", extra)
        return {a.strip().lower() for a in [s.get("address") or "", *extra] if a and "@" in a}

    def known(self) -> dict[str, str]:
        k = self.settings().get("known") or {}
        return {str(a).strip().lower(): str(v)[:60] for a, v in k.items()} if isinstance(k, dict) else {}

    def is_known(self, addr: str) -> bool:
        """Знакомый отправитель: адрес из списка хозяина (mail.known) или его собственный. Адрес в From можно подделать."""
        addr = (addr or "").strip().lower()
        return bool(addr) and (addr in self.known() or addr in self.owner_addresses())

    def sender_line(self, addr: str) -> str:
        """Кто прислал — так, чтобы строка не могла нести указаний: подпись хозяина или только домен."""
        addr = (addr or "").lower()
        if addr in self.owner_addresses():
            return "хозяин (адрес хозяина — но адрес отправителя можно подделать)"
        label = self.known().get(addr)
        if label:
            return f"«{label}» ({addr}, из списка хозяина)"
        dom = addr.rsplit("@", 1)[-1]
        return f"неизвестный отправитель (домен {dom})" if DOMAIN_RE.match(dom) else "неизвестный отправитель"

    def public(self) -> dict:
        s = self.settings()
        row = self.app.store.one("SELECT COUNT(*) AS n, SUM(status = 'new') AS fresh FROM mail WHERE dir = 'in'")
        counts = dict(row) if row else {}
        return {"enabled": bool(s.get("enabled")), "configured": self.configured(), "has_password": bool(self.password()), "state": self.state,
                "error": self.last_error, "last_check": self.last_check, "settings": s, "count": counts.get("n") or 0, "new": counts.get("fresh") or 0,
                "guess": guess(s.get("address") or "") if s.get("address") else None}

    # соединения (синхронные — в отдельном потоке)

    def _ctx(self):
        import ssl

        return ssl.create_default_context()

    def _imap(self):
        import imaplib

        s = self.settings()
        host, port, sec = s.get("imap_host") or "", int(s.get("imap_port") or 993), s.get("imap_security") or "ssl"
        try:
            if sec == "ssl":
                m = imaplib.IMAP4_SSL(host, port, ssl_context=self._ctx(), timeout=30)
            else:
                m = imaplib.IMAP4(host, port, timeout=30)
                if sec == "starttls":
                    m.starttls(ssl_context=self._ctx())
        except (OSError, imaplib.IMAP4.error) as e:
            raise _net_error("IMAP", host, port, e) from None
        try:
            m.login(self.login(), self.password() or "")
        except imaplib.IMAP4.error as e:
            _close(m)
            raise MailError("mail_auth", "Почта не приняла логин или пароль", guess(s.get("address") or "")["hint"], detail=str(e)[:300]) from None
        return m

    def _smtp(self):
        import smtplib

        s = self.settings()
        host, port, sec = s.get("smtp_host") or "", int(s.get("smtp_port") or 465), s.get("smtp_security") or "ssl"
        if not host:
            raise MailError("mail_no_smtp", "Не задан сервер отправки (SMTP)", "Впиши его в «Связь → Почта».")
        try:
            if sec == "ssl":
                c = smtplib.SMTP_SSL(host, port, context=self._ctx(), timeout=30)
            else:
                c = smtplib.SMTP(host, port, timeout=30)
                if sec == "starttls":
                    c.starttls(context=self._ctx())
        except (OSError, smtplib.SMTPException) as e:
            raise _net_error("SMTP", host, port, e) from None
        try:
            c.login(self.login(), self.password() or "")
        except smtplib.SMTPException as e:
            try:
                c.quit()
            except Exception:  # noqa: BLE001
                pass
            raise MailError("mail_auth", "Сервер отправки не принял логин или пароль", guess(s.get("address") or "")["hint"], detail=str(e)[:300]) from None
        return c

    def _check_sync(self) -> dict:
        out = {"imap": None, "smtp": None}
        m = self._imap()
        try:
            folder = self.settings().get("folder") or "INBOX"
            typ, data = m.status(_quote(folder), "(MESSAGES UIDNEXT)")
            if typ != "OK":
                raise MailError("mail_folder", f"Нет папки «{folder}»", "Обычно входящие — INBOX.")
            n = re.search(rb"MESSAGES (\d+)", data[0] or b"")
            out["imap"] = {"messages": int(n.group(1)) if n else None}
        finally:
            _close(m)
        if self.settings().get("smtp_host"):
            c = self._smtp()
            try:
                c.noop()
                out["smtp"] = {"ok": True}
            finally:
                try:
                    c.quit()
                except Exception:  # noqa: BLE001
                    pass
        return out

    async def check(self) -> dict:
        """Проверить вход в IMAP и SMTP, ничего не читая и не отправляя."""
        return await asyncio.to_thread(self._check_sync)

    def _fetch_sync(self, state: dict) -> tuple[list[dict], dict, bool]:
        """Новые письма после state['last_uid']. Возвращает (письма, новое состояние, первый_взгляд)."""
        folder = self.settings().get("folder") or "INBOX"
        m = self._imap()
        try:
            typ, data = m.status(_quote(folder), "(UIDVALIDITY UIDNEXT)")
            if typ != "OK":
                raise MailError("mail_folder", f"Нет папки «{folder}»", "Обычно входящие — INBOX.")
            raw = data[0] or b""
            uv = int((re.search(rb"UIDVALIDITY (\d+)", raw) or [0, b"0"])[1])
            nxt = int((re.search(rb"UIDNEXT (\d+)", raw) or [0, b"0"])[1])
            typ, _ = m.select(_quote(folder), readonly=True)
            if typ != "OK":
                raise MailError("mail_folder", f"Не открылась папка «{folder}»", "")
            first = not state or state.get("uidvalidity") != uv
            if first:  # первый раз (или ящик пересоздан): не разбирать весь архив, только последние письма для списка
                lo = max(1, nxt - 500) if nxt else 1
                uids = self._search(m, f"UID {lo}:*")
                uids = uids[-FIRST_LOOK:]
                last = max([nxt - 1, *uids]) if nxt else max(uids, default=0)
            else:
                last0 = int(state.get("last_uid") or 0)
                uids = [u for u in self._search(m, f"UID {last0 + 1}:*") if u > last0][:PER_POLL]
                last = max([last0, *uids])
            letters = []
            for uid in uids:
                typ, data = m.uid("FETCH", str(uid), f"(UID BODY.PEEK[]<0.{FETCH_LIMIT}>)")
                body = next((x[1] for x in data or [] if isinstance(x, tuple) and len(x) > 1), None)
                if typ != "OK" or body is None:
                    continue
                try:
                    letter = parse(body)
                except Exception as e:  # noqa: BLE001 — одно кривое письмо не останавливает почту
                    letter = {"msgid": "", "from_addr": "", "from_name": "", "to_addr": "", "subject": "(не разобралось)", "date": time.time(),
                              "body": f"Письмо не разобралось: {e}", "attachments": [], "images": []}
                letter["uid"] = uid
                if len(body) >= FETCH_LIMIT:  # письмо обрезано: картинки могут быть неполными — модели битый файл не отдаём
                    letter["truncated"] = True
                letters.append(letter)
            return letters, {"uidvalidity": uv, "last_uid": last}, first
        finally:
            _close(m)

    @staticmethod
    def _search(m, criteria: str) -> list[int]:
        typ, data = m.uid("SEARCH", None, criteria)
        if typ != "OK":
            return []
        return sorted(int(x) for x in (data[0] or b"").split() if x.isdigit())

    # жизнь

    def start(self) -> None:
        if not self._listening:
            self.app.bus.listen(self._on_settings)
            self._listening = True
        if self.enabled() and not (self.task and not self.task.done()):
            self.task = asyncio.ensure_future(self._run())

    async def stop(self) -> None:
        for t in list(self._bg):
            t.cancel()
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self.task = None
        self.state = "off"

    async def restart(self) -> None:
        await self.stop()
        self.start()

    def poke(self) -> None:
        """Проверить почту сейчас, не дожидаясь срока."""
        self._wake.set()

    def _on_settings(self, ev: dict) -> None:
        if ev.get("type") == "settings" and ev.get("group") == "mail":
            self._restarting = asyncio.ensure_future(self._safe(self.restart()))

    async def _safe(self, coro) -> None:
        try:
            await coro
        except Exception as e:  # noqa: BLE001
            self.last_error = str(getattr(e, "title", "") or e)[:300]

    async def _run(self) -> None:
        delay = 60.0
        while True:
            wait = max(60.0, float(self.settings().get("poll_minutes") or 5) * 60)
            try:
                await self.poll()
                delay = 60.0
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                self.state = "error"
                self.last_error = (f"{e.title}. {e.hint}".strip() if isinstance(e, HumanError) else str(e))[:300]
                wait = delay if not (isinstance(e, HumanError) and e.code == "mail_auth") else 3600
                delay = min(delay * 2, 1800)
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), wait)
            except asyncio.TimeoutError:
                pass

    async def poll(self) -> list[dict]:
        """Один опрос: новые письма — в базу, на разбор Чтецу и (по настройке) хозяину или агенту."""
        app = self.app
        self.state = "checking"
        state = app.store.meta_get(STATE_META) or {}
        letters, new_state, first = await asyncio.to_thread(self._fetch_sync, state if isinstance(state, dict) else {})
        self.state, self.last_error, self.last_check = "ok", None, time.time()
        if new_state != state:
            app.store.meta_set(STATE_META, new_state)
        saved = []
        for L in letters:
            mid = new_id()
            try:
                app.store.x("""INSERT INTO mail(id, dir, uid, msgid, from_addr, from_name, to_addr, subject, date, body, attachments, status, created)
                               VALUES(?, 'in', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (mid, L["uid"], L["msgid"], L["from_addr"], L["from_name"], L["to_addr"], L["subject"], L["date"], L["body"],
                             json.dumps(L["attachments"], ensure_ascii=False), "old" if first else "new", time.time()))
            except Exception:  # noqa: BLE001 — уже есть (повторный опрос после сбоя)
                continue
            if L.get("images"):
                try:
                    images = await self.save_images(L, first=first)
                except Exception as e:  # noqa: BLE001 — картинка не останавливает почту: письмо всё равно дойдёт до разбора
                    images = [{"name": "картинки", "skipped": "не удалось сохранить картинки письма"}]
                    app.journal.write("channel", "Почта: картинки письма не сохранены", actor="mail", data={"error": type(e).__name__})
                app.store.x("UPDATE mail SET images = ? WHERE id = ?", (json.dumps(images, ensure_ascii=False), mid))
            saved.append(self.get(mid))
        if saved:
            app.journal.write("channel", f"Почта: {'первый взгляд, писем' if first else 'новых писем'} {len(saved)}", actor="mail")
            app.bus.publish({"type": "channel", "channel": "mail", "new": 0 if first else len(saved)})
        if not first:
            for L in saved:
                t = asyncio.ensure_future(self._safe(self._incoming(L)))
                self._bg.add(t)
                t.add_done_callback(self._bg.discard)
        app.store.x("DELETE FROM mail WHERE created < ?", (time.time() - KEEP_DAYS * 86400,))
        return saved

    async def save_images(self, L: dict, *, first: bool = False) -> list[dict]:
        """Картинки письма — во входящие (по настройке mail.images). Запись — через Предохранитель (fs.write) от имени почты,
        без хозяина рядом: папка входящих внутри рабочей разрешена профилем, карточек нет; запрет — честная причина в записи."""
        from . import media

        mode = self.settings().get("images") or "known"
        cfg = self.app.settings.group("attachments")
        limit = min(media.DOWNLOAD_LIMIT, int(cfg.get("max_mb") or 20) * 1024 * 1024)
        why = None
        if first:
            why = "письмо пришло до подключения ящика (первый взгляд — только список)"
        elif L.get("truncated"):
            why = "письмо больше 2 МБ и обрезано — картинка могла оборваться"
        elif mode == "off":
            why = "картинки из писем выключены (Почта → Картинки из писем)"
        elif mode == "known" and not self.is_known(L.get("from_addr") or ""):
            why = "отправитель неизвестен (картинки сохраняются только от знакомых адресов)"
        elif not cfg.get("enabled", True):
            why = "вложения выключены"
        out = []
        for n, img in enumerate(L.get("images") or []):
            item = {"name": media.filename(img["name"]), "mime": img["mime"], "size": len(img["raw"])}
            reason = why or (f"больше {MAX_IMAGES} картинок в письме" if n >= MAX_IMAGES else
                             "больше лимита вложений" if len(img["raw"]) > limit else None)
            if reason is None:
                try:
                    saved = await media.save(self.app, img["name"], img["raw"], kind="photo", source="mail", actor="mail", unattended=True,
                                             why=f"сохранить картинку из письма от {(L.get('from_addr') or '?')[:80]}")
                    item.update(path=saved["path"], id=saved["id"])
                except HumanError as e:
                    reason = f"{e.title}. {e.hint}".strip(". ")[:200]
                except OSError:
                    reason = "не удалось записать файл во входящие"
            if reason:
                item["skipped"] = reason
            out.append(item)
        if any("path" in i for i in out):
            self.app.journal.write("channel", f"Почта: картинок сохранено {sum('path' in i for i in out)}", actor="mail")
        return out

    def image_lines(self, L: dict) -> list[str]:
        out = []
        for i in L.get("images") or []:
            if not isinstance(i, dict):
                continue
            out.append(f"{i.get('name') or 'картинка'} — " + ("сохранена во входящие" if i.get("path") else f"картинка не сохранена: {i.get('skipped') or 'причина неизвестна'}"))
        return out

    async def _incoming(self, L: dict) -> None:
        verdict = await self.triage(L) if self.settings().get("triage", True) else None
        await self.react(self.get(L["id"]) or L, verdict)

    # Чтец

    async def triage(self, L: dict) -> dict | None:
        """Чтец разбирает письмо по схеме. В verdict — только безвредные поля (да/нет, категория); пересказ — отдельно, для хозяина."""
        app = self.app
        if not app.settings.get("team", "enabled", True):
            return None
        from ..team.schema import harmless, validate
        from ..tools.web import untrusted

        t = app.team.spawn("reader", "Разбери письмо: спам ли, важно ли хозяину, ждёт ли отправитель ответа, категория и о чём оно.",
                           context=untrusted(f"письмо #{L['id'][-6:]}", self.render(L, for_reader=True)), schema=VERDICT_SCHEMA, tainted=True, unattended=True, budget={"steps": 2})
        self.app.store.x("UPDATE mail SET task = ? WHERE id = ?", (t["id"], L["id"]))
        done = (await app.team.wait([t["id"]], timeout=300))[0]
        data = done.get("data") if done.get("status") == "done" and done.get("schema_valid") else None
        if not isinstance(data, dict):
            return None
        sub_schema = {"type": "object", "properties": {k: VERDICT_SCHEMA["properties"][k] for k in HARMLESS_KEYS}, "required": list(HARMLESS_KEYS)}
        verdict = {k: data.get(k) for k in HARMLESS_KEYS}
        if validate(verdict, sub_schema) or not harmless(verdict, sub_schema):
            return None
        summary = str(data.get("summary") or "")[:400]
        app.store.x("UPDATE mail SET verdict = ?, summary = ? WHERE id = ?", (json.dumps(verdict, ensure_ascii=False), summary, L["id"]))
        return verdict

    async def react(self, L: dict, verdict: dict | None) -> None:
        app = self.app
        mode = self.settings().get("on_new") or "important"
        v = verdict or {}
        if mode == "quiet" or v.get("spam"):
            return
        if mode in ("important", "all"):
            if mode == "important" and verdict is not None and not v.get("important"):
                return
            who = L.get("from_name") or L.get("from_addr") or "?"
            text = f"📧 {'Важное письмо' if v.get('important') else 'Письмо'} от {who}: «{L.get('subject') or 'без темы'}»" + (f"\n{L['summary']}" if L.get("summary") else "") \
                + ("\nЖдёт ответа." if v.get("needs_reply") else "")
            sid = app.main_session()
            msg = {"role": "event", "kind": "mail", "content": text}  # событие: в модель не уходит, в память не индексируется
            app.store.add_message(sid, msg, {"mail": L["id"]})
            app.bus.publish({"session": sid, "type": "message", "message": msg})
            app.bus.publish({"type": "notify", "kind": "mail", "title": "Почта", "text": text, "mail": L["id"]})
            return
        if mode == "agent":
            # агенту — только то, что не может нести указаний: номер, отправитель по списку хозяина или домен, разбор Чтеца
            parts = [f"[Новое письмо #{L['id'][-6:]} от: {self.sender_line(L.get('from_addr') or '')}"]
            if verdict is not None:
                parts.append("Чтец: " + ", ".join([f"важное — {'да' if v['important'] else 'нет'}", f"ждёт ответа — {'да' if v['needs_reply'] else 'нет'}",
                                                   f"категория — {v['category']}"]))
            else:
                parts.append("Чтец письмо не разбирал")
            parts.append("Само письмо — тулом mail_read (ход станет заражённым) или спроси Чтеца: mail_ask. Хозяину пиши, только если это стоит его внимания.]")
            await app.mind.say(app.main_session(), "\n".join(parts), channel="mail", actor="mail", tainted=False, unattended=True, wait=False)

    # письма

    def get(self, mid: str) -> dict | None:
        r = self.app.store.one("SELECT * FROM mail WHERE id = ?", (mid,))
        if not r:
            r = self.app.store.one("SELECT * FROM mail WHERE id LIKE ? ORDER BY created DESC LIMIT 1", ("%" + mid.lstrip("#"),)) if len(mid.lstrip("#")) >= 4 else None
        if not r:
            return None
        d = dict(r)
        for k in ("attachments", "verdict", "images"):
            try:
                d[k] = json.loads(d[k]) if d.get(k) else (None if k == "verdict" else [])
            except ValueError:
                d[k] = None if k == "verdict" else []
        return d

    def recent(self, limit: int = 30, *, direction: str = "in", status: str | None = None) -> list[dict]:
        rows = self.app.store.q("SELECT id FROM mail WHERE dir = ?" + (" AND status = ?" if status else "") + " ORDER BY date DESC LIMIT ?",
                                (direction, status, limit) if status else (direction, limit))
        return [self.get(r["id"]) for r in rows]

    def render(self, L: dict, *, for_reader: bool = False, limit: int = 20000) -> str:
        atts = ", ".join(names(L.get("attachments")))
        pics = "; ".join(self.image_lines(L))
        head = (f"От: {L.get('from_name') or ''} <{L.get('from_addr') or ''}>\nКому: {L.get('to_addr') or ''}\nТема: {L.get('subject') or ''}\n"
                f"Дата: {time.strftime('%d.%m.%Y %H:%M', time.localtime(L.get('date') or 0))}" + (f"\nВложения: {atts}" if atts else "")
                + (f"\nКартинки: {pics}" if pics else ""))
        return f"{head}\n\n{(L.get('body') or '')[:limit]}"

    def mark(self, mid: str, status: str) -> None:
        self.app.store.x("UPDATE mail SET status = ? WHERE id = ?", (status, mid))

    async def send(self, to: str, subject: str, text: str, *, reply_to: dict | None = None) -> dict:
        import smtplib
        from email.message import EmailMessage
        from email.utils import formataddr, formatdate, make_msgid

        s = self.settings()
        frm = (s.get("address") or "").strip()
        if not frm:
            raise MailError("mail_off", "Почта не подключена", "Подключи ящик в «Связь → Почта».")
        _, rcpt = _addr(to)
        if not ADDR_RE.match(rcpt):
            raise MailError("mail_bad_to", f"Странный адрес «{to[:80]}»", "Нужен адрес вида имя@домен.")
        em = EmailMessage()
        em["From"] = formataddr((s.get("name") or self.app.selfdir.identity().get("name") or "", frm))
        em["To"] = rcpt
        if reply_to and not subject:
            subject = reply_to.get("subject") or ""
        if reply_to and not subject.lower().startswith("re:"):
            subject = "Re: " + subject
        em["Subject"] = subject
        em["Date"] = formatdate(localtime=True)
        em["Message-ID"] = make_msgid(domain=frm.rsplit("@", 1)[-1])
        if reply_to and reply_to.get("msgid"):
            em["In-Reply-To"] = reply_to["msgid"]
            em["References"] = reply_to["msgid"]
        em.set_content(text)

        def go():
            c = self._smtp()
            try:
                c.send_message(em)
            finally:
                try:
                    c.quit()
                except Exception:  # noqa: BLE001
                    pass

        try:
            await asyncio.to_thread(go)
        except smtplib.SMTPException as e:
            raise MailError("mail_send", "Письмо не ушло", str(e)[:300]) from None
        mid = new_id()
        self.app.store.x("""INSERT INTO mail(id, dir, msgid, from_addr, to_addr, subject, date, body, status, created)
                            VALUES(?, 'out', ?, ?, ?, ?, ?, ?, 'sent', ?)""", (mid, em["Message-ID"], frm, rcpt, subject, time.time(), text[:BODY_CHARS], time.time()))
        if reply_to:
            self.mark(reply_to["id"], "answered")
        self.app.journal.write("channel", f"Почта: письмо {rcpt} «{subject[:80]}»", actor="agent", data={"mail": mid})
        return self.get(mid)


def _close(m) -> None:
    try:
        m.logout()
    except Exception:  # noqa: BLE001
        pass


def _net_error(proto: str, host: str, port: int, e: Exception) -> MailError:
    import socket
    import ssl

    if isinstance(e, ssl.SSLError):
        return MailError("mail_tls", f"{proto}: не сложилось шифрование с {host}:{port}", "Проверь порт и защиту (SSL на 993/465, STARTTLS на 143/587).", detail=str(e)[:300])
    if isinstance(e, socket.gaierror):
        return MailError("mail_net", f"{proto}: нет такого сервера {host}", "Проверь адрес сервера.", detail=str(e)[:300])
    if isinstance(e, (TimeoutError, socket.timeout)):
        return MailError("mail_net", f"{proto}: {host}:{port} не отвечает", "Проверь порт и сеть.", detail=str(e)[:300])
    return MailError("mail_net", f"{proto}: нет связи с {host}:{port}", str(e)[:200])
