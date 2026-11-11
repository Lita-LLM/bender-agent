// Пассивная проверка, предпросмотр и скачивание архива с телефона.
const {chromium}=require('playwright'),fs=require('fs');
(async()=>{const browser=await chromium.launch();try{
 const page=await browser.newPage({viewport:{width:390,height:844}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto(process.env.BENDER_URL+'/#/doctor');
 await page.getByRole('heading',{name:'✅ Python и ОС'}).waitFor();
 await page.getByRole('button',{name:'Проверить сейчас',exact:true}).click();
 await page.getByRole('button',{name:'Проверить сейчас',exact:true}).waitFor({state:'visible'});
 await page.getByRole('button',{name:'Подготовить отчёт',exact:true}).click();
 await page.getByText('doctor.json',{exact:true}).waitFor();
 const download=page.waitForEvent('download');
 await page.getByRole('button',{name:'Скачать просмотренный отчёт',exact:true}).click();
 await (await download).saveAs(process.argv[2]+'/report.zip');
 if(fs.statSync(process.argv[2]+'/report.zip').size<100)throw Error('Пустой архив');
 const state=await (await page.request.get(process.env.BENDER_URL+'/api/doctor')).json();
 if(state.rows.find(r=>r.id==='port').details.port!==Number(new URL(process.env.BENDER_URL).port))throw Error('Потерян порт запуска');
 await page.locator('main .page').evaluate(e=>e.scrollTop=0);
 await page.locator('.sidebar').waitFor({state:'hidden'});
 await page.screenshot({path:process.argv[2]+'/doctor-phone.png',fullPage:true});
 await page.setViewportSize({width:1280,height:1000});
 await page.screenshot({path:process.argv[2]+'/doctor.png',fullPage:true});
 if(errors.length)throw Error(errors.join('\n'));
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
