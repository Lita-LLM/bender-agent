// Видимый конкретный профиль → явное применение → JPG multipart в фальшивом API.
const {chromium}=require('playwright'),path=require('path');
(async()=>{
 const browser=await chromium.launch(),page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 try{
  await page.goto(process.env.BENDER_URL+'/#/channels');
  await page.locator('summary:text-is("Публичное лицо общего бота")').click();
  await page.getByLabel('Публичное имя',{exact:true}).fill('Мой Бендер');
  await page.getByLabel('Описание бота',{exact:true}).fill('Личный помощник хозяина.');
  await page.getByLabel('Короткое описание',{exact:true}).fill('Помощник рядом.');
  const sent=async()=>await(await fetch(process.env.FAKE_URL+'/fake/tg/sent')).json();
  if((await sent()).some(m=>m.method==='setMyName'))throw Error('Профиль применился до подтверждения');
  await page.getByLabel('JPG аватара',{exact:true}).setInputFiles({name:'avatar.jpg',mimeType:'image/jpeg',buffer:Buffer.from([255,216,255,224,0,1,2,3,4])});
  await page.getByRole('button',{name:'Применить публичный профиль',exact:true}).click();
  await page.getByText('Публичный профиль обновлён',{exact:true}).waitFor();
  const records=await sent();
  for(const method of ['setMyName','setMyDescription','setMyShortDescription'])if(records.filter(m=>m.method===method).length!==2)throw Error('Не обновлены русский/default: '+method);
  if(!records.some(m=>m.method==='setMyProfilePhoto'&&JSON.parse(m.photo).photo==='attach://avatar'))throw Error('Нет JPG в профиле');
  await page.screenshot({path:path.join(process.argv[2],'botface-wide.png')});
  if(errors.length)throw Error(errors.join('\n'));console.log('Профиль: просмотр, явное применение и JPG — OK');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exit(1)});
