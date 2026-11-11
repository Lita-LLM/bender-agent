// Переезд из OpenClaw в настоящем Chromium: приветствие → найти → план → перенос → отчёт → разговор.
// Запуск: BENDER_URL=http://127.0.0.1:8791 OC_PATH=/путь/к/копии/.openclaw node tests/ui/relocate.js [папка для снимков]
// Бендер — с пустой папкой данных; OC_PATH — копия tests/fixtures/openclaw-2026.10.5/.openclaw.
const path = require("path");
let pw;
try { pw = require("playwright"); } catch { pw = require("/opt/node22/lib/node_modules/playwright"); }
const URL = process.env.BENDER_URL || "http://127.0.0.1:8791";
const OC = process.env.OC_PATH;
const OUT = process.argv[2] || "/tmp";

async function main() {
  if (!OC) throw new Error("нужен OC_PATH");
  const browser = await pw.chromium.launch();
  const errors = [];
  const page = await browser.newPage({ viewport: { width: 390, height: 844 } });
  page.on("console", (m) => { if (m.type() === "error") errors.push(m.text()); });
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(URL);
  await page.waitForSelector("text=Агент ещё спит");
  await page.click(".card:has-text('Перенести из OpenClaw') >> button:text-is('Переселить')");
  await page.waitForSelector(".modal >> text=Переезд из OpenClaw");
  await page.waitForSelector(".modal >> text=Укажи папку его состояния");
  await page.fill("#oc-path", OC);
  await page.click(".modal >> button:text-is('Искать')");
  await page.waitForSelector(".modal >> text=Нашёл OpenClaw");
  await page.click(".modal >> button:has-text('Посмотреть план')");
  await page.waitForSelector(".modal .pi");
  const profile=page.getByLabel(/^Шаблон прав:/);
  if(await profile.inputValue()!=='full')throw Error('Главному OpenClaw не предложен Полный');
  await profile.selectOption('helper');
  await page.waitForFunction(()=>[...document.querySelectorAll('.modal select')].some(e=>e.value==='helper'&&!e.disabled));
  await profile.selectOption('full');
  await page.waitForFunction(()=>[...document.querySelectorAll('.modal select')].some(e=>e.value==='full'&&!e.disabled));
  await page.screenshot({ path: path.join(OUT, "ui-relocate-plan.png") });
  // снять галочку с файла из рабочей папки — он не должен переехать
  await page.click(".pi:has-text('project-notes.txt') input");
  await page.click(".modal >> button:has-text('Переселить (')");
  await page.waitForSelector(".modal >> text=Перенесено:");
  await page.screenshot({ path: path.join(OUT, "ui-relocate-done.png") });
  await Promise.all([page.waitForNavigation(), page.click(".modal >> button:has-text('К разговору')")]);
  await page.waitForSelector(".modal", { state: "detached" });
  await page.waitForSelector("text=Привет! Меня зовут Миша");
  await page.waitForSelector(".m-meta:has-text('из OpenClaw')");
  const brand = await page.textContent(".brand-sub");
  if (!brand.includes("Tin")) throw new Error(`имя не сменилось: ${brand}`);
  await page.screenshot({ path: path.join(OUT, "ui-relocate-chat.png") });
  await page.goto(URL + "/#/self");
  await page.waitForSelector("text=Агент только что переехал из OpenClaw");
  await page.screenshot({ path: path.join(OUT, "ui-relocate-self.png") });
  // широкий экран: план повторного переезда — всё уже на месте
  const wide = await browser.newPage({ viewport: { width: 1280, height: 820 } });
  wide.on("pageerror", (e) => errors.push(e.message));
  await wide.goto(URL + "/#/self");
  await wide.waitForSelector('select[aria-label="Выбрать агента"] option:text-is("Tin")', {state:'attached'});
  const childId=await wide.locator('select[aria-label="Выбрать агента"] option:text-is("Tin")').getAttribute('value');
  await Promise.all([wide.waitForNavigation(),wide.selectOption('select[aria-label="Выбрать агента"]',childId)]);
  await wide.click("button:text-is('Переселить')");
  await wide.fill("#oc-path", OC);
  await wide.click(".modal >> button:text-is('Искать')");
  await wide.selectOption('select[aria-label="Назначение переезда"]', "current");
  await wide.click(".modal >> button:has-text('Посмотреть план')");
  await wide.waitForSelector(".modal .pi");
  await wide.waitForSelector(".modal .note:has-text('Новое:'):has-text('уже перенесено или пропускается')");  // осталось расписание и снятый файл
  await wide.click(".modal summary:has-text('Уже перенесено')");
  await wide.waitForSelector(".pi:has-text('Душа') >> text=уже перенесено");
  await wide.screenshot({ path: path.join(OUT, "ui-relocate-again-wide.png") });
  await browser.close();
  if (errors.length) throw new Error("Ошибки в консоли:\n" + errors.join("\n"));
  console.log("переезд в пульте: ок");
}

main().catch((e) => { console.error(e); process.exit(1); });
