// Два агента, отдельные чаты, переключение на телефоне и архив хозяина.
const {chromium}=require('playwright');
const path=require('path');
async function main(){const browser=await chromium.launch();try{
 const page=await browser.newPage({viewport:{width:390,height:844}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(process.env.BENDER_URL+'/#/agents');
 await page.getByLabel('Имя нового агента',{exact:true}).fill('Сосед');
 await page.getByLabel('С Пробуждением',{exact:false}).uncheck();
 await page.getByRole('button',{name:'Создать агента',exact:true}).click();
 const row=page.locator('.agent-row').filter({hasText:'Сосед'});await row.waitFor();
 const id=await row.locator('.badge').textContent();
 await Promise.all([page.waitForNavigation(),page.getByLabel('Выбрать агента',{exact:true}).selectOption(id)]);
 await page.goto(process.env.BENDER_URL+'/#/');
 await page.getByRole('button',{name:'Новый чат',exact:true}).click();
 await fetch(process.env.FAKE_URL+'/fake/script',{method:'POST',body:JSON.stringify([{text:'Личный ответ второго агента.'}])});
 await page.locator('.composer textarea').fill('Проверь свой чат');await page.locator('.composer .btn.primary').click();
 await page.getByText('Личный ответ второго агента.',{exact:true}).waitFor();
 const chat=await page.getByLabel('Выбрать чат',{exact:true}).inputValue();
 page.once('dialog',d=>d.accept('Личный разговор'));
 await page.getByRole('button',{name:'Имя чата',exact:true}).click();
 await page.getByLabel('Выбрать чат',{exact:true}).locator('option',{hasText:'Личный разговор'}).waitFor({state:'attached'});
 await Promise.all([page.waitForNavigation(),page.getByLabel('Выбрать агента',{exact:true}).selectOption('main')]);
 await page.goto(process.env.BENDER_URL+'/#/');
 if(await page.getByText('Личный ответ второго агента.',{exact:true}).count())throw Error('Ответ другого агента попал в main');
 await Promise.all([page.waitForNavigation(),page.getByLabel('Выбрать агента',{exact:true}).selectOption(id)]);
 await page.goto(process.env.BENDER_URL+'/#/');
 await page.getByText('Личный ответ второго агента.',{exact:true}).waitFor();
 if(await page.getByLabel('Выбрать чат',{exact:true}).inputValue()!==chat)throw Error('Выбор чата потерян');
 if(await page.locator('.page,.chat').first().evaluate(e=>e.scrollWidth>e.clientWidth+1))throw Error('Чат шире телефона');
 await page.screenshot({path:path.join(process.argv[2],'agents-chat-phone.png'),fullPage:true});
 await page.goto(process.env.BENDER_URL+'/#/agents');
 await page.locator('.agent-row').filter({hasText:'Сосед'}).getByRole('button',{name:'В архив',exact:true}).click();
 await page.locator('.agent-row').filter({hasText:'Сосед'}).getByRole('button',{name:'Разбудить',exact:true}).waitFor();
 await page.locator('.agent-row').filter({hasText:'Сосед'}).getByRole('button',{name:'Разбудить',exact:true}).click();
 await page.locator('.agent-row').filter({hasText:'Сосед'}).getByRole('button',{name:'В архив',exact:true}).waitFor();
 if(errors.length)throw Error(errors.join('\n'));
 console.log('Стая: создание, два контекста/чата, переименование, переключение, архив, телефон — OK');
}finally{await browser.close();}}
main().catch(e=>{console.error(e);process.exit(1);});
