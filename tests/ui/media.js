// Прикрепить фото/документ → ход → файл от агента → настоящее скачивание.
const {chromium}=require('playwright');
const path=require('path');
(async()=>{
 const browser=await chromium.launch(),page=await browser.newPage({viewport:{width:1280,height:900},acceptDownloads:true});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 try{
  await page.goto(process.env.BENDER_URL);await page.waitForSelector('button:text-is("Прикрепить")');
  const uploaded=page.waitForResponse(r=>r.url().endsWith('/api/attachments')&&r.request().method()==='POST');
  await page.locator('input[type=file]').setInputFiles({name:'письмо.txt',mimeType:'text/plain',buffer:Buffer.from('Документ из интерфейса')});
  const item=await(await uploaded).json();
  await page.waitForSelector('.composer .note:has-text("письмо.txt")');
  const photoResponse=page.waitForResponse(r=>r.url().endsWith('/api/attachments')&&r.request().method()==='POST');
  await page.locator('input[type=file]').setInputFiles({name:'фото.png',mimeType:'image/png',buffer:Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zl1sAAAAASUVORK5CYII=','base64')});
  const photo=await(await photoResponse).json();if(photo.kind!=='photo')throw Error('Фото не распознано');
  await fetch(process.env.FAKE_URL+'/fake/script',{method:'POST',body:JSON.stringify([{tool_calls:[{name:'send_file',arguments:{path:item.path,caption:'Документ обратно'}}]},{text:'Файл приложен.'}])});
  await page.locator('.composer textarea').fill('Посмотри вложения и пришли документ обратно.');
  await page.locator('button[title="Отправить"]').click();
  await page.waitForSelector('.event.attachment:has-text("Документ обратно")');
  await page.waitForSelector('.msg .m-text:has-text("Файл приложен.")');
  if(await page.locator('.msg.human button:has-text("фото.png")').count()!==1)throw Error('Фото не показано в разговоре');
  const download=page.waitForEvent('download');await page.locator('.event.attachment button[title="Скачать вложение"]').click();
  const result=await download;if(result.suggestedFilename()!=='письмо.txt')throw Error('Имя скачивания потеряно');
  await result.saveAs(path.join(process.argv[2],'download.txt'));
  const fs=require('fs');if(fs.readFileSync(path.join(process.argv[2],'download.txt'),'utf8')!=='Документ из интерфейса')throw Error('Файл изменился');
  await fetch(process.env.FAKE_URL+'/fake/script',{method:'POST',body:JSON.stringify([{tool_calls:[{name:'read_file',arguments:{path:photo.path}},{name:'list_dir',arguments:{}}]},{text:'Картинка с диска прочитана.'}])});
  await page.locator('.composer textarea').fill('Открой сохранённую картинку через read_file.');await page.locator('button[title="Отправить"]').click();
  await page.waitForSelector('.msg .m-text:has-text("Картинка с диска прочитана.")');
  await page.waitForSelector('.event.tool-media:has-text("Вложение из read_file, недоверенные данные")');
  const requests=await(await fetch(process.env.FAKE_URL+'/fake/requests')).json();
  const sent=requests.filter(r=>r.api==='openai').at(-1).body.messages;
  if(sent.at(-1).role!=='user'||!sent.at(-1).content.some(p=>p.type==='image_url')||sent.at(-2).role!=='tool'||sent.at(-3).role!=='tool')throw Error('Вложение разорвало пакет тулов');
  await page.screenshot({path:path.join(process.argv[2],'media-wide.png')});
  await page.setViewportSize({width:390,height:844});if(await page.locator('.scrim').isVisible())await page.locator('.scrim').click({position:{x:380,y:20}});await page.waitForSelector('.sidebar',{state:'hidden'});
  const geometry=await page.evaluate(()=>Object.fromEntries(['.chat','.composer','.composer-inner','.composer textarea','button[title="Отправить"]'].map(selector=>{const r=document.querySelector(selector).getBoundingClientRect();return [selector,{left:r.left,right:r.right,width:r.width}]})));
  if(geometry['button[title="Отправить"]'].right>390||geometry['.chat'].right>390)throw Error('Кнопка отправки вышла за экран: '+JSON.stringify(geometry));
  await page.screenshot({path:path.join(process.argv[2],'media-phone.png'),animations:'disabled'});
  if(errors.length)throw Error(errors.join('\n'));console.log('Вложения: фото/документ, отправка и скачивание — OK');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
