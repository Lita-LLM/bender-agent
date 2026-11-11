// Настоящий MediaRecorder с фальшивым микрофоном, STT → редактируемый текст → ход → TTS.
const {chromium}=require('playwright');
(async()=>{const b=await chromium.launch({args:['--use-fake-ui-for-media-stream','--use-fake-device-for-media-stream']});try{
 const context=await b.newContext({viewport:{width:1280,height:900},permissions:['microphone']});const page=await context.newPage(),errors=[];
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto(process.env.BENDER_URL+'/#/');
 await page.getByRole('button',{name:'Микрофон',exact:true}).click();
 await page.getByRole('button',{name:'Закончить запись',exact:true}).waitFor();
 await page.waitForTimeout(700);
 await page.getByRole('button',{name:'Закончить запись',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('.composer textarea')?.value==='Привет голосом');
 await page.getByRole('button',{name:'Отправить',exact:true}).click();
 await page.getByRole('button',{name:'Озвучить',exact:true}).first().waitFor();
 await page.locator('.voice-controls audio').waitFor();
 await page.waitForFunction(()=>document.querySelector('.voice-controls audio')?.readyState>=2);
 await page.screenshot({path:process.argv[2]+'/voice.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});await page.locator('.sidebar').waitFor({state:'hidden'});
 await page.screenshot({path:process.argv[2]+'/voice-phone.png',fullPage:true});
 if(errors.length)throw Error(errors.join('\n'));
}finally{await b.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
