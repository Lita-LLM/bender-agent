// Модели на агентов, чаты клиента и отдельная строка прав API.
const {chromium}=require('playwright'),fs=require('fs'),path=require('path');
const URL=process.env.BENDER_URL,OUT=process.argv[2];
async function main(){
 const browser=await chromium.launch(),errors=[];
 try{
  const page=await browser.newPage({viewport:{width:1280,height:950}});page.on('pageerror',e=>errors.push(e.message));
  await page.goto(URL+'/#/channels');const card=page.locator('.api-channel');await card.waitFor();
  await card.getByRole('button',{name:'Создать ключ',exact:true}).click();await card.locator('.api-key').waitFor();const key=await card.locator('.api-key').innerText();
  await card.locator('label').filter({hasText:'API включено'}).click();await card.locator('.badge.green').waitFor();
  await card.getByText('bender/second',{exact:true}).waitFor();
  const api=async(model,user,text,header)=>page.evaluate(async({key,model,user,text,header})=>{const response=await fetch('/v1/chat/completions',{method:'POST',headers:{Authorization:'Bearer '+key,'Content-Type':'application/json',...(header?{'X-Bender-Chat':header}:{})},body:JSON.stringify({model,user,messages:[{role:'user',content:text}]})});return {code:response.status,body:await response.json()};},{key,model,user,text,header});
  const one=await api('bender/main','client-one','Первый чат'),again=await api('bender/main','client-one','Продолжение'),two=await api('bender/main','client-two','Второй чат'),other=await api('bender/second','client-one','Другому агенту');
  if([one,again,two,other].some(r=>r.code!==200))throw Error('Запрос API не прошёл');
  if(one.body.bender.session!==again.body.bender.session||one.body.bender.session===two.body.bender.session||other.body.bender.agent!=='second')throw Error('Смешаны чаты/агенты');
  await page.goto(URL+'/#/rights?agent=main');await page.locator('.api-rights').waitFor();
  if(await page.locator('[name="api-profile"]').inputValue()!=='helper')throw Error('API по умолчанию не Помощник');
  await page.selectOption('[name="api-profile"]','reader');await page.getByText('Права API сохранены',{exact:true}).waitFor();
  if(await page.getByRole('combobox',{name:'Шаблон прав',exact:true}).inputValue()!=='full')throw Error('API изменило общий потолок агента');
  const reader=await api('bender/main','reader','Вопрос чтецу');if(reader.code!==200)throw Error('Чтец не отвечает');
  const requests=await(await fetch(process.env.FAKE_URL+'/fake/requests')).json();const last=requests.requests?.at(-1)||requests.at?.(-1);
  if(last?.body?.tools?.length)throw Error('Чтец получил тулы');
  await page.getByRole('checkbox',{name:'Агент доступен по API',exact:true}).click();
  await page.waitForFunction(async()=>!(await(await fetch('/api/agents/main/rights')).json()).api_rights.enabled);
  const disabled=await api('bender/main','reader','Не должен отвечать');if(disabled.code!==403)throw Error('Выключенный API агента продолжил отвечать');
  await page.getByRole('checkbox',{name:'Агент доступен по API',exact:true}).click();await page.waitForFunction(async()=>(await(await fetch('/api/agents/main/rights')).json()).api_rights.enabled);
  await page.locator('.api-rights').scrollIntoViewIfNeeded();await page.screenshot({path:path.join(OUT,'api-rights.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});const scrim=page.locator('.scrim');if(await scrim.isVisible())await scrim.click({position:{x:380,y:20}});await page.locator('.sidebar').waitFor({state:'hidden'});await page.locator('.api-rights').scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(OUT,'api-rights-phone.png'),fullPage:true});
  const size=await page.locator('.page').evaluate(e=>[e.scrollWidth,e.clientWidth]);if(size[0]>size[1]+1)throw Error('Права шире телефона '+size);
  if(errors.length)throw Error(errors.join('\n'));fs.writeFileSync(path.join(OUT,'api-metrics.json'),JSON.stringify({sameChat:true,separateChats:true,separateAgents:true,defaultHelper:true,reader:true,agentOff:true},null,2));console.log('API стаи: модели, отдельные чаты, Помощник/Чтец, выключатель и телефон — OK');
 }finally{await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
