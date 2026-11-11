// Панель ввода остаётся над клавиатурой телефона: оболочка следует за видимой областью (visualViewport).
const {chromium}=require('playwright');
async function main(){const browser=await chromium.launch();try{
 const page=await browser.newPage({viewport:{width:390,height:844},hasTouch:true,isMobile:true}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.goto(process.env.BENDER_URL+'/#/');
 const box=page.locator('.composer textarea');await box.waitFor();
 const bottom=async()=>page.evaluate(()=>{const r=document.querySelector('.composer').getBoundingClientRect();return Math.round(r.bottom)});
 const before=await bottom();if(before<800)throw Error('Без клавиатуры панель ввода должна быть у низа экрана: '+before);
 // клавиатуру браузер не рисует: имитируем её тем, что сообщает visualViewport (высота 520 из 844)
 await page.evaluate(()=>{const vv=window.visualViewport;Object.defineProperty(vv,'height',{configurable:true,get:()=>520});vv.dispatchEvent(new Event('resize'));});
 await page.waitForFunction(()=>document.documentElement.style.getPropertyValue('--app-h')==='520px');
 const after=await bottom();if(after>521)throw Error('Панель ввода скрыта клавиатурой: нижний край '+after);
 await page.evaluate(()=>{const vv=window.visualViewport;delete vv.height;vv.dispatchEvent(new Event('resize'));});
 await page.waitForFunction(()=>document.documentElement.style.getPropertyValue('--app-h')==='844px');
 if(errors.length)throw Error(errors.join('; '));
 console.log('Клавиатура телефона: панель ввода поднимается над ней и возвращается — OK');
}finally{await browser.close();}}
main().catch(e=>{console.error(e);process.exit(1);});
