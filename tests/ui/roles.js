// Своя роль через пульт, настоящий субагент и предложение settings под одобрение.
const {chromium}=require('playwright');
const path=require('path');
async function main(){
 const browser=await chromium.launch();
 try{
  const context=await browser.newContext({viewport:{width:1280,height:900}});
  const page=await context.newPage();const errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  page.on('console',m=>{if(m.type()==='error')errors.push(m.text());});
  await page.goto(process.env.BENDER_URL+'/#/tasks');
  await page.locator('summary').filter({hasText:/^Роли$/}).click();
  const builtin=page.locator('.role-row').filter({hasText:'Чтец'});
  if(await builtin.getByRole('button',{name:'Удалить',exact:true}).count())throw Error('Встроенную роль можно удалить');
  await page.getByRole('button',{name:'Создать роль',exact:true}).click();
  await page.getByLabel('Имя роли',{exact:true}).fill('my_reader');
  await page.getByLabel('Название роли',{exact:true}).fill('Мой проверяющий');
  await page.getByLabel('Промпт роли',{exact:true}).fill('Проверь факты и верни ответ по схеме.');
  await page.getByLabel('fs.read',{exact:true}).check();
  await page.getByLabel('Схема ответа роли',{exact:true}).fill('{"type":"object","properties":{"ok":{"type":"boolean"}},"required":["ok"]}');
  await page.getByRole('button',{name:'Сохранить роль',exact:true}).click();
  await page.locator('.role-row').filter({hasText:'Мой проверяющий'}).waitFor();
  await page.goto(process.env.BENDER_URL+'/#/polygon');
  await page.selectOption('select.select','spawn_task');
  await page.fill('textarea.textarea.mono',JSON.stringify({role:'my_reader',goal:'Проверка своей роли'}));
  await page.getByRole('button',{name:'Выполнить',exact:true}).click();
  await page.waitForSelector('pre:has-text("Мой проверяющий")');
  await page.goto(process.env.BENDER_URL+'/#/tasks');
  await page.waitForSelector('.li-title:has-text("Проверка своей роли")');
  await page.locator('summary').filter({hasText:/^Роли$/}).click();
  const row=page.locator('.role-row').filter({hasText:'Мой проверяющий'});
  await row.getByRole('button',{name:'Выключить',exact:true}).click();
  await row.getByRole('button',{name:'Включить',exact:true}).waitFor();
  await page.screenshot({path:path.join(process.argv[2],'roles.png'),fullPage:true});
  await page.goto(process.env.BENDER_URL+'/#/polygon');
  await page.selectOption('select.select','settings');
  await page.fill('textarea.textarea.mono',JSON.stringify({group:'team_roles',changes:{suggested:{title:'Предложенная',prompt:'Читай и отвечай',capabilities:[],model:'fast'}}}));
  await page.getByRole('button',{name:'Выполнить',exact:true}).click();
  const guard=await context.newPage();await guard.goto(process.env.BENDER_URL+'/#/guard');
  await guard.waitForSelector('.approval:has-text("Роли команды")');
  await guard.getByRole('button',{name:'Разрешить один раз',exact:true}).click();
  await page.waitForSelector('pre:has-text("Изменено:")');await guard.close();
  await page.goto(process.env.BENDER_URL+'/#/tasks');
  await page.locator('summary').filter({hasText:/^Роли$/}).click();
  await page.locator('.role-row').filter({hasText:'Предложенная'}).waitFor();
  await page.setViewportSize({width:390,height:844});
  if(await page.locator('.page').evaluate(e=>e.scrollWidth>e.clientWidth+1))throw Error('Роли шире телефона');
  if(errors.length)throw Error(errors.join('\n'));
  console.log('Роли: создание, субагент, выключение, предложение хозяину — OK');
 }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exit(1);});
