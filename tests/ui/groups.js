// Каталог чатов и независимое разрешение недоверенного контекста.
const {chromium}=require('playwright'),path=require('path');
(async()=>{
 const browser=await chromium.launch(),page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 try{
  await page.goto(process.env.BENDER_URL+'/#/channels');
  await page.locator('summary:text-is("Где находится бот")').click();
  const row=page.locator('[data-chat="-100001"]');await row.getByText('Рабочая группа',{exact:true}).waitFor();
  await row.locator('.toggle').filter({hasText:'Контекст участников -100001'}).click();
  await page.waitForFunction(async()=>{const r=await fetch('/api/channels/telegram/chats');const d=await r.json();return d.chats.find(x=>x.id===-100001).context===true;});
  await row.locator('.toggle').filter({hasText:'Разрешить группу -100001'}).click();
  await page.waitForFunction(async()=>{const r=await fetch('/api/channels/telegram/chats');const d=await r.json();return d.chats.find(x=>x.id===-100001).allowed===false;});
  await row.locator('summary').click();await row.getByText('"can_send_messages": true',{exact:false}).waitFor();
  await page.screenshot({path:path.join(process.argv[2],'groups-wide.png')});
  if(errors.length)throw Error(errors.join('\n'));console.log('Группы: каталог, права, доступ и недоверенный контекст — OK');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
