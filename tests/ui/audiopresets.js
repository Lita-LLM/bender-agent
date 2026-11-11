// Подключение аудиопресетов без текстового modelping; настоящий микрофон → WAV.
const {chromium}=require('playwright'),path=require('path');
(async()=>{const browser=await chromium.launch({args:['--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream']});
 const context=await browser.newContext({viewport:{width:1280,height:900},permissions:['microphone']}),page=await context.newPage(),errors=[];page.on('pageerror',e=>errors.push(e.message));
 try{
  await page.goto(process.env.BENDER_URL+'/#/models');
  await page.getByRole('button',{name:'Подключить Уши',exact:true}).click();
  let modal=page.getByRole('dialog');await modal.getByLabel('Откуда',{exact:true}).selectOption('groq_voice');
  await modal.getByLabel('Адрес API',{exact:true}).fill(process.env.FAKE_URL+'/v1');await modal.getByLabel('API-ключ',{exact:true}).fill('fake-groq-key');
  await modal.getByRole('button',{name:'Подключить',exact:true}).click();
  await modal.locator('.list-item').filter({has:page.getByText('whisper-large-v3-turbo',{exact:true})}).getByRole('button',{name:'Взять',exact:true}).click();
  await modal.waitFor({state:'hidden'});
  await page.getByRole('button',{name:'Подключить Голос',exact:true}).click();modal=page.getByRole('dialog');
  await modal.getByText('На бесплатном уровне аудио используется для улучшения продуктов Google.',{exact:false}).waitFor();
  await modal.getByText('России нет в доступных регионах',{exact:false}).waitFor();
  await modal.getByLabel('Адрес API',{exact:true}).fill(process.env.FAKE_URL+'/v1beta');await modal.getByLabel('API-ключ',{exact:true}).fill('fake-google-key');
  await page.screenshot({path:path.join(process.argv[2],'google-notice-wide.png')});
  await modal.getByRole('button',{name:'Подключить',exact:true}).click();
  await modal.locator('.list-item').filter({has:page.getByText('gemini-3.8-flash-tts',{exact:true})}).getByRole('button',{name:'Взять',exact:true}).click();await modal.waitFor({state:'hidden'});
  const speech=await page.evaluate(async()=>await(await fetch('/api/voice/speech',{method:'POST',headers:{'X-Bender':'1','Content-Type':'application/json'},body:JSON.stringify({text:'Проверка русского голоса'})})).json());
  if(speech.mime!=='audio/wav'||!speech.audio.startsWith('UklGR'))throw Error('Gemini PCM не превратился в WAV');
  await page.getByText('Локальный голос на этой машине',{exact:true}).scrollIntoViewIfNeeded();await page.screenshot({path:path.join(process.argv[2],'audio-roles-wide.png')});
  await page.goto(process.env.BENDER_URL+'/#/');await page.getByRole('button',{name:'Микрофон',exact:true}).click();
  await page.getByRole('button',{name:'Закончить запись',exact:true}).waitFor();await page.waitForTimeout(700);await page.getByRole('button',{name:'Закончить запись',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('.composer textarea')?.value==='Привет голосом');
  const requests=await(await fetch(process.env.FAKE_URL+'/fake/requests')).json();
  if(requests.some(r=>r.api==='openai'))throw Error('Аудиопрессет вызвал текстовый modelping');
  if(!requests.some(r=>r.api==='audio_stt'&&r.model==='whisper-large-v3-turbo'&&r.file_magic==='52494646'))throw Error('Микрофон не передал настоящий WAV в выбранные Уши');
  if(!requests.some(r=>r.api==='gemini_audio'))throw Error('Нет родного запроса Gemini');
  if(errors.length)throw Error(errors.join('\n'));console.log('Аудио: предупреждение Google, пресеты и роли, Gemini PCM/WAV, настоящий микрофон/WAV — OK');
 }finally{await browser.close();}
})().catch(error=>{console.error(error);process.exitCode=1;});
