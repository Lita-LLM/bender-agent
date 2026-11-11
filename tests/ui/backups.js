// Снимок, дедупликация, проверка и восстановление отдельного слота с телефона.
const {chromium}=require('playwright');
(async()=>{const browser=await chromium.launch();try{
 const page=await browser.newPage({viewport:{width:1280,height:1000}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto(process.env.BENDER_URL+'/#/backups');
 await page.getByRole('button',{name:'Сделать снимок',exact:true}).click();
 await page.locator('[role=status]').filter({hasText:'"id"'}).waitFor();
 await page.getByRole('button',{name:'Проверить всё',exact:true}).click();
 await page.locator('[role=status]').filter({hasText:'"ok": true'}).waitFor();
 const state=await (await page.request.get(process.env.BENDER_URL+'/api/backups')).json();
 const target=state.settings.path+'-restored';
 await page.getByRole('button',{name:'Восстановить…',exact:true}).first().click();
 await page.getByLabel('Новая папка данных').fill(target);
 await page.getByRole('button',{name:'Предпросмотр восстановления',exact:true}).click();
 await page.getByRole('button',{name:'Восстановить слот',exact:true}).click();
 await page.locator('[role=status]').filter({hasText:'"ok": true'}).waitFor();
 await page.screenshot({path:process.argv[2]+'/backups.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});await page.locator('.sidebar').waitFor({state:'hidden'});
 await page.screenshot({path:process.argv[2]+'/backups-phone.png',fullPage:true});
 if(errors.length)throw Error(errors.join('\n'));
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
