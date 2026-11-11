// Claude Code как движок из пульта: первый запуск → «Claude Code (на этой машине)» → проверка (найден, вошёл)
// → подключить → взять модель → разговор, в котором Claude Code пишет файл руками Бендера через петлевой MCP.
// Фальшивый claude кладёт tests/ui/claudecode_setup.py, PATH к нему добавляет run.sh:
//   tests/ui/run.sh claudecode [папка для снимков]
const path = require("path");
let pw;
try { pw = require("playwright"); } catch { pw = require("/opt/node22/lib/node_modules/playwright"); }
const URL = process.env.BENDER_URL || "http://127.0.0.1:8800";
const OUT = process.argv[2] || "/tmp";

async function main() {
  const browser = await pw.chromium.launch();
  const errors = [];
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(URL);
  await page.waitForSelector("text=Агент ещё спит");
  await page.selectOption("select.select", "claude_code");
  await page.waitForSelector(".cc-status >> text=вошёл");
  await page.waitForSelector(".cc-status >> text=9.9.9 (Claude Code)");
  await page.waitForSelector(".note.amber >> text=Правила Anthropic");
  if (await page.$("input[type=password]")) throw new Error("для Claude Code не нужен ключ, а поле ключа показано");
  await page.screenshot({ path: path.join(OUT, "ui-claudecode-check.png"), fullPage: true });
  await page.click(".cc-setup button:has-text('Подключить')");
  await page.waitForSelector(".list-item:has-text('sonnet')").catch(async (e) => { await page.screenshot({ path: path.join(OUT, "ui-claudecode-fail.png"), fullPage: true }); throw e; });
  await page.waitForSelector(".list-item:has-text('sonnet') >> text=по подписке");
  await page.screenshot({ path: path.join(OUT, "ui-claudecode-models.png"), fullPage: true });
  await page.click(".list-item:has-text('sonnet') >> text=Взять");
  await page.waitForSelector("text=Модель отвечает", { timeout: 15000 });  // проверка без тулов — разовый вызов claude
  await page.waitForSelector(".composer textarea", { timeout: 15000 });
  await page.fill(".composer textarea", "запиши привет в файл");
  await page.click(".composer .btn.primary");
  await page.waitForSelector(".m-text:has-text('Записал файл через руки Бендера')", { timeout: 20000 });
  await page.waitForSelector(".event.tool:has-text('write_file')");
  await page.waitForSelector(".msg.human .m-text:has-text('запиши привет в файл')");
  await page.screenshot({ path: path.join(OUT, "ui-claudecode-chat.png"), fullPage: true });
  await page.goto(URL + "/#/models");
  await page.waitForSelector(".li-sub:has-text('claude на этой машине')");
  await page.waitForSelector(".badge:has-text('подписка')");
  await page.screenshot({ path: path.join(OUT, "ui-claudecode-providers.png"), fullPage: true });
  await browser.close();
  if (errors.length) throw new Error("Ошибки в консоли:\n" + errors.join("\n"));
  console.log("Claude Code в пульте: ок");
}

main().catch((e) => { console.error(e); process.exit(1); });
