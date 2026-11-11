// Rich в настоящем фальшивом API: черновик/итог/видимая поддержка/выключение.
const {chromium}=require('playwright'),path=require('path');
const FAKE=process.env.FAKE_URL;
const fakePost=(route,data)=>fetch(FAKE+route,{method:'POST',body:JSON.stringify(data)});
async function until(predicate){for(let i=0;i<120;i++){const value=await predicate();if(value)return value;await new Promise(r=>setTimeout(r,100));}throw Error('Не дождался ответа Bot API');}
(async()=>{
 await fakePost('/fake/tg/features',{rich:true});
 const browser=await chromium.launch(),page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 try{
  await page.goto(process.env.BENDER_URL+'/#/channels');
  await page.getByText('Расширенное оформление',{exact:true}).waitFor();
  await fakePost('/fake/script',[{reasoning:'Проверяю таблицу.',text:'# Итог\n\n| Имя | Цена |\n| --- | --- |\n| Бендер | $5 |',delay:.12}]);
  await fakePost('/fake/tg/update',{text:'Покажи итог'});
  const records=await until(async()=>{const sent=await(await fetch(FAKE+'/fake/tg/sent')).json();return sent.some(m=>m.method==='sendRichMessage')&&sent;});
  if(!records.some(m=>m.method==='sendRichMessageDraft'&&m.rich_message.markdown.includes('<tg-thinking>')))throw Error('Нет Rich-мыслей в черновике');
  if(records.some(m=>m.method==='sendRichMessage'&&m.rich_message.markdown.includes('<tg-thinking>')))throw Error('Мысли попали в итог');
  await page.getByText('Rich: поддерживается',{exact:false}).waitFor();
  await page.screenshot({path:path.join(process.argv[2],'rich-support.png')});
  const toggle=page.locator('.toggle').filter({hasText:'Расширенное оформление'});await toggle.click();
  await until(async()=>{const data=await page.evaluate(async()=>await(await fetch('/api/channels')).json());return data.telegram.settings.rich===false;});
  await fakePost('/fake/script',[{text:'Обычный **ответ**.'}]);await fakePost('/fake/tg/update',{text:'Покажи простой ответ'});
  await until(async()=>{const sent=await(await fetch(FAKE+'/fake/tg/sent')).json();return sent.some(m=>m.method==='sendMessage'&&m.text==='Обычный <b>ответ</b>.');});
  if(errors.length)throw Error(errors.join('\n'));console.log('Rich: черновик, итог, поддержка и выключение в пульте — OK');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
