// Настоящий ручной запуск: подтверждение, дождаться, Сторож, прервать и чисто выключить.
const {chromium}=require('playwright'),path=require('path');
async function main(){
 const base=process.env.BENDER_URL,browser=await chromium.launch();
 try{
  const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
  const state=async()=>await(await page.request.get(base+'/api/state',{timeout:1200})).json();
  await page.goto(base+'/#/settings');await page.getByRole('heading',{name:'Состояние запуска',exact:true}).waitFor();
  await page.getByText(/запущен вручную через Сторожа/).waitFor();
  const first=await state();
  const port=page.getByLabel('Порт',{exact:true});await port.fill(new URL(base).port);await port.blur();
  await page.locator('.banner').filter({hasText:'Нужен перезапуск'}).waitFor();
  const request=async()=>{
   await fetch(process.env.FAKE_URL+'/fake/script',{method:'POST',body:JSON.stringify([{text:'Я завершаю текущий ход и сохраняю его ответ перед тем как Бендер перезапустится.',delay:.3}])});
   await page.request.post(base+'/api/say',{headers:{'X-Bender':'1'},data:{text:'Проверочный ход',session:first.main_session}});
  };
  await request();
  const card=page.locator('.card').filter({has:page.getByRole('heading',{name:'Состояние запуска',exact:true})});
  await card.getByRole('button',{name:'Перезапустить',exact:true}).click();
  let modal=page.locator('[role="dialog"]');await modal.getByText(/Текущих ходов: 1/).waitFor();
  await modal.getByLabel('Дождаться текущих ходов',{exact:true}).check();
  await page.screenshot({path:path.join(process.argv[2],'restart-wait.png'),fullPage:true});
  await modal.getByRole('button',{name:'Перезапустить сейчас',exact:true}).click();
  let restarted=false;
  for(let i=0;i<150;i++){
   try{if((await state()).boot.starts>first.boot.starts){restarted=true;break;}}catch{}
   await page.waitForTimeout(100);
  }
  if(!restarted)throw Error('Сторож не перезапустил ядро');
  if((await state()).lifecycle.restart_fields.length)throw Error('Ядро не сбросило флаг перезапуска');
  await page.locator('.banner').filter({hasText:'Нужен перезапуск'}).waitFor({state:'hidden'});
  await page.getByRole('heading',{name:'Состояние запуска',exact:true}).waitFor();
  await page.setViewportSize({width:390,height:844});
  if(await page.locator('.scrim').isVisible())await page.locator('.scrim').click();
  await page.locator('.sidebar').waitFor({state:'hidden'});
  await request();await card.getByRole('button',{name:'Выключить',exact:true}).click();
  modal=page.locator('[role="dialog"]');await modal.getByText(/Текущих ходов: 1/).waitFor();
  await modal.getByLabel('Прервать текущие ходы',{exact:true}).check();
  if(await modal.textContent().then(s=>s.includes('systemctl')))throw Error('Ручному запуску показан чужой способ остановки');
  await page.screenshot({path:path.join(process.argv[2],'shutdown-phone.png'),fullPage:true});
  await modal.getByRole('button',{name:'Выключить сейчас',exact:true}).click();
  await modal.getByText(/Запустить снова: bender/).waitFor();
  let stopped=false;
  for(let i=0;i<70;i++){
   try{await state();}catch{stopped=true;break;}
   await page.waitForTimeout(100);
  }
  if(!stopped)throw Error('Выключение не остановило ядро');
  if(errors.length)throw Error(errors.join('\n'));
  console.log('Управление: ручной запуск, плашка, ожидание хода, настоящий перезапуск, прерывание и выключение с телефона — OK');
 }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exit(1);});
