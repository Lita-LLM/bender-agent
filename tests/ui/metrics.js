// Метрики в Мастерской: цифры и SVG, история при выключенном учёте, телефон.
const {chromium}=require('playwright');
async function main(){
 const browser=await chromium.launch();
 try{
  const page=await browser.newPage({viewport:{width:1280,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.BENDER_URL+'/#/workshop');
  const panel=page.locator('.metrics-panel');
  await panel.getByText(/Ходы без ошибки: 2 \/ 3/).waitFor();
  if(await panel.locator('svg[role="img"]').count()!==3)throw Error('Не построены три графика');
  await page.screenshot({path:process.argv[2]+'/metrics.png',fullPage:true});
  await page.request.post(process.env.BENDER_URL+'/api/settings/workshop',{headers:{'X-Bender':'1'},data:{metrics:false}});
  await page.getByRole('button',{name:'Обновить',exact:true}).click();
  await panel.getByText('Учёт новых исходов выключен. Сохранённая история доступна.',{exact:true}).waitFor();
  await page.setViewportSize({width:390,height:844});
  await page.locator('.sidebar').waitFor({state:'hidden'});
  await page.screenshot({path:process.argv[2]+'/metrics-phone.png',fullPage:true});
  if(errors.length)throw Error(errors.join('\n'));
 }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
