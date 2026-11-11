// Настоящий браузер: темы, переименование сессии и выключатель без вызова модели.
const {chromium}=require('playwright'),path=require('path');
const FAKE=process.env.FAKE_URL;
const push=data=>fetch(FAKE+'/fake/tg/update',{method:'POST',body:JSON.stringify(data)});
async function until(predicate){for(let i=0;i<200;i++){const result=await predicate();if(result)return result;await new Promise(r=>setTimeout(r,100));}throw Error('Не дождался темы');}
(async()=>{
 const browser=await chromium.launch(),page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 try{
  await page.goto(process.env.BENDER_URL+'/#/channels');await page.getByText('Темы у бота: включены.',{exact:true}).waitFor();
  await push({text:'/new Список покупок'});
  const read=()=>page.evaluate(async()=>await(await fetch('/api/channels/telegram/topics')).json());
  const first=await until(async()=>{const data=await read();return data.topics[0];});
  const renamed=await page.evaluate(async sid=>await(await fetch('/api/chats/'+sid,{method:'PATCH',headers:{'X-Bender':'1','Content-Type':'application/json'},body:JSON.stringify({title:'Новый план'})})).json(),first.session);
  if(renamed.session?.title!=='Новый план')throw Error('Заголовок не обновлён');
  const child=await page.evaluate(async()=>await(await fetch('/api/agents',{method:'POST',headers:{'X-Bender':'1','Content-Type':'application/json'},body:JSON.stringify({name:'Сосед',awaken:false})})).json());
  await push({text:'/agent '+child.id});await push({text:'/new Для соседа'});
  await until(async()=>(await read()).topics.length===2);
  const records=await(await fetch(FAKE+'/fake/tg/sent')).json();
  if(!records.some(m=>m.method==='editForumTopic'&&m.name.includes('Новый план')&&m.message_thread_id===first.thread))throw Error('Тема не переименована');
  const responses=records.filter(m=>m.method==='sendMessage'&&m.text.includes('Новый разговор создан'));
  if(responses.length!==2||responses.some(m=>!m.message_thread_id)||responses[0].message_thread_id===responses[1].message_thread_id)throw Error('Разговоры смешали темы');
  await page.reload();await page.locator('summary:text-is("Темы агентов")').click();await page.getByText('Сосед · Для соседа',{exact:true}).waitFor();
  await page.locator('.toggle').filter({hasText:'Темы разговоров в личке'}).click();
  await until(async()=>!(await read()).enabled);
  await page.screenshot({path:path.join(process.argv[2],'topics-wide.png')});
  await page.setViewportSize({width:390,height:844});
  const scrim=page.locator('.scrim');if(await scrim.isVisible())await scrim.click({position:{x:380,y:20}});
  await page.locator('.sidebar').waitFor({state:'hidden'});
  await page.getByText('Темы агентов',{exact:true}).scrollIntoViewIfNeeded();
  await page.screenshot({path:path.join(process.argv[2],'topics-phone.png'),animations:'disabled'});
  const requests=await(await fetch(FAKE+'/fake/requests')).json();if(requests.some(r=>r.api==='openai'))throw Error('Управление темами вызвало модель');
  if(errors.length)throw Error(errors.join('\n'));console.log('Темы: два агента, новый чат, название, выключатель и телефон — без модели, OK');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
