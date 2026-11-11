"""Фальшивые IMAP и SMTP для тестов почты — без TLS, на asyncio, только то, что нужно Бендеру
(imaplib: CAPABILITY, LOGIN, STATUS, EXAMINE/SELECT, UID SEARCH, UID FETCH, NOOP, LOGOUT;
smtplib: EHLO, AUTH PLAIN, MAIL, RCPT, DATA, NOOP, RSET, QUIT).

`bender fake-llm` поднимает их рядом с фальшивыми моделями (IMAP — порт+1, SMTP — порт+2); письма кладутся
POST /fake/mail/add, отправленное видно в GET /fake/mail/sent.
"""

from __future__ import annotations

import asyncio
import base64
import re
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid


class Mailbox:
    def __init__(self, user: str = "owner@example.com", password: str = "secret", uidvalidity: int = 777):
        self.user = user
        self.password = password
        self.uidvalidity = uidvalidity
        self.letters: list[tuple[int, bytes]] = []
        self.next_uid = 1
        self.sent: list[dict] = []
        self.seen_set = 0          # сколько раз письмо пометили прочитанным (FETCH без PEEK, STORE)
        self.logins = 0
        self.commands: list[str] = []

    def add(self, raw: bytes | None = None, *, frm: str = "Вася <vasya@example.org>", subject: str = "Привет", text: str = "Текст письма",
            html: str | None = None) -> int:
        if raw is None:
            m = EmailMessage()
            m["From"] = frm
            m["To"] = self.user
            m["Subject"] = subject
            m["Date"] = formatdate(time.time(), localtime=True)
            m["Message-ID"] = make_msgid(domain="example.org")
            m.set_content(text)
            if html:
                m.add_alternative(html, subtype="html")
            raw = bytes(m)
        uid = self.next_uid
        self.next_uid += 1
        self.letters.append((uid, raw))
        return uid


def _args(line: str) -> list[str]:
    """Разбить аргументы IMAP: атомы, "строки в кавычках", (списки) — целиком."""
    out, i = [], 0
    while i < len(line):
        c = line[i]
        if c == " ":
            i += 1
            continue
        if c == '"':
            j, buf = i + 1, []
            while j < len(line) and line[j] != '"':
                if line[j] == "\\" and j + 1 < len(line):
                    j += 1
                buf.append(line[j])
                j += 1
            out.append("".join(buf))
            i = j + 1
        elif c == "(":
            depth, j = 0, i
            while j < len(line):
                depth += line[j] == "("
                depth -= line[j] == ")"
                j += 1
                if depth == 0:
                    break
            out.append(line[i:j])
            i = j
        else:
            j = line.find(" ", i)
            j = len(line) if j < 0 else j
            out.append(line[i:j])
            i = j
    return out


def _uid_set(spec: str, uids: list[int]) -> list[int]:
    top = max(uids, default=0)
    out = set()
    for part in spec.split(","):
        if ":" in part:
            a, b = part.split(":", 1)
            lo = int(a) if a != "*" else top
            hi = int(b) if b != "*" else top
            lo, hi = min(lo, hi), max(lo, hi)
            out.update(u for u in uids if lo <= u <= hi)
        elif part == "*":
            if top:
                out.add(top)
        elif part.isdigit() and int(part) in uids:
            out.add(int(part))
    return sorted(out)


