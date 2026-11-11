// Экран «Связь»: подключить бота Telegram, привязаться кодом, поговорить — против поддельного Bot API из `bender fake-llm`.
// Запуск:
//   python3 -m bender fake-llm --port 5296 &
//   python3 tests/ui/channels_setup.py /tmp/bc 5296
//   python3 -m bender --data /tmp/bc run --no-watchdog --no-browser --port 8796 &
//   BENDER_URL=http://127.0.0.1:8796 FAKE_URL=http://127.0.0.1:5296 node tests/ui/channels.js [папка для снимков]
const path = require("path");
let pw;
try { pw = require("playwright"); } catch { pw = require("/opt/node22/lib/node_modules/playwright"); }
const URL = process.env.BENDER_URL || "http://127.0.0.1:8796";
const FAKE = process.env.FAKE_URL || "http://127.0.0.1:5296";
const OUT = process.argv[2] || "/tmp";

async function until(fn, ms = 8000) {
  const t0 = Date.now();
  while (Date.now() - t0 < ms) { const v = await fn(); if (v) return v; await new Promise((r) => setTimeout(r, 150)); }
  throw new Error("не дождался");
}

async function main() {
  const browser = await pw.chromium.launch();
  const errors = [];
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(URL + "/#/channels");
  await page.waitForSelector("text=@BotFather");
  await page.fill("input[placeholder^='123456789']", "123:FAKE");
  await page.click("button:has-text('Подключить')");
  await page.waitForSelector("a:has-text('@bender_test_bot')");
  const code = (await page.textContent(".big-code-sm")).replace("/start", "").trim();
  if (!/^BENDER-/.test(code)) throw new Error("нет кода привязки: " + code);
  const link = await page.getAttribute("a:has-text('ссылку')", "href");
  if (link !== `https://t.me/bender_test_bot?start=${code}`) throw new Error("ссылка привязки: " + link);
  await page.screenshot({ path: path.join(OUT, "ui-channels-code.png"), fullPage: true });
  // хозяин жмёт «Старт» в Telegram
  await fetch(FAKE + "/fake/tg/update", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: `/start ${code}` }) });
  await page.waitForSelector("text=Хозяин:", { timeout: 10000 });
  await page.waitForSelector(".badge:has-text('на связи')");
  // и пишет боту
  await fetch(FAKE + "/fake/tg/update", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text: "привет из телеграма" }) });
  const reply = await until(async () => (await (await fetch(FAKE + "/fake/tg/sent")).json()).find((m) => m.method === "sendMessage" && /Эхо:[\s\S]*привет из телеграма/.test(m.text)));
  if (!reply) throw new Error("бот не ответил");
  // то же сообщение видно в пульте — один разговор на все каналы
  await page.goto(URL + "/#/");
  await page.waitForSelector(".m-text:has-text('привет из телеграма')");
  await page.screenshot({ path: path.join(OUT, "ui-channels-chat.png"), fullPage: true });
  await page.goto(URL + "/#/channels");
  await page.waitForSelector("text=Хозяин:");
  await page.screenshot({ path: path.join(OUT, "ui-channels-bound.png"), fullPage: true });
  // API для программ: включить, создать ключ, спросить Бендера как модель
  await page.click(".card:has-text('API для программ') .toggle");
  await page.click("button:has-text('Создать ключ')");
  await page.waitForSelector(".api-key");
  const key = (await page.textContent(".api-key")).trim();
  const r = await fetch(URL + "/v1/chat/completions", { method: "POST", headers: { "Authorization": `Bearer ${key}`, "Content-Type": "application/json" },
    body: JSON.stringify({ model: "bender-isolated", messages: [{ role: "user", content: "привет из программы" }] }) });
  const j = await r.json();
  if (!/привет из программы/.test(j.choices?.[0]?.message?.content || "")) throw new Error("API ответило не то: " + JSON.stringify(j).slice(0, 300));
  await page.screenshot({ path: path.join(OUT, "ui-channels-api.png"), fullPage: true });
  const over = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
  if (over) errors.push("на телефоне экран «Связь» шире экрана");
  await browser.close();
  if (errors.length) throw new Error("Ошибки:\n" + errors.join("\n"));
  console.log("экран «Связь» и Telegram: ок");
}

main().catch((e) => { console.error(e); process.exit(1); });
