// Почта и вебхуки на экране «Связь»: подключить ящик (фальшивые IMAP/SMTP из `bender fake-llm`), новое письмо разбирает
// Чтец, важное — строкой в разговоре; письмо открывается; вебхук создаётся, принимает запрос с секретом, хозяину — строка.
//   tests/ui/run.sh mail [папка для снимков]
const path = require("path");
let pw;
try { pw = require("playwright"); } catch { pw = require("/opt/node22/lib/node_modules/playwright"); }
const URL = process.env.BENDER_URL || "http://127.0.0.1:8794";
const FAKE = process.env.FAKE_URL || "http://127.0.0.1:5294";
const OUT = process.argv[2] || "/tmp";
const fakePort = +new globalThis.URL(FAKE).port;

async function main() {
  const browser = await pw.chromium.launch();
  const errors = [];
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push(e.message));
  const mailCard = ".card:has(h3:text-is('Почта'))";
  await page.goto(URL + "/#/channels");
  await page.waitForSelector(mailCard);
  await page.fill(`${mailCard} input[type=email]`, "owner@example.com");
  await page.fill(`${mailCard} input[type=password]`, "secret");
  await page.click(`${mailCard} details.mail-servers summary`);
  await page.fill("input[name=imap_host]", "127.0.0.1");
  await page.fill("input[name=imap_port]", String(fakePort + 1));
  await page.selectOption("select[name=imap_security]", "none");
  await page.fill("input[name=smtp_host]", "127.0.0.1");
  await page.fill("input[name=smtp_port]", String(fakePort + 2));
  await page.selectOption("select[name=smtp_security]", "none");
  await page.screenshot({ path: path.join(OUT, "ui-mail-setup.png"), fullPage: true });
  await page.click(`${mailCard} button:has-text('Проверить и подключить')`);
  await page.waitForSelector(`${mailCard} .badge:has-text('на связи')`, { timeout: 15000 });
  // новое важное письмо: Чтец (модель script) отвечает разбором по схеме
  await fetch(FAKE + "/fake/script", { method: "POST", body: JSON.stringify([{ text: '{"spam": false, "important": true, "needs_reply": true, "category": "работа", "summary": "Просит отчёт к пятнице."}' }]) });
  await fetch(FAKE + "/fake/mail/add", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ from: "Начальник <boss@work.example>", subject: "Отчёт к пятнице", text: "Пришли отчёт к пятнице, пожалуйста." }) });
  await page.click(`${mailCard} button:has-text('Проверить сейчас')`);
  await page.waitForSelector(".mail-list .list-item:has-text('Начальник') .badge:has-text('важное')", { timeout: 20000 });
  await page.waitForSelector(".mail-list .list-item:has-text('Просит отчёт к пятнице')");
  await page.screenshot({ path: path.join(OUT, "ui-mail-list.png"), fullPage: true });
  await page.click(".mail-list .list-item:has-text('Начальник')");
  await page.waitForSelector(".modal .letter-body:has-text('Пришли отчёт к пятнице')");
  await page.screenshot({ path: path.join(OUT, "ui-mail-letter.png") });
  await page.click(".modal [title='Закрыть']");
  // вебхук
  await page.click("button:has-text('Новый вебхук')");
  await page.fill(".hook-form input.input:not(.port)", "Датчик");
  await page.click(".hook-form button:has-text('Создать')");
  await page.waitForSelector(".list-item.hook:has-text('Датчик')");
  const hooks = await (await fetch(URL + "/api/hooks")).json();
  const h = hooks.hooks[0];
  const r = await fetch(URL + h.path, { method: "POST", headers: { Authorization: `Bearer ${h.secret}`, "Content-Type": "application/json" }, body: JSON.stringify({ text: "Протечка в ванной!" }) });
  if (r.status !== 202) throw new Error("вебхук ответил " + r.status);
  const bad = await fetch(URL + h.path, { method: "POST", body: "{}" });
  if (bad.status !== 404) throw new Error("вебхук без секрета ответил " + bad.status);
  await page.waitForSelector(".list-item.hook:has-text('запросов 1')", { timeout: 10000 });
  await page.screenshot({ path: path.join(OUT, "ui-mail-hooks.png"), fullPage: true });
  // в разговоре — строки письма и вебхука
  await page.goto(URL + "/#/");
  await page.waitForSelector(".event.mail:has-text('Важное письмо от Начальник')");
  await page.waitForSelector(".event.hook:has-text('Датчик: Протечка в ванной!')");
  await page.screenshot({ path: path.join(OUT, "ui-mail-chat.png"), fullPage: true });
  await browser.close();
  if (errors.length) throw new Error("Ошибки в консоли:\n" + errors.join("\n"));
  console.log("почта и вебхуки в пульте: ок");
}

main().catch((e) => { console.error(e); process.exit(1); });
