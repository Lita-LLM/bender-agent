// Экран «Память» в настоящем Chromium. Данные готовит tests/ui/memory_setup.py (факты, дневник, длинный разговор),
// модель — `bender fake-llm` (Главная — echo, Быстрая — script с пересказом).
// Запуск:
//   python3 -m bender fake-llm --port 5298 &
//   python3 tests/ui/memory_setup.py /tmp/bm 5298
//   python3 -m bender --data /tmp/bm run --no-watchdog --no-browser --port 8794 &
//   BENDER_URL=http://127.0.0.1:8794 node tests/ui/memory.js [папка для снимков]
const path = require("path");
let pw;
try { pw = require("playwright"); } catch { pw = require("/opt/node22/lib/node_modules/playwright"); }
const URL = process.env.BENDER_URL || "http://127.0.0.1:8794";
const OUT = process.argv[2] || "/tmp";

async function main() {
  const browser = await pw.chromium.launch();
  const errors = [];
  for (const [w, h, tag] of [[1280, 900, "wide"], [390, 844, "phone"]]) {
    const page = await browser.newPage({ viewport: { width: w, height: h } });
    page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
    page.on("pageerror", (e) => errors.push(e.message));
    page.on("dialog", (d) => d.accept());
    await page.goto(URL + "/#/memory");
    await page.waitForSelector("text=MEMORY.md");
    await page.waitForSelector(".meter i");
    await page.waitForSelector("text=Фактов:");
    if (tag === "wide") {
      // пересказ: сжать сейчас
      await page.click("button:has-text('Сжать сейчас')");
      await page.waitForSelector("summary:has-text('Пересказ начала')");
      await page.screenshot({ path: path.join(OUT, `ui-memory-overview-${tag}.png`), fullPage: true });
      // поиск
      await page.click(".seg button:has-text('Поиск')");
      await page.fill("input.input", "Казань");
      await page.click("button:has-text('Найти')");
      await page.waitForSelector(".hit-text:has-text('Факт: хозяин — город — Казань')");
      await page.waitForSelector(".hit-text:has-text('живёт в Казани')");  // «всё» — и ядро тоже
      await page.screenshot({ path: path.join(OUT, `ui-memory-search-${tag}.png`), fullPage: true });
      // дневник
      await page.click(".seg button:has-text('Дневник')");
      await page.waitForSelector(".split-body .m-text:has-text('Переехал')");
      // факты: закрыть
      await page.click(".seg button:has-text('Факты')");
      await page.waitForSelector("td:has-text('Казань')");
      await page.click("button.link:has-text('закрыть')");
      await page.waitForSelector("text=Фактов пока нет");
      await page.click(".seg button:has-text('с прошлыми')");
      await page.waitForSelector("s:has-text('Казань')");
      await page.screenshot({ path: path.join(OUT, `ui-memory-facts-${tag}.png`), fullPage: true });
    } else {
      await page.screenshot({ path: path.join(OUT, `ui-memory-overview-${tag}.png`), fullPage: true });
      const over = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
      if (over) errors.push("на телефоне страница шире экрана");
    }
    await page.close();
  }
  await browser.close();
  if (errors.length) throw new Error("Ошибки:\n" + errors.join("\n"));
  console.log("экран «Память»: ок");
}

main().catch((e) => { console.error(e); process.exit(1); });