async def _imap_client(box: Mailbox, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    def w(s: str | bytes) -> None:
        writer.write(s if isinstance(s, bytes) else (s + "\r\n").encode())

    w("* OK [CAPABILITY IMAP4rev1 AUTH=PLAIN] fake IMAP ready")
    authed = False
    try:
        while True:
            raw = await reader.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            box.commands.append(line)
            tag, _, rest = line.partition(" ")
            cmd, _, arg = rest.partition(" ")
            cmd = cmd.upper()
            uids = [u for u, _ in box.letters]
            if cmd == "CAPABILITY":
                w("* CAPABILITY IMAP4rev1 AUTH=PLAIN")
                w(f"{tag} OK done")
            elif cmd == "LOGIN":
                a = _args(arg)
                if len(a) >= 2 and a[0] == box.user and a[1] == box.password:
                    authed = True
                    box.logins += 1
                    w(f"{tag} OK logged in")
                else:
                    w(f"{tag} NO [AUTHENTICATIONFAILED] Invalid credentials")
            elif cmd in ("NOOP",):
                w(f"{tag} OK done")
            elif cmd == "LOGOUT":
                w("* BYE")
                w(f"{tag} OK bye")
                await writer.drain()
                break
            elif not authed:
                w(f"{tag} BAD login first")
            elif cmd == "STATUS":
                a = _args(arg)
                if a and a[0].upper() != "INBOX":
                    w(f"{tag} NO no such folder")
                else:
                    w(f"* STATUS INBOX (MESSAGES {len(uids)} UIDVALIDITY {box.uidvalidity} UIDNEXT {box.next_uid})")
                    w(f"{tag} OK done")
            elif cmd in ("EXAMINE", "SELECT"):
                a = _args(arg)
                if a and a[0].upper() != "INBOX":
                    w(f"{tag} NO no such folder")
                else:
                    w(f"* {len(uids)} EXISTS")
                    w(f"* OK [UIDVALIDITY {box.uidvalidity}] ok")
                    w(f"* OK [UIDNEXT {box.next_uid}] ok")
                    w(f"{tag} OK [{'READ-ONLY' if cmd == 'EXAMINE' else 'READ-WRITE'}] done")
            elif cmd == "UID":
                sub, _, sarg = arg.partition(" ")
                sub = sub.upper()
                if sub == "SEARCH":
                    m = re.search(r"UID (\S+)", sarg, re.I)
                    found = _uid_set(m.group(1), uids) if m else uids
                    w("* SEARCH" + "".join(f" {u}" for u in found))
                    w(f"{tag} OK done")
                elif sub == "FETCH":
                    spec, _, items = sarg.partition(" ")
                    if "BODY[" in items.upper() and "PEEK" not in items.upper():
                        box.seen_set += 1
                    lim = re.search(r"<0\.(\d+)>", items)
                    for u in _uid_set(spec, uids):
                        seq = uids.index(u) + 1
                        body = dict(box.letters)[u]
                        if lim:
                            body = body[: int(lim.group(1))]
                        w(f"* {seq} FETCH (UID {u} BODY[]{'<0>' if lim else ''} {{{len(body)}}}".encode() + b"\r\n" + body + b")\r\n")
                    w(f"{tag} OK done")
                elif sub == "STORE":
                    box.seen_set += 1
                    w(f"{tag} OK done")
                else:
                    w(f"{tag} BAD unknown")
            else:
                w(f"{tag} BAD unknown command")
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()


async def _smtp_client(box: Mailbox, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    def w(s: str) -> None:
        writer.write((s + "\r\n").encode())

    w("220 fake SMTP ready")
    authed, frm, rcpt = False, None, []
    try:
        while True:
            raw = await reader.readline()
            if not raw:
                break
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            up = line.upper()
            if up.startswith(("EHLO", "HELO")):
                w("250-fake")
                w("250-AUTH PLAIN")
                w("250 OK")
            elif up.startswith("AUTH PLAIN"):
                parts = line.split()
                tok = parts[2] if len(parts) > 2 else ""
                try:
                    _, user, pw = base64.b64decode(tok).decode().split("\0")
                except Exception:  # noqa: BLE001
                    user = pw = ""
                if user == box.user and pw == box.password:
                    authed = True
                    w("235 ok")
                else:
                    w("535 bad credentials")
            elif up.startswith("MAIL FROM"):
                if not authed:
                    w("530 auth first")
                else:
                    frm = re.search(r"<([^>]*)>", line).group(1) if "<" in line else ""
                    rcpt = []
                    w("250 ok")
            elif up.startswith("RCPT TO"):
                rcpt.append(re.search(r"<([^>]*)>", line).group(1) if "<" in line else "")
                w("250 ok")
            elif up == "DATA":
                w("354 go")
                await writer.drain()
                buf = b""
                while True:
                    chunk = await reader.readline()
                    if not chunk or chunk in (b".\r\n", b".\n"):
                        break
                    buf += chunk[1:] if chunk.startswith(b"..") else chunk
                box.sent.append({"from": frm, "to": rcpt, "raw": buf.decode("utf-8", "replace")})
                w("250 queued")
            elif up in ("NOOP", "RSET"):
                w("250 ok")
            elif up == "QUIT":
                w("221 bye")
                await writer.drain()
                break
            else:
                w("502 unknown")
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError):
        pass
    finally:
        writer.close()


class FakeMail:
    """IMAP и SMTP на 127.0.0.1 с одним ящиком."""

    def __init__(self, box: Mailbox | None = None):
        self.box = box or Mailbox()
        self.imap: asyncio.base_events.Server | None = None
        self.smtp: asyncio.base_events.Server | None = None

    async def start(self, imap_port: int = 0, smtp_port: int = 0, host: str = "127.0.0.1") -> tuple[int, int]:
        self.imap = await asyncio.start_server(lambda r, w: _imap_client(self.box, r, w), host, imap_port)
        self.smtp = await asyncio.start_server(lambda r, w: _smtp_client(self.box, r, w), host, smtp_port)
        return self.imap.sockets[0].getsockname()[1], self.smtp.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        for s in (self.imap, self.smtp):
            if s:
                s.close()
                try:
                    await asyncio.wait_for(s.wait_closed(), 2)
                except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                    pass
