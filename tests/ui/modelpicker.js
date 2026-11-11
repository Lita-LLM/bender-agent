// Настоящий select, поиск, ручной путь и обновление каталога на телефоне.
const { chromium }=require('playwright');
const path=require('path');
async function main(){
 const browser=await chromium.launch();
 try{
  const page=await browser.newPage({viewport:{width:390,height:844}});const errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.BENDER_URL+'/#/models');
  const role=page.locator('.card').filter({has:page.getByRole('heading',{name:'Главная',exact:true})});
  await role.getByLabel('Поиск модели',{exact:true}).fill('model-19');
  const picker=role.getByLabel('модель',{exact:true});
  if(await picker.locator('option').count()>4)throw Error('Поиск не сузил каталог');
  await picker.selectOption('model-19');
  await role.getByRole('button',{name:'Сохранить',exact:true}).click();
  await page.reload();await role.getByLabel('модель',{exact:true}).waitFor();
  if(await role.getByLabel('модель',{exact:true}).inputValue()!=='model-19')throw Error('Выбор не сохранён');
  await role.getByLabel('модель',{exact:true}).selectOption('__manual__');
  await role.getByLabel('Модель вручную',{exact:true}).fill('custom-new');
  await role.getByRole('button',{name:'Сохранить',exact:true}).click();
  await page.reload();await role.getByLabel('модель',{exact:true}).waitFor();
  if(await role.getByLabel('модель',{exact:true}).inputValue()!=='custom-new')throw Error('Ручная модель не запомнилась в списке');
  if(!(await role.getByLabel('модель',{exact:true}).locator('option[value="custom-new"]').textContent()).includes('введена вручную'))throw Error('Нет пометки «введена вручную»');
  await role.getByRole('button',{name:'Обновить список',exact:true}).click();
  await role.getByLabel('модель',{exact:true}).locator('option[value="echo"]').waitFor({state:'attached'});
  if(!await role.getByLabel('модель',{exact:true}).locator('option[value="custom-new"]').count())throw Error('Обновление каталога стёрло ручную модель');
  const providers=await role.getByLabel('Провайдер роли Главная',{exact:true}).locator('option').evaluateAll(es=>es.map(e=>({label:e.label,value:e.value})));
  await role.getByLabel('Провайдер роли Главная',{exact:true}).selectOption(providers.find(p=>p.label==='Без каталога').value);
  await role.getByText('Каталог пока недоступен.',{exact:false}).waitFor();
  await role.getByLabel('Модель вручную',{exact:true}).fill('manual-only');
  if(await page.locator('.page').evaluate(e=>e.scrollWidth>e.clientWidth+1))throw Error('Выбор модели шире телефона');
  await page.screenshot({path:path.join(process.argv[2],'model-picker-phone.png'),fullPage:true});
  await page.goto(process.env.BENDER_URL+'/#/tasks');
  await page.locator('summary').filter({hasText:/^Роли$/}).click();
  await page.getByRole('button',{name:'Создать роль',exact:true}).click();
  if(!(await page.getByLabel('Роль модели',{exact:true}).textContent()).includes('custom-new'))throw Error('Команда не показывает модель связанной роли');
  if(errors.length)throw Error(errors.join('\n'));
  console.log('Модель: select, поиск, сохранение, ручная запомнилась, обновление, команда, телефон — OK');
 }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exit(1);});
