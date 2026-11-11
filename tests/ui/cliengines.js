// Codex не включается; устаревший CLI отсутствует, Google API сохранён.
const { chromium } = require("playwright");
const path = require("path");
async function main() {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
    const errors = [];
    page.on("pageerror", e => errors.push(e.message));
    page.on("console", m => { if (m.type() === "error") errors.push(m.text()); });
    await page.goto(process.env.BENDER_URL);
    await page.waitForSelector("text=Агент ещё спит");
    await page.selectOption("select.select", "codex_cli");
    await page.getByRole("button", { name: "Проверить CLI", exact: true }).click();
    await page.waitForSelector(".cli-status:has-text('0.163.0')");
    if (!await page.getByRole("button", { name: "Подключить CLI", exact: true }).isDisabled()) throw Error("Codex включён без границы тулов");
    if (await page.locator('option[value="gemini_cli"]').count()) throw Error('Удалённый CLI доступен');
    await page.selectOption("select.select", "gemini");
    await page.waitForSelector('input[type="password"]');
    await page.screenshot({ path: path.join(process.argv[2], "google-api.png"), fullPage: true });
    if (errors.length) throw Error(errors.join("\n"));
    console.log("CLI: Codex заблокирован, устаревший CLI удалён, Google API сохранён — OK");
  } finally { await browser.close(); }
}
main().catch(e => { console.error(e); process.exit(1); });
