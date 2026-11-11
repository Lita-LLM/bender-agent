// Аккаунт Telegram агента: ключи, вход по коду, привязка хозяина и список чатов — на фальшивом помощнике.
const {chromium}=require('playwright'),fs=require('fs'),path=require('path');
const URL=process.env.BENDER_URL,OUT=process.argv[2];
async function main(){
 const browser=await chromium.launch(),errors=[];
 try{
  const page=await browser.newPage({viewport:{width:1280,height:950}});page.on('pageerror',e=>errors.push(e.message));
  await page.goto(URL+'/#/channels');const card=page.locator('.userbot-channel');await card.waitFor();
  await card.getByText('Установлен Telethon',{exact:false}).waitFor();
  const get=page.getByRole('button',{name:'Получить код',exact:true});
  if(!await get.isDisabled())throw Error('Вход доступен без ключей и без согласия на риск');
  await card.locator('[name="userbot-api-id"]').fill('12345');await card.locator('[name="userbot-api-hash"]').fill('a'.repeat(32));
  await card.getByRole('button',{name:'Сохранить ключи',exact:true}).click();await card.getByText('Ключи сохранены.',{exact:true}).waitFor();
  await card.locator('[name="userbot-phone"]').fill('+79001234567');
  if(!await get.isDisabled())throw Error('Вход доступен без согласия на риск');
  await card.locator('label').filter({hasText:'Я понимаю риск для аккаунта'}).click();await get.click();
  await card.locator('[name="userbot-code"]').fill('12345');await card.getByRole('button',{name:'Войти',exact:true}).click();
  await card.getByText('Состояние: подключён',{exact:false}).waitFor();
  await card.getByRole('button',{name:'Получить код привязки',exact:true}).click();await card.locator('code').filter({hasText:'BENDER-'}).waitFor();
  await card.getByText('Список пуст: аккаунт никого не видит.',{exact:false}).waitFor();
  await card.getByRole('button',{name:'Найти чаты',exact:true}).click();
  const row=card.locator('.userbot-chat').filter({hasText:'Рабочая группа'});await row.waitFor();
  await row.locator('label').filter({hasText:'Видит'}).click();
  await page.waitForFunction(async()=>(await(await fetch('/api/channels/userbot')).json()).settings.chats.some(c=>c.id===-1001&&c.see&&!c.reply));
  await row.getByRole('button',{name:'По упоминанию',exact:true}).click();
  await page.waitForFunction(async()=>(await(await fetch('/api/channels/userbot')).json()).settings.chats.some(c=>c.id===-1001&&c.wake==='mention'));
  await card.getByRole('button',{name:'В чат аккаунта',exact:true}).click();
  await page.waitForFunction(async()=>(await(await fetch('/api/channels/userbot')).json()).settings.service==='userbot');
  const state=await page.evaluate(async()=>JSON.stringify(await(await fetch('/api/channels/userbot')).json()));
  if(state.includes('SESSION-STRING')||state.includes('a'.repeat(32)))throw Error('Пульт получил вход или ключ аккаунта');
  await card.scrollIntoViewIfNeeded();await card.screenshot({path:path.join(OUT,'userbot.png')});
  await page.setViewportSize({width:390,height:844});const scrim=page.locator('.scrim');if(await scrim.isVisible())await scrim.click({position:{x:380,y:20}});
  await card.screenshot({path:path.join(OUT,'userbot-phone.png')});
  const size=await page.locator('.page').evaluate(e=>[e.scrollWidth,e.clientWidth]);if(size[0]>size[1]+1)throw Error('Экран шире телефона '+size);
  if(errors.length)throw Error(errors.join('\n'));fs.writeFileSync(path.join(OUT,'userbot-metrics.json'),JSON.stringify({login:true,pair:true,chats:true,secretsHidden:true}));
 }finally{await browser.close();}
}
main().catch(error=>{console.error(error);process.exitCode=1;});
