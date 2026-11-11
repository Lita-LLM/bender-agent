// Хозяин входит кукой, видит настоящее ограничение шаблона и меняет SSH/изоляцию.
const {chromium}=require('playwright'),path=require('path'),http=require('http');
async function main(){
 const base=process.env.BENDER_URL;
 const status=await new Promise((resolve,reject)=>http.get(base+'/api/state',r=>{r.resume();resolve(r.statusCode);}).on('error',reject));
 if(status!==401)throw Error('localhost без куки допущен к пульту');
 const browser=await chromium.launch();
 try{
  const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
  await page.goto(base);
  await Promise.all([page.waitForNavigation(),page.getByLabel('Выбрать агента').selectOption('migrated')]);
  await page.getByText(/получил шаблон Помощник при переезде/).waitFor();
  let rights=await(await page.request.get(base+'/api/agents/migrated/rights')).json();
  if(rights.effective.profile!=='helper')throw Error('Агент повышен без хозяина');
  await page.getByRole('button',{name:'Оставить Помощником',exact:true}).click();
  await page.goto(base+'/#/guard');await page.getByText(/задан шаблоном Помощник/).waitFor();
  await page.getByRole('button',{name:'Доверие',exact:true}).click();
  await page.getByText(/Действующий профиль:/).filter({hasText:'Осторожный'}).waitFor();
  await page.screenshot({path:path.join(process.argv[2],'effective-guard.png'),fullPage:true});
  await page.goto(base+'/#/rights');await page.getByLabel('Шаблон прав',{exact:true}).selectOption('full');
  const ssh=page.getByLabel('SSH: ключи только на чтение, known_hosts на запись, сокет ssh-agent');
  if(!await ssh.isChecked())throw Error('У Полного выключен SSH');
  if(await page.getByLabel('Требовать изоляцию команд',{exact:true}).isChecked())throw Error('Полный требует недоступную песочницу');
  await page.getByRole('button',{name:'Сохранить права',exact:true}).click();
  await page.getByText('Права сохранены хозяином',{exact:true}).waitFor();
  rights=await(await page.request.get(base+'/api/agents/migrated/rights')).json();
  if(rights.effective.profile!=='full'||!rights.effective.ssh)throw Error('Не сохранены права');
  await page.setViewportSize({width:390,height:844});
  if(await page.locator('.scrim').isVisible())await page.locator('.scrim').click();
  await page.locator('.sidebar').waitFor({state:'hidden'});
  await ssh.scrollIntoViewIfNeeded();
  if(await page.locator('.page').evaluate(e=>e.scrollWidth>e.clientWidth+1))throw Error('Права шире телефона');
  await page.screenshot({path:path.join(process.argv[2],'ssh-rights-phone.png'),fullPage:true});
  if(errors.length)throw Error(errors.join('\n'));
  console.log('Приёмка 3: localhost 401, предложение шаблона, действующий профиль, SSH и телефон — OK');
 }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exit(1);});
