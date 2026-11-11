// Поиск по пульту (Ctrl+K): находит настройку на другом экране, переходит и подсвечивает её.
// Запуск: tests/ui/run.sh finder [папка для снимков]
const path = require("path");
let pw;
try { pw = require("playwright"); } catch { pw = require("/opt/node22/lib/node_modules/playwright"); }
const URL = process.env.BENDER_URL || "http://127.0.0.1:8796";
const OUT = process.argv[2] || "/tmp";

async function main() {
  const browser = await pw.chromium.launch();
  const errors = [];
  for (const [w, h, tag] of [[1280, 900, "wide"], [390, 844, "phone"]]) {
    const page = await browser.newPage({ viewport: { width: w, height: h } });
    page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto(URL + "/#/settings");
    await page.waitForSelector(".card h3");
    // Песочница живёт на экране «Права», а не в «Настройках»: поиск должен привести туда.
    if (tag === "wide") await page.keyboard.press("Control+k");
    else await page.click("button[aria-label='Найти в пульте (Ctrl+K)']");
    await page.fill("input[aria-label='Что найти']", "песочн");
    await page.waitForSelector(".find-item.on .find-title:has-text('Песочница команд')");
    if (await page.locator(".find-item .find-title:text-is('Песочница команд')").count() !== 1) errors.push(`${tag}: настройка найдена дважды`);
    await page.screenshot({ path: path.join(OUT, `ui-finder-list-${tag}.png`) });
    const over = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
    if (over) errors.push(`${tag}: окно поиска шире экрана`);
    await page.keyboard.press("Enter");
    await page.waitForFunction(() => location.hash === "#/guard");
    await page.waitForSelector(".found-flash label:has-text('Песочница команд')");
    await page.screenshot({ path: path.join(OUT, `ui-finder-guard-${tag}.png`) });
    if (tag === "wide") {
      // «/» вне поля ввода тоже открывает поиск; находит кнопку на экране и подпись из схемы настроек.
      await page.keyboard.press("/");
      await page.fill("input[aria-label='Что найти']", "бюджет");
      await page.waitForSelector(".find-item .find-where:has-text('настройка')");
      await page.fill("input[aria-label='Что найти']", "снимок");
      await page.click(".find-item:has(.find-title:text-is('Сделать снимок'))");
      await page.waitForFunction(() => location.hash === "#/backups");
      await page.waitForSelector(".found-flash");
      // Ничего в пульте — предлагается поиск по памяти, и он сразу ищет.
      await page.keyboard.press("Control+k");
      await page.fill("input[aria-label='Что найти']", "Казань");
      await page.click(".find-item:has-text('в памяти агента')");
      await page.waitForFunction(() => location.hash.startsWith("#/memory?q="));
      await page.waitForSelector(".hit-text:has-text('Казань')");
      await page.screenshot({ path: path.join(OUT, `ui-finder-memory-${tag}.png`), fullPage: true });
    }
    await page.close();
  }
  await browser.close();
  if (errors.length) throw new Error("Ошибки:\n" + errors.join("\n"));
  console.log("поиск по пульту: ок");
}

main().catch((e) => { console.error(e); process.exit(1); });
