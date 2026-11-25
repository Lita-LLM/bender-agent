// Карточка «Нагрузка» на экране «Тело»: числа и график, отчёт с диска, выключатели и заведующий узлом.
// Данные — tests/ui/load_setup.py (агент «Завхоз», замер раз в 5 с, отчёт о нагрузке узла). Запуск: tests/ui/run.sh load
const path = require("path");
let pw;
try { pw = require("playwright"); } catch { pw = require("/opt/node22/lib/node_modules/playwright"); }
const URL = process.env.BENDER_URL || "http://127.0.0.1:8794";
const OUT = process.argv[2] || "/tmp";

async function state(page) { return (await page.request.get(URL + "/api/load")).json(); }

async function until(page, check, what, ms = 30000) {
  const end = Date.now() + ms;
  while (Date.now() < end) { if (await check()) return; await page.waitForTimeout(250); }
  throw new Error("не дождался: " + what);
}

async function main() {
  const browser = await pw.chromium.launch();
  const errors = [];
  try {
    for (const [w, h, tag, scheme] of [[1280, 1000, "wide", "dark"], [390, 844, "phone", "light"]]) {
      const page = await browser.newPage({ viewport: { width: w, height: h }, colorScheme: scheme });
      page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
      page.on("pageerror", (e) => errors.push(e.message));
      await page.goto(URL + "/#/body");
      const card = page.locator("[data-load-card]");
      await card.getByRole("heading", { name: "Нагрузка" }).waitFor();
      if (tag === "wide") {
        // замеры идут раз в 5 с: к второму из них карточка рисует график (обновление — кнопкой «Кто грузит сейчас»)
        await until(page, async () => (await state(page)).samples.length >= 2, "два замера");
        await card.getByRole("button", { name: "Кто грузит сейчас" }).click();
        await card.locator("[data-load-top]").waitFor();
        await card.locator("svg[aria-label='Процессор за последний час']").waitFor();
        if (await card.locator("svg[aria-label='Процессор за последний час'] polyline").count() !== 2) throw new Error("на графике нет двух линий");
        await card.getByText(/Бендер \d+% ядра/).first().waitFor();
        // отчёт с диска открывается текстом
        await card.getByRole("link", { name: /· узел$/ }).first().click();
        const dialog = page.getByRole("dialog");
        await dialog.getByText("Главный подозреваемый: процесс stress", { exact: false }).waitFor();
        await page.screenshot({ path: path.join(OUT, "ui-load-report.png"), fullPage: true });
        await page.keyboard.press("Escape");
        await dialog.waitFor({ state: "hidden" });
        // заведующий узлом — агент стаи; на карточке агента в «Стае» — пометка
        await card.getByLabel("Заведующий узлом").selectOption({ label: "Завхоз" });
        await until(page, async () => (await state(page)).settings.manager === "zavhoz", "заведующий выбран");
        await card.getByLabel("Когда звать заведующего").selectOption("node");
        await until(page, async () => (await state(page)).settings.manager_on === "node", "когда звать");
        // выключатели работают без перезапуска
        await card.getByRole("checkbox", { name: "Следить за всем узлом" }).click({ force: true });
        await until(page, async () => (await state(page)).settings.node === false, "узел выключен");
        await card.getByRole("checkbox", { name: "Анализ потребления Бендера" }).click({ force: true });
        await card.getByText("Выключено: ничего не читается.").waitFor();
        await page.screenshot({ path: path.join(OUT, "ui-load-off.png"), fullPage: true });
        await card.getByRole("checkbox", { name: "Анализ потребления Бендера" }).click({ force: true });
        await card.getByRole("checkbox", { name: "Следить за всем узлом" }).click({ force: true });
        await until(page, async () => { const s = (await state(page)).settings; return s.enabled === true && s.node === true; }, "снова включено");
        await page.goto(URL + "/#/agents");
        await page.locator(".agent-row:has-text('Завхоз') .badge:has-text('заведующий узлом')").waitFor();
        await page.goto(URL + "/#/body");
        await card.getByRole("heading", { name: "Нагрузка" }).waitFor();
      } else {
        const over = await page.evaluate(() => document.documentElement.scrollWidth > window.innerWidth + 1);
        if (over) errors.push("на телефоне экран «Тело» шире экрана");
        await card.getByLabel("Заведующий узлом").waitFor();
        if (await card.getByLabel("Заведующий узлом").inputValue() !== "zavhoz") errors.push("на телефоне не виден выбранный заведующий");
      }
      await card.scrollIntoViewIfNeeded();
      await page.screenshot({ path: path.join(OUT, `ui-load-${tag}-${scheme}.png`), fullPage: true });
      await page.close();
    }
  } finally {
    await browser.close();
  }
  if (errors.length) throw new Error("Ошибки:\n" + errors.join("\n"));
  console.log("карточка «Нагрузка»: ок");
}

main().catch((e) => { console.error(e); process.exit(1); });
