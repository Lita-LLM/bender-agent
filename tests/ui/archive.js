// Свой архив, импорт OpenClaw, кнопка сжатия чата и /compact другого агента без потери истории.
const {chromium}=require('playwright'),path=require('path');
async function until(fn){for(let i=0;i<160;i++){const value=await fn();if(value)return value;await new Promise(r=>setTimeout(r,100));}throw Error('Не дождался результата');}
(async()=>{
 const browser=await chromium.launch(),page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 const fake=async(route,body)=>{const response=await fetch(process.env.FAKE_URL+route,{method:'POST',body:JSON.stringify(body)});if(!response.ok)throw Error(route);return response.json();};
 const api=async(route,body,agent='main')=>page.evaluate(async({route,body,agent})=>{const response=await fetch(route,{method:body===undefined?'GET':'POST',headers:{'X-Bender':'1','X-Bender-Agent':agent,'Content-Type':'application/json'},...(body===undefined?{}:{body:JSON.stringify(body)})});const data=await response.json();if(!response.ok)throw Error(JSON.stringify(data));return data;},{route,body,agent});
 try{
  await page.goto(process.env.BENDER_URL);await page.getByRole('button',{name:'Сжать сейчас',exact:true}).waitFor();
  const state=await api('/api/state');const before=await api('/api/session/'+state.main_session+'/messages');
  await fake('/fake/script',[{text:JSON.stringify({summary:'Первый пересказ из чата',diary:''})}]);
  await page.getByRole('button',{name:'Сжать сейчас',exact:true}).click();
  await until(async()=>{const data=await api('/api/memory/overview');return data.compact.summary.includes('Первый пересказ из чата');});
  const after=await api('/api/session/'+state.main_session+'/messages');if(!before.messages.every(m=>after.messages.some(n=>n.id===m.id&&JSON.stringify(n.content)===JSON.stringify(m.content))))throw Error('Сжатие удалило первоисточник');
  const found=await api('/api/try/tool/sessions',{query:'МИША'});if(!found.ok||!found.content.includes('imported'))throw Error('Не найден импорт');
  const chats=await api('/api/chats'),imported=chats.sessions.find(s=>s.kind==='imported');if(!imported)throw Error('Нет импортированного чата');
  const read=await api('/api/try/tool/session_read',{session_id:imported.id,limit:2});if(!read.ok||!read.content.includes('Меня зовут Миша')||!read.content.includes('Следующая страница'))throw Error('Страница импорта не прочитана');
  await fake('/fake/tg/update',{text:'/agent archive2'});
  await until(async()=>{const rows=await(await fetch(process.env.FAKE_URL+'/fake/tg/sent')).json();return rows.some(r=>r.text?.includes('Теперь отвечает'));});
  await fake('/fake/script',[{text:JSON.stringify({summary:'Второй пересказ из Telegram',diary:''})}]);await fake('/fake/tg/update',{text:'/compact'});
  await until(async()=>{const data=await api('/api/memory/overview',undefined,'archive2');return data.compact.summary.includes('Второй пересказ из Telegram');});
  if(!(await api('/api/memory/overview')).compact.summary.includes('Первый пересказ из чата'))throw Error('Сжат чужой разговор');
  const menus=await(await fetch(process.env.FAKE_URL+'/fake/tg/commands')).json();if(!menus.menus.some(m=>m.commands.some(c=>c.command==='compact')))throw Error('compact отсутствует в меню');
  await page.screenshot({path:path.join(process.argv[2],'archive-wide.png')});
  await page.setViewportSize({width:390,height:844});if(await page.locator('.scrim').isVisible())await page.locator('.scrim').click({position:{x:380,y:20}});await page.waitForSelector('.sidebar',{state:'hidden'});
  if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Архив шире телефона');
  await page.screenshot({path:path.join(process.argv[2],'archive-phone.png'),animations:'disabled'});
  if(errors.length)throw Error(errors.join('\n'));console.log('Архив/импорт, session_read, сжатие чата и Telegram другого агента — OK');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1)});
