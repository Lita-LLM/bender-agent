// Проверка поставщика из настроек и поиск через тул в Полигоне.
const { chromium } = require("playwright");
const path = require("path");
async function main() {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    const errors = [];
    page.on("pageerror", e => errors.push(e.message));
    page.on("console", m => { if (m.type() === "error") errors.push(m.text()); });
    await page.goto(process.env.BENDER_URL + "/#/settings");
    await page.getByLabel("Поисковый запрос", { exact: true }).fill("проверка");
    await page.getByRole("button", { name: "Проверить поиск", exact: true }).click();
    await page.waitForSelector(".search-results:has-text('Результат проверка')");
    await page.screenshot({ path: path.join(process.argv[2], "search.png"), fullPage: true });
    await page.goto(process.env.BENDER_URL + "/#/polygon");
    await page.selectOption("select.select", "web_search");
    await page.fill("textarea.textarea.mono", JSON.stringify({ query: "проверка", count: 2 }));
    await page.getByRole("button", { name: "Выполнить", exact: true }).click();
    await page.waitForSelector("pre:has-text('Краткий ответ')");
    await page.waitForSelector(".badge:has-text('недоверенное')");
    if (errors.length) throw Error(errors.join("\n"));
    console.log("Поиск: настройки, проверка поставщика и Полигон — OK");
  } finally { await browser.close(); }
}
main().catch(e => { console.error(e); process.exit(1); });
