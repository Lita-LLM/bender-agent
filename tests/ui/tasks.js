// Экран «Задачи»: дерево задач субагентов, подробности, итог по схеме. Данные — tests/ui/tasks_setup.py.
// Запуск: tests/ui/run.sh tasks
const path = require("path");
let pw;
try { pw = require("playwright"); } catch { pw = require("/opt/node22/lib/node_modules/playwright"); }
const URL = process.env.BENDER_URL || "http://127.0.0.1:8795";
const OUT = process.argv[2] || "/tmp";

async function main() {
  const browser = await pw.chromium.launch();
  const errors = [];
  for (const [w, h, tag] of [[1280, 900, "wide"], [390, 844, "phone"]]) {
    const page = await browser.newPage({ viewport: { width: w, height: h } });
    page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto(URL + "/#/tasks");
    await page.waitForSelector(".li-title:has-text('Сравни три статьи')");
    await page.waitForSelector(".li-title:has-text('Спам ли письмо?')");
    await page.waitForSelector(".list-item:has-text('Почини сборку') .badge:has-text('ошибка')");
    const nested = await page.evaluate(() => [...document.querySelectorAll(".list > div > div")].some((d) => d.style.marginLeft === "18px"));
    if (!nested) errors.push("вложенная задача не сдвинута вправо");
    await page.click(".li-title:has-text('Спам ли письмо?')");
    await page.waitForSelector(".modal pre:has-text('\"spam\": true')");
    await page.waitForSelector("summary:has-text('Схема ответа ✓')");
    await page.screenshot({ path: path.join(OUT, `ui-tasks-${tag}.png`), fullPage: true });
    const over = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
    if (over) errors.push(`${tag}: экран шире окна`);
    await page.close();
  }
  await browser.close();
  if (errors.length) throw new Error("Ошибки:\n" + errors.join("\n"));
  console.log("экран «Задачи»: ок");
}

main().catch((e) => { console.error(e); process.exit(1); });
