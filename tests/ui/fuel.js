// Топливо обновляется событием после ответа, без запроса модели по кнопке квот.
const { chromium } = require('playwright');
const path = require('path');
async function main() {
 const browser = await chromium.launch();
 try {
  const page = await browser.newPage({ viewport:{width:390,height:844} });
  const errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.BENDER_URL+'/#/models');
  await page.locator('.fuel').getByText('Пока нет данных.',{exact:false}).waitFor();
  await page.getByRole('button',{name:'Спросить',exact:true}).click();
  await page.locator('.fuel-account').waitFor();
  await page.locator('.fuel-account').getByText('script',{exact:false}).waitFor();
  await page.locator('.fuel').getByRole('button',{name:'Обновить',exact:true}).click();
  await page.getByRole('button',{name:'Меню',exact:true}).click().catch(()=>{});
  if(await page.locator('.fuel').evaluate(e=>e.scrollWidth>e.clientWidth+1))throw Error('Топливо шире телефона');
  await page.screenshot({path:path.join(process.argv[2],'fuel-phone.png'),fullPage:true});
  if(errors.length)throw Error(errors.join('\n'));
  console.log('Топливо: SSE после ответа, обновление без модели, телефон — OK');
 }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exit(1);});
