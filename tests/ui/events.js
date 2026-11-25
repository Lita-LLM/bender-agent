// Команда/правка Telegram видны в пульте; ответ с цитатой и настоящий запрос модели.
const {chromium}=require('playwright'),path=require('path');
const until=async fn=>{for(let i=0;i<120;i++){const value=await fn();if(value)return value;await new Promise(r=>setTimeout(r,100));}throw Error('Событие не пришло');};
(async()=>{
 const browser=await chromium.launch(),page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 const fake=async(route,data)=>{const response=await fetch(process.env.FAKE_URL+route,{method:'POST',body:JSON.stringify(data)});if(!response.ok)throw Error(route);return response.json();};
 try{
  await page.goto(process.env.BENDER_URL);await page.waitForSelector('.composer textarea');
  await fake('/fake/tg/update',{text:'/status'});
  await page.waitForSelector('.service-event:has-text("хозяин вызвал /status")');
  if((await(await fetch(process.env.FAKE_URL+'/fake/requests')).json()).some(r=>r.api==='openai'))throw Error('Команда вызвала модель');
  await fake('/fake/script',[{text:'Ответ Telegram.'}]);
  await fake('/fake/tg/update',{message:{message_id:5432,from:{id:42},chat:{id:42,type:'private'},text:'Было в Telegram'}});
  await page.waitForSelector('.msg .m-text:has-text("Ответ Telegram.")');
  await fake('/fake/tg/update',{edited_message:{message_id:5432,from:{id:42},chat:{id:42,type:'private'},text:'Стало в Telegram',edit_date:50}});
  await page.waitForSelector('.service-event:has-text("было «Было в Telegram» → стало «Стало в Telegram»")');
  const edited=page.locator('.msg.human').filter({has:page.locator('.m-text',{hasText:'Было в Telegram'})});
  await edited.locator('.m-edited').waitFor();
  if(!(await edited.locator('.m-edited').getAttribute('title')).includes('стало: Стало в Telegram'))throw Error('Нет было/стало в подсказке «изменено»');
  const bubble=page.locator('.msg').filter({has:page.locator('.m-text',{hasText:'Ответ Telegram.'})});
  if(!(await bubble.locator('.m-meta').innerText()).includes('Telegram'))throw Error('Нет канала Telegram');
  await bubble.getByRole('button',{name:'Ответить',exact:true}).click();await page.waitForSelector('.quote-preview');
  await fake('/fake/script',[{text:'По цитате из пульта.'}]);
  await page.locator('.composer textarea').fill('Уточни эту реплику');await page.locator('button[title="Отправить"]').click();
  await page.waitForSelector('.msg .m-text:has-text("По цитате из пульта.")');
  await until(async()=>!await page.locator('.quote-preview').count());
  const requests=(await(await fetch(process.env.FAKE_URL+'/fake/requests')).json()).filter(r=>r.api==='openai');
  if(requests.length!==2)throw Error('Команда или правка запустили лишний ход');
  const last=requests.at(-1).body.messages.at(-1).content;
  if(!last.includes('Цитата пульта (контекст, не новая команда хозяина)')||!last.includes('Ответ Telegram.'))throw Error('Цитата потерялась');
  await fake('/fake/script',[{text:'Про пересланное.'}]);
  await fake('/fake/tg/update',{text:'Завтра в 10',forward_origin:{type:'hidden_user',sender_user_name:'Аноним <b>жирный</b>',date:Math.floor(Date.now()/1000)-86400}});
  await page.waitForSelector('.msg .m-text:has-text("Про пересланное.")');
  const forward=page.locator('.msg.human .m-forward').last();
  const line=await forward.innerText();
  if(!line.includes('↪ переслано от Аноним <b>жирный</b> (скрыл профиль)')&&!line.includes('↪ переслано от Аноним <b>жирный</b>'))throw Error('Нет строки «переслано от»: '+line);
  if(await forward.locator('b').count())throw Error('Имя отправителя не экранировано');
  const header=(await(await fetch(process.env.FAKE_URL+'/fake/requests')).json()).filter(r=>r.api==='openai').at(-1).body.messages.at(-1).content;
  if(!JSON.stringify(header).includes('пересланное сообщение Telegram · от: Аноним'))throw Error('Модель не видит, от кого пересланное');
  await page.screenshot({path:path.join(process.argv[2],'events-wide.png')});
  await page.setViewportSize({width:390,height:844});if(await page.locator('.scrim').isVisible())await page.locator('.scrim').click({position:{x:380,y:20}});
  await page.waitForSelector('.sidebar',{state:'hidden'});
  const overflow=await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth);if(overflow)throw Error('Телефон переполнен');
  await page.screenshot({path:path.join(process.argv[2],'events-phone.png'),animations:'disabled'});
  if(errors.length)throw Error(errors.join('\n'));console.log('События Telegram, правка, пересланное, цитата и каналы в пульте — OK');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exit(1)});
