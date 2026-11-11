// Меню в пульте, /help и переключение агента кнопкой — без обращения к модели.
const {chromium}=require('playwright'),path=require('path');
const FAKE=process.env.FAKE_URL;
const post=(route,data)=>fetch(FAKE+route,{method:'POST',body:JSON.stringify(data)});
async function until(predicate){for(let i=0;i<200;i++){const result=await predicate();if(result)return result;await new Promise(r=>setTimeout(r,100));}throw Error('Не дождался команды');}
(async()=>{
 const browser=await chromium.launch(),page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 try{
  await page.goto(process.env.BENDER_URL+'/#/channels');
  await page.getByText('Обновлять меню команд',{exact:true}).waitFor();
  await page.getByText('Меню: обновлено.',{exact:false}).waitFor();
  const menus=await(await fetch(FAKE+'/fake/tg/commands')).json();
  const own=menus.menus.find(m=>m.scope==='default'&&m.language==='ru');
  if(!own||(own.commands.length<25||own.commands.length>100)||!own.commands.some(c=>c.command==='compact'))throw Error('Нет собственного русского меню с compact');
  const firstSet=menus.calls.findIndex(m=>m.method==='setMyCommands');
  if(firstSet<12||menus.calls.slice(firstSet).some(m=>m.method!=='setMyCommands'))throw Error('Нарушен порядок очистки меню');
  await post('/fake/tg/update',{text:'/help'});
  await until(async()=>{const sent=await(await fetch(FAKE+'/fake/tg/sent')).json();return sent.some(m=>m.method==='sendMessage'&&m.text.includes('Разговоры и стая')&&m.text.includes('Навыки — слэш-команды'));});
  const child=await page.evaluate(async()=>await(await fetch('/api/agents',{method:'POST',headers:{'X-Bender':'1','Content-Type':'application/json'},body:JSON.stringify({name:'Сосед',awaken:false,profile:'helper'})})).json());
  await post('/fake/tg/update',{text:'/agent'});
  const choice=await until(async()=>{const sent=await(await fetch(FAKE+'/fake/tg/sent')).json();const entry=sent.findLast(m=>m.reply_markup?.inline_keyboard?.some(row=>row.some(b=>b.text==='Сосед')));return entry&&entry.reply_markup.inline_keyboard.flat().find(b=>b.text==='Сосед');});
  await post('/fake/tg/update',{callback_query:{id:'choose',from:{id:42},message:{chat:{id:42},message_id:1},data:choice.callback_data}});
  await post('/fake/tg/update',{text:'/whoami'});
  await until(async()=>{const sent=await(await fetch(FAKE+'/fake/tg/sent')).json();return sent.some(m=>m.method==='sendMessage'&&m.text.includes('Я — Сосед')&&m.text.includes('Помощник'));});
  const requests=await(await fetch(FAKE+'/fake/requests')).json();if(requests.some(r=>r.api==='openai'))throw Error('Команды вызвали модель');
  await page.locator('.toggle').filter({hasText:'Обновлять меню команд'}).click();
  await until(async()=>await page.evaluate(async()=>!(await(await fetch('/api/channels')).json()).telegram.settings.commands));
  await page.screenshot({path:path.join(process.argv[2],'commands-wide.png')});
  if(errors.length)throw Error(errors.join('\n'));console.log('Команды: очистка/русское меню, help, кнопка агента, выключатель — без модели, OK');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
