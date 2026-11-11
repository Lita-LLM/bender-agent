// Сценарий пульта в настоящем Chromium: подключить тестовую модель, поговорить, ответить на карточку,
// пройти по экранам. Ширина телефона и широкий экран. Ошибки консоли — провал.
// Запуск: tests/ui/run.sh flow [папка для снимков]  (или BENDER_URL=… FAKE_URL=… node tests/ui/flow.js)
const path = require("path");
let pw;
try { pw = require("playwright"); } catch { pw = require("/opt/node22/lib/node_modules/playwright"); }
const URL = process.env.BENDER_URL || "http://127.0.0.1:8790";
const FAKE = process.env.FAKE_URL || "http://127.0.0.1:5199";
const OUT = process.argv[2] || "/tmp";

async function main() {
  const browser = await pw.chromium.launch();
  const errors = [];
  for (const [label, viewport] of [["phone", { width: 390, height: 844 }], ["wide", { width: 1280, height: 820 }]]) {
    const page = await browser.newPage({ viewport });
    page.on("console", (m) => { if (m.type() === "error") errors.push(`[${label}] ${m.text()}`); });
    page.on("pageerror", (e) => errors.push(`[${label}] ${e.message}`));
    await page.goto(URL);
    await page.waitForSelector(".topbar");
    if (label === "phone") {
      // первый запуск: подключить тестовую модель через интерфейс
      await page.waitForSelector("text=Агент ещё спит");
      await page.screenshot({ path: path.join(OUT, `ui-${label}-welcome.png`) });
      await page.selectOption("select.select", "fake");
      await page.fill("input[placeholder='http://127.0.0.1:5199/v1']", FAKE + "/v1");  // фальшивые модели могут жить не на 5199 (run.sh)
      await page.click("button.btn.primary:text-is('Подключить')");
      await page.waitForSelector("text=Моделей:");
      await page.fill("input[placeholder='название или free']", "script");
      await page.click(".list-item:has-text('script') >> text=Взять");
      await page.waitForSelector(".composer textarea");
      // сценарий: агент хочет выполнить команду → карточка → «один раз» → ответ
      await fetch(`${FAKE}/fake/script`, { method: "POST", body: JSON.stringify([
        { tool_calls: [{ name: "run", arguments: { command: "echo пульт работает && uname -s" } }] },
        { text: "Готово: **команда выполнена**.\n\n- пункт один\n- пункт два" },
      ]) });
      await page.fill(".composer textarea", "выполни проверку");
      await page.click(".composer .btn.primary");
      await page.waitForSelector(".approval");
      await page.screenshot({ path: path.join(OUT, `ui-${label}-approval.png`) });
      await page.click(".approval >> text=Разрешить один раз");
      await page.waitForSelector("text=команда выполнена");
      await page.waitForTimeout(400);
      await page.screenshot({ path: path.join(OUT, `ui-${label}-chat.png`), fullPage: false });
      // что ушло в модель
      await page.click(".msg .icon-btn[title='Что ушло в модель']");
      await page.waitForSelector("text=Что ушло в модель");
      await page.keyboard.press("Escape");
    }
    for (const [hash, marker] of [["#/self", "Кто я"], ["#/models", "Роли моделей"], ["#/guard", "Предохранитель"], ["#/journal", "Проверить цепочку"],
                                  ["#/polygon", "Модель напрямую"], ["#/settings", "Сейф"], ["#/", "команда выполнена"]]) {
      await page.goto(URL + "/" + hash);
      await page.waitForSelector(`text=${marker}`, { timeout: 8000 });
      await page.waitForTimeout(250);
      await page.screenshot({ path: path.join(OUT, `ui-${label}-${hash.replace(/[#/]/g, "") || "chat"}.png`) });
    }
    // Предохранитель: проверить решение без выполнения
    await page.goto(URL + "/#/guard");
    await page.fill(".card input.mono", "rm -rf /");
    await page.click("button:text-is('Проверить')");
    await page.waitForSelector("text=Жёсткий список");
    if (label === "phone") {
      // невысокий телефон: меню листается, «Настройки» и нижняя часть доступны
      await page.setViewportSize({ width: 390, height: 520 });
      await page.click("button[title='Меню']");
      await page.waitForSelector(".sidebar.open");
      const scroll = await page.$eval(".sidebar", (el) => ({ over: el.scrollHeight > el.clientHeight, style: getComputedStyle(el).overflowY }));
      if (!scroll.over || scroll.style !== "auto") throw new Error("меню телефона не листается: " + JSON.stringify(scroll));
      const link = page.locator(".sidebar .nav a[href='#/settings']");
      await link.scrollIntoViewIfNeeded();
      const box = await link.boundingBox();
      if (!box || box.y + box.height > 520) throw new Error("«Настройки» за краем экрана: " + JSON.stringify(box));
      await page.screenshot({ path: path.join(OUT, `ui-${label}-menu-scrolled.png`) });
      await link.click();
      await page.waitForSelector("text=Сейф");
    }
    await page.close();
  }
  await browser.close();
  if (errors.length) { console.error("Ошибки в консоли:\n" + errors.join("\n")); process.exit(1); }
  console.log("Пульт: сценарий пройден, ошибок в консоли нет.");
}
main().catch((e) => { console.error(e); process.exit(1); });
