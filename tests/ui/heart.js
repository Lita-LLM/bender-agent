// Экраны «Сердце», «Сны», «Тело» в настоящем Chromium. Данные — tests/ui/heart_setup.py, модель — `bender fake-llm`.
// Запуск:
//   python3 -m bender fake-llm --port 5298 &
//   python3 tests/ui/heart_setup.py /tmp/bh 5298
//   python3 -m bender --data /tmp/bh run --no-watchdog --no-browser --port 8795 &
//   BENDER_URL=http://127.0.0.1:8795 node tests/ui/heart.js [папка для снимков]
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
    page.on("dialog", (d) => d.accept());
    await page.goto(URL + "/#/heart");
    await page.waitForSelector(".li-title:has-text('Утренняя сводка')");
    if (tag === "wide") {
      await page.waitForSelector("text=Из OpenClaw пришло задач: 1");
      await page.waitForSelector(".li-title:has-text('Утренняя сводка') .badge:has-text('выключена')");
      // включить задачу из OpenClaw
      await page.click(".job:has(.li-title:has-text('Утренняя сводка')) .toggle");
      await page.waitForSelector(".job:has(.li-title:has-text('Утренняя сводка')) .li-sub:has-text('следующий')");
      // новое напоминание с подсказкой, как понято время
      await page.click("button:has-text('Добавить')");
      await page.fill("input[placeholder='позвонить маме']", "позвонить маме");
      await page.fill("input[placeholder^='завтра в 9:00']", "через 20 минут");
      await page.waitForSelector("text=через 20 мин");
      await page.click(".card:has-text('Новая задача') button.primary:has-text('Добавить')");
      await page.waitForSelector(".li-title:has-text('позвонить маме')");
      // пульс сейчас — ровный, без модели
      await page.click("button:has-text('Проверить сейчас')");
      await page.waitForSelector(".toast:has-text('Пульс: ровный')");
      // намерение
      await page.fill("input[placeholder^='на слова']", "релиз");
      await page.fill("input[placeholder^='напомнить себе']", "спроси про changelog");
      await page.click(".card:has-text('Намерения') button:has-text('Добавить')");
      await page.waitForSelector(".li-title:has-text('спроси про changelog')");
      await page.screenshot({ path: path.join(OUT, `ui-heart-${tag}.png`), fullPage: true });
      // сон вхолостую, потом по-настоящему
      await page.goto(URL + "/#/dreams");
      await page.waitForSelector("text=нового для сна: есть");
      await page.click("button:has-text('Вхолостую')");
      await page.waitForSelector("text=Вхолостую — ничего не записано");
      await page.waitForSelector("li:has-text('Хозяин живёт в Казани')");
      await page.screenshot({ path: path.join(OUT, `ui-dreams-${tag}.png`), fullPage: true });
      // тело
      await page.goto(URL + "/#/body");
      await page.waitForSelector(".mood .badge:has-text('бодр')");
      await page.waitForSelector("text=Больше всех памяти едят");
      await page.screenshot({ path: path.join(OUT, `ui-body-${tag}.png`), fullPage: true });
    } else {
      for (const r of ["heart", "dreams", "body"]) {
        await page.goto(URL + "/#/" + r);
        await page.waitForTimeout(400);
        const over = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
        if (over) errors.push(`на телефоне экран «${r}» шире экрана`);
        await page.screenshot({ path: path.join(OUT, `ui-${r}-${tag}.png`), fullPage: true });
      }
    }
    await page.close();
  }
  await browser.close();
  if (errors.length) throw new Error("Ошибки:\n" + errors.join("\n"));
  console.log("экраны «Сердце», «Сны», «Тело»: ок");
}

main().catch((e) => { console.error(e); process.exit(1); });
