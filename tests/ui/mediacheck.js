// Явный выбор трёх состояний и отдельные маленькие пробы в настоящем браузере.
const {chromium}=require('playwright');
(async()=>{const browser=await chromium.launch();try{
 const page=await browser.newPage({viewport:{width:1280,height:1000}}),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto(process.env.BENDER_URL+'/#/models');
 const main=page.locator('.card').filter({has:page.getByRole('heading',{name:'Главная',exact:true})});
 await main.locator('.model-media summary').first().click();
 const vision=main.getByLabel('Зрение модели echo',{exact:true});
 await vision.selectOption('unknown');await page.waitForFunction(()=>document.querySelector('.model-media select')?.value==='unknown');
 await main.getByText(/Возможности: зрение — неизвестно/).waitFor();
 await main.getByRole('button',{name:'Проверить зрение',exact:true}).click();
 await main.locator('.model-media p.note').filter({hasText:/Проверка пройдена|Запрос принят, ответ не совпал/}).waitFor();
 await main.getByRole('button',{name:'Проверить слух',exact:true}).click();
 await main.getByRole('button',{name:'Проверить слух',exact:true}).waitFor({state:'visible'});
 await main.locator('.model-media p.note').filter({hasText:/Проверка пройдена|Запрос принят, ответ не совпал/}).waitFor();
 await vision.selectOption('yes');await main.getByText(/Возможности: зрение — да/).waitFor();
 await vision.selectOption('no');await main.getByText(/Возможности: зрение — нет/).waitFor();
 await page.screenshot({path:process.argv[2]+'/capabilities.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});
 const scrim=page.locator('.sidebar-scrim');if(await scrim.isVisible())await scrim.click();
 await page.locator('.sidebar').waitFor({state:'hidden'});
 if(await page.locator('.page').evaluate(e=>e.scrollWidth>e.clientWidth+1))throw Error('Возможности шире телефона');
 await main.locator('.model-media').screenshot({path:process.argv[2]+'/capabilities-phone.png'});
 if(errors.length)throw Error(errors.join('\n'));
 console.log('Медиа: да/нет/неизвестно, зрительная и слуховая проба, телефон — OK');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
