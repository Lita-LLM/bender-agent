// Настоящее поручение, уборка и «Оставить», шаблон постоянного агента.
const {chromium}=require('playwright');const path=require('path');
async function main(){const browser=await chromium.launch();try{
 const page=await browser.newPage({viewport:{width:390,height:844}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await fetch(process.env.FAKE_URL+'/fake/script',{method:'POST',body:JSON.stringify([{tool_calls:[{name:'write_file',arguments:{path:'kept.txt',content:'Файл временного агента'}}]},{text:'Файл подготовлен.'}])});
 await page.goto(process.env.BENDER_URL+'/#/polygon');await page.selectOption('select.select','spawn_task');
 await page.fill('textarea.textarea.mono',JSON.stringify({role:'coder',goal:'Файл для постоянного агента'}));await page.getByRole('button',{name:'Выполнить',exact:true}).click();
 await page.waitForSelector('pre:has-text("Кодер")');await page.goto(process.env.BENDER_URL+'/#/tasks');
 await page.locator('.li-title').filter({hasText:'Файл для постоянного агента'}).click();
 await page.getByRole('button',{name:'Оставить агента',exact:true}).waitFor();
 await page.getByRole('button',{name:'Оставить агента',exact:true}).click();
 await page.getByRole('link',{name:/Агент оставлен:/}).waitFor();
 const all=await(await fetch(process.env.BENDER_URL+'/api/agents')).json();
 if(all.agents.length!==2||all.agents.some(a=>a.temporary))throw Error('Временный агент не убран или не оставлен');
 await page.screenshot({path:path.join(process.argv[2],'temporary-kept-phone.png'),fullPage:true});
 await page.goto(process.env.BENDER_URL+'/#/agents');
 await page.getByLabel('Имя нового агента',{exact:true}).fill('Из роли');
 await page.getByLabel('Шаблон из роли команды',{exact:true}).selectOption('coder');
 await page.getByLabel('С Пробуждением',{exact:false}).uncheck();await page.getByRole('button',{name:'Создать агента',exact:true}).click();
 await page.locator('.agent-row').filter({hasText:'Из роли'}).waitFor();
 if(await page.locator('.page').evaluate(e=>e.scrollWidth>e.clientWidth+1))throw Error('Стая шире телефона');
 if(errors.length)throw Error(errors.join('\n'));
 console.log('Временные агенты: задача, файлы, уборка, оставить, шаблон роли, телефон — OK');
}finally{await browser.close();}}main().catch(e=>{console.error(e);process.exit(1);});
