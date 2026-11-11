// Вход через ChatGPT в настоящем Chromium — против фальшивого OpenAI из `bender fake-llm`.
// Бендер запускается с BENDER_CHATGPT_AUTH_URL=$FAKE и BENDER_CHATGPT_BACKEND_URL=$FAKE/backend-api/codex.
// Запуск: BENDER_URL=http://127.0.0.1:8793 node tests/ui/chatgpt.js [папка для снимков]
const path = require("path");
let pw;
try { pw = require("playwright"); } catch { pw = require("/opt/node22/lib/node_modules/playwright"); }
const URL = process.env.BENDER_URL || "http://127.0.0.1:8793";
const OUT = process.argv[2] || "/tmp";

async function main() {
  const browser = await pw.chromium.launch();
  const errors = [];
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(URL);
  await page.waitForSelector("text=Агент ещё спит");
  await page.selectOption("select.select", "chatgpt");
  await page.click("button:has-text('Войти через ChatGPT')");
  await page.waitForSelector(".big-code:text-is('FAKE-CODE1')");
  await page.screenshot({ path: path.join(OUT, "ui-chatgpt-code.png") });
  // фальшивый OpenAI подтверждает код со второго опроса; Главная назначается сама — сразу разговор
  await page.waitForSelector(".composer textarea", { timeout: 15000 });
  await page.waitForSelector("text=Главная модель — gpt-6-astra");
  await page.fill(".composer textarea", "привет по подписке");
  await page.click(".composer .btn.primary");
  await page.waitForSelector(".m-text:has-text('Эхо:')");
  await page.screenshot({ path: path.join(OUT, "ui-chatgpt-chat.png") });
  await page.goto(URL + "/#/models");
  await page.waitForSelector(".li-sub:has-text('вход owner@example.com')");
  await browser.close();
  if (errors.length) throw new Error("Ошибки в консоли:\n" + errors.join("\n"));
  console.log("вход через ChatGPT в пульте: ок");
}

main().catch((e) => { console.error(e); process.exit(1); });
