// Сообщение во время хода: режим всегда виден у «Отправить», меняет настройку; ждущее слово — с разовыми кнопками;
// «↪ Вставить» отдаёт его модели посреди хода (команда уходит в фон), метка «вставлено в ход»; телефон без прокрутки вбок.
const {chromium}=require('playwright');
const path=require('path');
async function state(page){return page.evaluate(async()=>(await(await fetch('/api/state')).json()).new_message);}
async function main(){
 const browser=await chromium.launch();
 try{
  const context=await browser.newContext({viewport:{width:1280,height:900}});
  const page=await context.newPage(),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('console',e=>{if(e.type()==='error')errors.push(e.text());});
  await page.goto(process.env.BENDER_URL+'/#/');
  const mode=page.getByLabel('Новое сообщение во время хода',{exact:true});
  await mode.waitFor();
  if(await mode.inputValue()!=='insert')throw Error('По умолчанию должно быть «вставлять»');
  await mode.selectOption('queue');
  await page.waitForFunction(async()=>(await(await fetch('/api/state')).json()).new_message==='queue');
  await fetch(process.env.FAKE_URL+'/fake/script',{method:'POST',body:JSON.stringify([
    {tool_calls:[{name:'run',arguments:{command:'sleep 20'}}]},{text:'Увидел твоё посреди работы.'},{text:'(лишнее)'}])});
  const box=page.locator('.composer textarea');
  await box.fill('Долгая задача');
  await page.getByRole('button',{name:'Отправить',exact:true}).click();
  await page.getByPlaceholder('Написать во время работы — отвечу после хода').waitFor();
  await box.fill('А теперь другое');
  await page.getByRole('button',{name:'Отправить',exact:true}).click();
  const pending=page.locator('.msg.human.queued').filter({hasText:'А теперь другое'});
  await pending.getByText('⏳ после хода',{exact:false}).waitFor();
  await pending.getByRole('button',{name:'⚡ Прервать и ответить'}).waitFor();
  await page.screenshot({path:path.join(process.argv[2],'steer-queued.png'),fullPage:true});
  await pending.getByRole('button',{name:'↪ Вставить'}).click();
  const word=page.locator('.msg.human').filter({hasText:'А теперь другое'});
  await word.getByText('↪ вставлено в ход',{exact:false}).waitFor({timeout:8000});
  await page.locator('.m-text').filter({hasText:'Увидел твоё посреди работы.'}).waitFor({timeout:8000});
  await page.locator('.toolcall').filter({hasText:'в фоне'}).waitFor();  // команда не убита, а ушла в фон
  await page.screenshot({path:path.join(process.argv[2],'steer-inserted.png'),fullPage:true});
  await mode.selectOption('insert');
  await page.waitForFunction(async()=>(await(await fetch('/api/state')).json()).new_message==='insert');
  await page.setViewportSize({width:390,height:844});
  if(await page.locator('.scrim').isVisible())await page.locator('.scrim').click({position:{x:380,y:20}});
  await page.locator('.sidebar').waitFor({state:'hidden'});
  await page.waitForTimeout(350);
  if(!await mode.isVisible())throw Error('Режим не виден на телефоне');
  if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1))throw Error('Пульт шире телефона');
  await page.screenshot({path:path.join(process.argv[2],'steer-phone.png'),fullPage:true});
  if(await state(page)!=='insert')throw Error('Режим не вернулся');
  if(errors.length)throw Error(errors.join('\n'));
  console.log('Сообщение во время хода: режим у «Отправить», после хода → вставить, вставлено посреди хода, телефон — OK');
 }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exit(1);});
