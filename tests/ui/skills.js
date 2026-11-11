// Импорт в карантин, просмотр файлов, одобрение, слэш-команда и настоящий скрипт.
const { chromium } = require("playwright");
const fs = require("fs");
const path = require("path");
const URL = process.env.BENDER_URL;
const FAKE = process.env.FAKE_URL;
const OUT = process.argv[2];

async function main() {
  const source = path.resolve(OUT, "source-summary");
  fs.mkdirSync(path.join(source, "references"), { recursive: true });
  fs.mkdirSync(path.join(source, "scripts"), { recursive: true });
  fs.writeFileSync(path.join(source, "SKILL.md"), "---\nname: summary\ndescription: Составить краткий отчёт\n---\nSKILL_BODY_SENTINEL: составь отчёт по задаче хозяина.\n");
  fs.writeFileSync(path.join(source, "references", "guide.md"), "SKILL_REFERENCE_SENTINEL: дополнительные сведения");
  fs.writeFileSync(path.join(source, "scripts", "report.py"), "from pathlib import Path\nPath('skill-result.txt').write_text('Навык проверен')\nprint('Навык проверен')\n");
  const browser = await chromium.launch();
  const errors = [];
  try {
    const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
    const page = await context.newPage();
    page.on("pageerror", e => errors.push(e.message));
    page.on("console", m => { if (m.type() === "error") errors.push(m.text()); });
    await page.goto(URL + "/#/skills");
    await page.fill('[name="skills-folder"]', source);
    await page.getByRole("button", { name: "Импортировать в карантин", exact: true }).click();
    await page.waitForSelector(".approval:has-text('импортировать папку навыков')");
    await page.getByRole("button", { name: "Разрешить один раз", exact: true }).click();
    await page.waitForSelector(".skill-card:has-text('/summary')");
    await page.waitForSelector(".skill-card .badge:has-text('Карантин')");
    await page.getByRole("button", { name: "Просмотреть", exact: true }).click();
    await page.waitForSelector(".skill-text:has-text('SKILL_BODY_SENTINEL')");
    await page.selectOption(".skill-preview select", "references/guide.md");
    await page.waitForSelector(".skill-text:has-text('SKILL_REFERENCE_SENTINEL')");
    await page.selectOption(".skill-preview select", "scripts/report.py");
    await page.waitForSelector(".skill-text:has-text('Path(')");
    await page.selectOption(".skill-preview select", "SKILL.md");
    await page.getByRole("button", { name: "Одобрить и включить просмотренный пакет", exact: true }).click();
    await page.waitForSelector(".skill-card .badge.green");
    await fetch(FAKE + "/fake/script", { method: "POST", body: JSON.stringify([{ text: "Навык применён" }]) });
    await page.goto(URL + "/#/");
    await page.fill(".composer textarea", "/summary проверь задачу");
    await page.click(".composer .btn.primary");
    await page.waitForSelector("text=Навык применён");
    const sent = await (await fetch(FAKE + "/fake/requests")).json();
    const request = sent.find(r => JSON.stringify(r.body.messages).includes("SKILL_BODY_SENTINEL"));
    if (!request || request.body.messages[0].content.includes("SKILL_BODY_SENTINEL")) throw Error("Тело навыка не раскрыто по команде или попало в системный индекс");
    if (JSON.stringify(request).includes("SKILL_REFERENCE_SENTINEL")) throw Error("Справочный файл попал в контекст без запроса");
    await page.goto(URL + "/#/polygon");
    await page.selectOption("select.select", "skill_run");
    await page.fill("textarea.textarea.mono", JSON.stringify({ name: "summary", path: "scripts/report.py", args: [], timeout: 5 }));
    await page.getByRole("button", { name: "Выполнить", exact: true }).click();
    const guard = await context.newPage();
    await guard.goto(URL + "/#/guard");
    await guard.waitForSelector(".approval:has-text('запустить навык summary')");
    await guard.getByRole("button", { name: "Разрешить один раз", exact: true }).click();
    await guard.locator(".approval").waitFor({ state: "detached" });
    await guard.close();
    await page.waitForSelector("text=Навык проверен");
    const catalog = await (await page.request.get(URL + "/api/skills")).json();
    const directory = catalog.skills.find(s => s.id === "summary").directory;
    fs.appendFileSync(path.join(directory, "references", "guide.md"), "\nИзменение после одобрения");
    await page.goto(URL + "/#/skills");
    await page.waitForSelector(".skill-card .badge:has-text('Изменён — карантин')");
    await page.getByRole("button", { name: "Просмотреть", exact: true }).click();
    await page.waitForSelector("text=Ранее одобренный SKILL.md");
    await page.screenshot({ path: path.join(OUT, "skills-wide.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    await page.waitForTimeout(350);
    if (await page.locator(".page").evaluate(e => e.scrollWidth > e.clientWidth)) throw Error("Страница навыков шире телефона");
    await page.locator(".skill-card").screenshot({ path: path.join(OUT, "skills-phone.png") });
    if (errors.length) throw Error(errors.join("\n"));
    console.log("Навыки: карантин, файлы, одобрение, слэш-команда, скрипт и изменение пакета — OK");
  } finally {
    await browser.close();
  }
}
main().catch(e => { console.error(e); process.exit(1); });
