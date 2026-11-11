// Настоящая команда в Полигоне наследует лимит из настроек «Права».
const { chromium } = require("playwright");
const path = require("path");
async function main() {
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage();
    const errors = [];
    page.on("pageerror", e => errors.push(e.message));
    page.on("console", m => { if (m.type() === "error") errors.push(m.text()); });
    await page.goto(process.env.BENDER_URL + "/#/guard");
    await page.waitForSelector("text=Лимиты дочерних процессов");
    await page.screenshot({ path: path.join(process.argv[2], "sandbox.png"), fullPage: true });
    await page.goto(process.env.BENDER_URL + "/#/polygon");
    await page.selectOption("select.select", "run");
    await page.fill("textarea.textarea.mono", JSON.stringify({ command: "python3 -c 'import resource; print(resource.getrlimit(resource.RLIMIT_AS)[0])'" }));
    await page.getByRole("button", { name: "Выполнить", exact: true }).click();
    await page.waitForSelector("pre:has-text('1073741824')");
    if (errors.length) throw Error(errors.join("\n"));
    console.log("Защита ОС: сведения в пульте и настоящий лимит команды — OK");
  } finally { await browser.close(); }
}
main().catch(e => { console.error(e); process.exit(1); });
