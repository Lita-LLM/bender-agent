"""Картинки из писем: по содержимому, только от знакомых (по умолчанию), во входящие через Предохранитель без карточек,
при чтении письма — модели отдельной частью; старые записи читаются; агент не расширяет настройку сам."""

import base64
import json
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

from bender.channels.mail import parse
from bender.core.settings import NeedsOwner
from bender.router.probes import png
from tests.helpers import TempDirCase, async_test
from tests.test_mail import MailEnv
from tests.test_mind import call


def letter(frm: str, *images: tuple[str, bytes, str], text: str = "Смотри картинку.") -> bytes:
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = frm, "owner@example.com", "Фото"
    m["Date"], m["Message-ID"] = formatdate(localtime=True), make_msgid(domain="example.org")
    m.set_content(text)
    for name, raw, declared in images:
        main, sub = declared.split("/")
        m.add_attachment(raw, maintype=main, subtype=sub, filename=name)
    return bytes(m)


class MailImagesTest(TempDirCase):
    def test_parse_by_content_not_declared_type(self):
        raw = png((255, 0, 0))
        L = parse(letter("a@b.example", ("отчёт.pdf", raw, "application/pdf"), ("не-картинка.png", b"%PDF-1.4 hello", "image/png")))
        self.assertEqual(L["attachments"], ["отчёт.pdf", "не-картинка.png"])  # имена — как раньше, строками
        self.assertEqual([(i["name"], i["mime"]) for i in L["images"]], [("отчёт.pdf", "image/png")])

    @async_test
    async def test_known_sender_png_saved_and_shown_to_model(self):
        async with MailEnv(self.tmp) as env:
            app = env.app
            app.settings.set("mail", {"triage": False, "on_new": "quiet", "known": {"vasya@example.org": "Вася"}})
            await app.mail.poll()
            raw = png((0, 128, 255))
            env.box.add(letter("Вася <vasya@example.org>", ("фото.png", raw, "application/octet-stream")))
            n, seen, check = len(env.approvals), [], app.guard.check

            async def spy(a, **kw):
                seen.append(a)
                return await check(a, **kw)

            app.guard.check = spy
            got = await app.mail.poll()
            app.guard.check = check
            await env.settle()
            self.assertEqual(len(env.approvals), n)  # фоновая запись во входящие — без карточек хозяину
            L = app.mail.get(got[0]["id"])
            self.assertEqual(L["attachments"], ["фото.png"])
            [img] = L["images"]
            self.assertEqual((img["name"], img["mime"], img["size"]), ("фото.png", "image/png", len(raw)))
            path = Path(img["path"])
            self.assertTrue(path.is_relative_to(app.dirs.work / "inbox"))
            self.assertEqual(path.read_bytes(), raw)
            # запись прошла через Предохранитель от имени почты, без хозяина рядом
            self.assertEqual([(a.capability, a.actor, a.unattended) for a in seen], [("fs.write", "mail", True)])
            self.assertEqual(Path(seen[0].target), path)
            # агент читает письмо тулом — картинка в запросе модели, ход заражён
            env.script([call("mail_read", id=L["id"][-6:]), {"text": "На картинке голубое."}])
            result = await app.mind.say(env.sid, "Что за картинка в письме?")
            self.assertIsNone(result["error"], result)
            self.assertTrue(result["tainted"])
            body = json.dumps(env.fake.state.requests[-1]["body"], ensure_ascii=False)
            self.assertIn(base64.b64encode(raw).decode(), body)
            self.assertIn("Картинка из письма", body)
            self.assertIn("недоверенные данные", body)
            tool = next(m for m in app.store.messages(env.sid) if m.get("role") == "tool")
            self.assertIn("приложена отдельным сообщением", tool["content"])
            self.assertNotIn(base64.b64encode(raw).decode(), json.dumps(tool, ensure_ascii=False))
            media = next(m for m in app.store.messages(env.sid) if m["meta"].get("tool_media"))
            self.assertEqual(media["meta"]["source"], "mail_read")

    @async_test
    async def test_stranger_not_saved_limits_and_old_rows(self):
        async with MailEnv(self.tmp) as env:
            app = env.app
            app.settings.set("mail", {"triage": False, "on_new": "quiet"})
            await app.mail.poll()
            env.box.add(letter("Злодей <evil@attacker.example>", ("x.png", png((1, 2, 3)), "image/png")))
            [L] = await app.mail.poll()
            self.assertEqual(L["images"][0]["name"], "x.png")
            self.assertNotIn("path", L["images"][0])
            self.assertFalse((app.dirs.work / "inbox").exists() and any((app.dirs.work / "inbox").rglob("*x.png")))
            r = await app.tools.run("mail_read", {"id": L["id"][-6:]}, env.ctx())
            self.assertTrue(r.ok, r.content)
            self.assertIn("картинка не сохранена: отправитель неизвестен", r.content)
            self.assertEqual(r.model_parts, [])
            # от всех — но не больше пяти, и в пределах лимита вложений
            app.settings.set("mail", {"images": "all"})
            pics = [(f"p{i}.png", png((i, i, i)), "image/png") for i in range(7)]
            env.box.add(letter("Злодей <evil@attacker.example>", *pics))
            [L] = await app.mail.poll()
            self.assertEqual(sum("path" in i for i in L["images"]), 5)
            self.assertEqual([i.get("skipped") for i in L["images"][5:]], ["больше 5 картинок в письме"] * 2)
            # лимит за ход: только read_files_per_turn картинок уходит модели
            app.settings.set("attachments", {"read_files_per_turn": 2})
            ctx = env.ctx()
            r = await app.tools.run("mail_read", {"id": L["id"][-6:]}, ctx)
            self.assertEqual(sum(p["type"] == "image" for p in r.model_parts), 2)
            self.assertTrue(r.tainted)
            self.assertIn("лимит медиафайлов за ход", r.content)
            # старая запись: вложения — список строк, колонки images нет (NULL)
            app.store.x("""INSERT INTO mail(id, dir, uid, from_addr, subject, date, body, attachments, status, created)
                           VALUES('oldletter000001', 'in', 9999, 'a@b.example', 'старое', 1, 'текст', '["счёт.pdf", "фото.jpg"]', 'old', 1)""")
            old = app.mail.get("oldletter000001")
            self.assertEqual(old["images"], [])
            self.assertIn("Вложения: счёт.pdf, фото.jpg", app.mail.render(old))
            r = await app.tools.run("mail_read", {"id": "000001"}, env.ctx())
            self.assertTrue(r.ok, r.content)
            self.assertIn("счёт.pdf", r.content)
            # вложения объектами (на будущее) тоже читаются
            app.store.x("UPDATE mail SET attachments = ? WHERE id = 'oldletter000001'", (json.dumps([{"name": "a.png", "path": "/x"}, "b.txt"]),))
            self.assertIn("Вложения: a.png, b.txt", app.mail.render(app.mail.get("oldletter000001")))
            # выключено — не сохраняется
            app.settings.set("mail", {"images": "off"})
            env.box.add(letter("Вася <vasya@example.org>", ("y.png", png((9, 9, 9)), "image/png")))
            [L] = await app.mail.poll()
            self.assertIn("выключены", L["images"][0]["skipped"])
            await env.settle()

    @async_test
    async def test_agent_cannot_widen(self):
        async with MailEnv(self.tmp) as env:
            app = env.app
            self.assertEqual(app.settings.get("mail", "images"), "known")
            with self.assertRaises(NeedsOwner):
                app.settings.set("mail", {"images": "all"}, by="agent")
            app.settings.set("mail", {"images": "off"}, by="agent")  # сужать можно
            with self.assertRaises(NeedsOwner):
                app.settings.set("mail", {"images": "known"}, by="agent")
            app.settings.set("mail", {"images": "all"}, by="owner")
            app.settings.set("mail", {"images": "known"}, by="agent")
            self.assertEqual(app.settings.get("mail", "images"), "known")
