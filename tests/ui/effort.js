// Усилия: сохранение, настоящий запрос Claude, команда без модели и телефон.
const {chromium}=require('playwright');
const path=require('path');
async function main(){
 const browser=await chromium.launch();
 try{
  const context=await browser.newContext({viewport:{width:1280,height:900}});
  const page=await context.newPage(),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('console',e=>{if(e.type()==='error')errors.push(e.text());});
  await page.goto(process.env.BENDER_URL+'/#/models');
  await page.getByLabel('Усилия по умолчанию',{exact:true}).selectOption('high');
  await page.waitForFunction(async()=>(await(await fetch('/api/models')).json()).reasoning.effort==='high');
  const role=page.locator('.card').filter({has:page.getByRole('heading',{name:'Главная',exact:true})});
  await role.getByText('Усилия модели',{exact:true}).click();
  await page.getByLabel('Усилия main 1',{exact:true}).selectOption('max');
  await role.getByText('Запрошено: max. В запросе: max · anthropic.',{exact:false}).waitFor();
  await role.getByRole('button',{name:'Сохранить',exact:true}).click();
  await page.waitForFunction(async()=>(await(await fetch('/api/models')).json()).roles.main[0].params.effort==='max');
  await page.reload();
  const params=await page.evaluate(async()=>(await(await fetch('/api/models')).json()).roles.main[0].params);
  if(params.future_field!==7)throw Error('Потеряно неизвестное поле кандидата');
  await page.getByRole('button',{name:'Спросить',exact:true}).click();
  await page.locator('.m-text').filter({hasText:'Эхо:'}).waitFor();
  const records=await(await fetch(process.env.FAKE_URL+'/fake/requests')).json();
  const request=(records.requests||records).filter(r=>r.api==='anthropic').at(-1);
  if(!request||request.body.output_config?.effort!=='max'||request.body.thinking?.type!=='adaptive')throw Error('В Claude не пришёл native effort');
  if(request.body.thinking.budget_tokens||request.body.effort)throw Error('Утёк служебный параметр или старый бюджет');
  await role.locator('.effort-editor summary').click();
  await role.scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(process.argv[2],'effort-wide.png'),fullPage:true});
  await page.goto(process.env.BENDER_URL+'/#/');
  await page.getByLabel('Сообщение…',{exact:true}).fill('/effort low');
  await page.getByRole('button',{name:'Отправить',exact:true}).click();
  await page.getByText('Усилия этого агента: low.',{exact:false}).waitFor();
  await page.goto(process.env.BENDER_URL+'/#/models');
  await page.getByLabel('Усилия по умолчанию',{exact:true}).waitFor();
  if(await page.getByLabel('Усилия по умолчанию',{exact:true}).inputValue()!=='low')throw Error('Команда не сохранила умолчание');
  await page.setViewportSize({width:390,height:844});
  if(await page.locator('.scrim').isVisible())await page.locator('.scrim').click({position:{x:380,y:20}});
  await page.locator('.sidebar').waitFor({state:'hidden'});
  await page.waitForTimeout(350);
  await page.locator('.effort-editor summary').first().click();
  if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1))throw Error('Усилия шире телефона');
  await page.screenshot({path:path.join(process.argv[2],'effort-phone.png'),fullPage:true});
  if(errors.length)throw Error(errors.join('\n'));
  console.log('Усилия: сохранение, native Claude, /effort и телефон — OK');
 }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exit(1);});
