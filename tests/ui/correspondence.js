// Матрица хозяина и реальная переписка моделей, видимая с телефона.
const {chromium}=require('playwright');
const path=require('path');
async function main(){const browser=await chromium.launch();try{
 const page=await browser.newPage({viewport:{width:390,height:844}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 const response=await fetch(process.env.BENDER_URL+'/api/agents',{method:'POST',headers:{'X-Bender':'1','Content-Type':'application/json'},body:JSON.stringify({name:'Собеседник',id:'second',awaken:false})});
 if(!response.ok)throw Error('Не создан собеседник');
 await page.goto(process.env.BENDER_URL+'/#/correspondence');
 const link=page.locator('.agent-link[data-sender="main"][data-recipient="second"]');
 await link.waitFor();
 await page.evaluate(async()=>{const response=await fetch('/api/correspondence',{method:'PATCH',headers:{'X-Bender':'1','Content-Type':'application/json'},body:JSON.stringify({max_depth:7})});if(!response.ok)throw Error('Не записана параллельная настройка');});
 await link.getByLabel('Разрешение',{exact:true}).selectOption('yes');
 await link.getByLabel('Сообщений в час',{exact:true}).fill('12');
 await page.getByRole('button',{name:'Сохранить матрицу',exact:true}).click();
 await page.getByText('Матрица общения сохранена',{exact:true}).waitFor();
 const settings=(await(await fetch(process.env.BENDER_URL+'/api/correspondence')).json()).settings;if(settings.max_depth!==7)throw Error('Пульт стёр параллельную настройку');
 await fetch(process.env.FAKE_URL+'/fake/script',{method:'POST',body:JSON.stringify([
 {tool_calls:[{name:'message_agent',arguments:{agent:'second',text:'Проверь план',wait:true}}]},
 {tool_calls:[{name:'list_dir',arguments:{}}]},{text:'Проверил у себя.'},{text:'Ответ соседа получен.'}])});
 await page.goto(process.env.BENDER_URL+'/#/');
 await page.locator('.composer textarea').fill('Поговори с соседом');await page.locator('.composer .btn.primary').click();
 await page.getByText('Ответ соседа получен.',{exact:true}).waitFor();
 await page.goto(process.env.BENDER_URL+'/#/correspondence');
 await page.getByText('Проверь план',{exact:true}).waitFor({state:'attached'});
 const data=await(await fetch(process.env.BENDER_URL+'/api/correspondence')).json();
 if(!data.letters.some(r=>r.state==='done'&&r.recipient==='second'&&r.result==='Проверил у себя.'))throw Error('Переписки нет в ленте');
 if(await page.locator('.page').evaluate(e=>e.scrollWidth>e.clientWidth+1))throw Error('Общение шире телефона');
 await page.screenshot({path:path.join(process.argv[2],'correspondence-phone.png'),fullPage:true});
 await Promise.all([page.waitForNavigation(),page.getByLabel('Выбрать агента',{exact:true}).selectOption('second')]);
 await page.goto(process.env.BENDER_URL+'/#/');
 await page.waitForSelector('.service-event:has-text("Письмо от Главный — ответил")');
 await page.getByRole('button',{name:'Открыть переписку',exact:true}).click();
 await page.waitForSelector('.correspondence-readonly');
 if(await page.locator('.composer textarea').count()||await page.getByRole('button',{name:'Ответить',exact:true}).count())throw Error('Переписка доступна для записи');
 if(await page.locator('optgroup[label="Переписка"] option').count()!==1)throw Error('Нет раздела Переписка');
 await page.getByText('Агент Главный',{exact:true}).waitFor();await page.getByText('Проверил у себя.',{exact:true}).waitFor();
 if(!(await page.locator('.messages').innerText()).includes('list_dir'))throw Error('Тулы получателя потеряны');
 if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Нить шире телефона');
 await page.screenshot({path:path.join(process.argv[2],'correspondence-recipient-phone.png'),fullPage:true});
 if(errors.length)throw Error(errors.join('\n'));
 console.log('Общение: матрица, message_agent, получатель/тулы/ответ, ссылка в главном и нить только на чтение — OK');
}finally{await browser.close();}}main().catch(e=>{console.error(e);process.exit(1);});
