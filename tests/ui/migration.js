// Знакомство → Hermes → точный план → перенос; Claude требует отдельного выбора.
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');
async function main() {
  const out = process.argv[2];
  const hermes = path.resolve(out, 'hermes-source');
  const claude = path.resolve(out, 'claude-source');
  fs.cpSync(path.resolve(__dirname, '../fixtures/hermes'), hermes, {recursive: true});
  fs.cpSync(path.resolve(__dirname, '../fixtures/claude-notes'), claude, {recursive: true});
  if(fs.existsSync(path.join(hermes,'state.db'))) fs.unlinkSync(path.join(hermes,'state.db'));
  execFileSync('python3', ['-c', 'import sqlite3,sys,pathlib; p=pathlib.Path(sys.argv[1]); db=sqlite3.connect(p/"state.db"); db.executescript((p/"state.sql").read_text()); db.close()', hermes]);
  const browser = await chromium.launch();
  try {
    const page = await browser.newPage({viewport:{width:1280,height:900}});
    const errors=[];
    page.on('pageerror',e=>errors.push(e.message));
    page.on('console',m=>{if(m.type()==='error') errors.push(m.text());});
    await page.goto(process.env.BENDER_URL + '/#/');
    async function plan(kind, folder) {
      await page.locator('.card').filter({has:page.getByRole('heading',{name:'Перенести из '+kind,exact:true})}).getByRole('button',{name:'Переселить',exact:true}).click();
      await page.locator('#oc-path').fill(folder);
      await page.getByRole('button',{name:'Искать',exact:true}).click();
      await page.getByRole('button',{name:'Посмотреть план',exact:true}).click();
      await page.waitForSelector('.approval:has-text("прочитать источник переезда")');
      await page.getByRole('button',{name:'Разрешить один раз',exact:true}).click();
      await page.waitForSelector('.pi-list');
    }
    await plan('Hermes',hermes);
    await page.locator('pre:has-text("проверочный ананас")').waitFor();
    await page.getByRole('button',{name:/^Переселить \(/}).click();
    await page.waitForSelector('.approval:has-text("прочитать выбранный источник переезда")');
    await page.getByRole('button',{name:'Разрешить один раз',exact:true}).click();
    await page.locator('.note.green:has-text("Перенесено:")').waitFor();
    await page.screenshot({path:path.join(out,'hermes.png'),fullPage:true});
    await Promise.all([page.waitForNavigation(),page.getByRole('button',{name:'К разговору',exact:true}).click()]);
    await page.goto(process.env.BENDER_URL + '/#/self');
    await plan('Claude Code',claude);
    if (await page.locator('.pi input:checked').count()) throw Error('Заметки Claude выбраны молча');
    await page.locator('.pi').filter({hasText:'CLAUDE.md → заметка памяти'}).locator('input').check();
    await page.getByRole('button',{name:'Переселить (1)',exact:true}).click();
    await page.waitForSelector('.approval:has-text("прочитать выбранный источник переезда")');
    await page.getByRole('button',{name:'Разрешить один раз',exact:true}).click();
    await page.locator('.note.green:has-text("Перенесено: 1")').waitFor();
    await page.setViewportSize({width:390,height:844});
    await page.screenshot({path:path.join(out,'claude-phone.png'),fullPage:true});
    if(errors.length) throw Error(errors.join('\n'));
    console.log('Переезд: Hermes, просмотр заметок Claude и выбор хозяином — OK');
  } finally {await browser.close();}
}
main().catch(e=>{console.error(e);process.exit(1);});
