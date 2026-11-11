// Настоящий отдельный процесс Matrix, привязка и ответ через /sync с интервалом 60 с.
const {chromium}=require('playwright'),http=require('http'),fs=require('fs'),path=require('path');
const URL=process.env.BENDER_URL,OUT=process.argv[2],ROOM='!own:example.org',BOT='@bot:example.org',OWNER='@owner:example.org';
let events=[],batch=0;const sent=[],metrics={syncs:0,joins:0,headers:true};const sockets=new Set();
const server=http.createServer(async(req,res)=>{
 let raw='';for await(const part of req)raw+=part;
 const u=new globalThis.URL(req.url,'http://local');let result={},status=200;
 if(req.headers.authorization!=='Bearer UI_MATRIX_PRIVATE_TOKEN')metrics.headers=false;
 if(u.pathname==='/_matrix/client/v3/account/whoami')result={user_id:BOT};
 else if(u.pathname.startsWith('/_matrix/client/v3/join/')){metrics.joins++;result={room_id:ROOM};}
 else if(u.pathname.endsWith('/joined_members'))result={joined:{[BOT]:{},[OWNER]:{}}};
 else if(u.pathname.endsWith('/state/m.room.encryption/'))status=404;
 else if(u.pathname==='/_matrix/client/v3/sync'){
  metrics.syncs++;batch++;result={next_batch:String(batch),rooms:{join:{[ROOM]:{timeline:{events}}}}};events=[];
 }else if(req.method==='PUT'&&u.pathname.includes('/send/')){sent.push(JSON.parse(raw));result={event_id:'$sent'+sent.length};}
 else status=404;
 res.writeHead(status,{'Content-Type':'application/json'});res.end(JSON.stringify(result));
});
server.on('connection',socket=>{sockets.add(socket);socket.on('close',()=>sockets.delete(socket));});
async function main(){
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const address=`http://127.0.0.1:${server.address().port}`;
 const browser=await chromium.launch(),errors=[];
 try{
  const context=await browser.newContext({viewport:{width:1280,height:950}}),page=await context.newPage();page.setDefaultTimeout(20000);
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(URL+'/#/channels');const card=page.locator('.matrix-channel');await card.waitFor();
  await card.getByText(/Из переноса OpenClaw/).waitFor();await card.getByRole('button',{name:'Подставить адрес из переноса',exact:true}).click();
  if(await card.locator('[name="matrix-homeserver"]').inputValue()!=='https://imported.example.org')throw Error('Предложение переноса не подставилось');
  if((await card.innerText()).includes('НЕ_ПОКАЗЫВАТЬ_В_ПРЕДЛОЖЕНИИ'))throw Error('Секрет попал в предложение');
  await card.getByRole('button',{name:'Установить Matrix в карантин',exact:true}).click();await card.getByText(/Пакет в карантине/).waitFor();
  await page.goto(URL+'/#/modules');const module=page.locator('.module-card').filter({has:page.getByRole('heading',{name:'matrix 1.0.0',exact:true})});await module.waitFor();
  await module.getByRole('button',{name:'Просмотреть пакет',exact:true}).click();await module.locator('.module-text').first().waitFor();
  await module.getByRole('button',{name:'Одобрить просмотренный пакет',exact:true}).click();await module.locator('.badge.green').waitFor();
  await module.getByRole('button',{name:'Обнаружить инструменты',exact:true}).click();
  await page.locator('.approval').waitFor();await page.getByRole('button',{name:'Разрешить один раз',exact:true}).click();
  await module.getByRole('button',{name:'Одобрить определения инструментов',exact:true}).click();
  await page.goto(URL+'/#/channels');await card.locator('[name="matrix-homeserver"]').fill(address);await card.locator('[name="matrix-room"]').fill(ROOM);await card.locator('[name="matrix-token"]').fill('UI_MATRIX_PRIVATE_TOKEN');
  await card.getByRole('button',{name:'Сохранить Matrix выключенным',exact:true}).click();await page.waitForFunction(()=>document.querySelector('[name="matrix-token"]').value==='');
  const before=await page.evaluate(async()=>await(await fetch('/api/channels/matrix')).json());if(before.settings.enabled||!before.ready||!before.has_token)throw Error('Настройка не сохранилась выключенной/готовой');
  if(JSON.stringify(before).includes('UI_MATRIX_PRIVATE_TOKEN'))throw Error('Токен попал в API');
  await card.getByRole('button',{name:'Подключить комнату Matrix',exact:true}).click();await card.locator('.approval').waitFor();await card.getByRole('button',{name:'Разрешить один раз',exact:true}).click();
  await card.getByRole('button',{name:'Получить код Matrix',exact:true}).waitFor();
  await page.waitForFunction(()=>!document.querySelector('.matrix-channel button').disabled);
  await card.getByRole('button',{name:'Получить код Matrix',exact:true}).click();await card.locator('code').filter({hasText:'/pair'}).waitFor();
  const pair=await card.locator('code').filter({hasText:'/pair'}).innerText();events.push({type:'m.room.message',event_id:'$pair',sender:OWNER,content:{msgtype:'m.text',body:pair}});
  await card.getByText('Состояние: ok · хозяин '+OWNER,{exact:true}).waitFor({timeout:85000});
  if(sent.length!==1||metrics.syncs!==2)throw Error('Привязка повторялась или /sync опрашивал слишком часто: '+JSON.stringify(metrics));
  events.push({type:'m.room.message',event_id:'$question',sender:OWNER,content:{msgtype:'m.text',body:'Привет из Matrix'}});
  await page.waitForFunction(async()=>{const r=await(await fetch('/api/channels/matrix')).json();return !!r.owner;});
  // Минута — настоящий таймер канала, не ускоренная подмена.
  await new Promise((resolve,reject)=>{const began=Date.now();const check=()=>{if(sent.length>=2)return resolve();if(Date.now()-began>85000)return reject(Error('Нет финального ответа Matrix'));setTimeout(check,1000);};check();});
  if(!sent[1].body.includes('Привет из Matrix')||metrics.syncs!==3)throw Error('Не доставлен ровно один итог/частый опрос');
  await page.screenshot({path:path.join(OUT,'matrix-bound.png'),fullPage:true});
  await page.setViewportSize({width:390,height:844});const scrim=page.locator('.scrim');if(await scrim.isVisible())await scrim.click({position:{x:380,y:20}});await page.locator('.sidebar').waitFor({state:'hidden'});
  await card.scrollIntoViewIfNeeded();await page.screenshot({path:path.join(OUT,'matrix-phone.png'),fullPage:true});
  const width=await page.locator('.page').evaluate(e=>[e.scrollWidth,e.clientWidth]);if(width[0]>width[1]+1)throw Error('Страница шире телефона '+width);
  await card.getByRole('button',{name:'Выключить Matrix',exact:true}).click();await card.getByText('Состояние: off · хозяин '+OWNER,{exact:true}).waitFor();
  if(errors.length||!metrics.headers)throw Error(errors.join('\n')||'Неверный Authorization');fs.writeFileSync(path.join(OUT,'matrix-metrics.json'),JSON.stringify({...metrics,messages:sent.length},null,2));console.log('Matrix: модуль, привязка, настоящий минутный /sync, эхо-модель, ответ и телефон — OK');
 }finally{await browser.close();for(const socket of sockets)socket.destroy();await new Promise(resolve=>server.close(resolve));}
}
main().catch(error=>{console.error(error);for(const socket of sockets)socket.destroy();server.close();process.exitCode=1;});
