// Настройки лица, темы, reduced-motion и реальные показатели фоновой вкладки.
const {chromium}=require('playwright'),fs=require('fs'),os=require('os'),path=require('path'),{spawn}=require('child_process');
async function launch(){
 if(!process.env.BENDER_HEADED)return {browser:await chromium.launch(),cleanup:()=>{}};
 const profile=fs.mkdtempSync(path.join(os.tmpdir(),'bender-face-browser-'));
 const child=spawn(process.env.BENDER_CHROMIUM||chromium.executablePath(),['--no-sandbox','--disable-dev-shm-usage','--remote-debugging-port=0','--user-data-dir='+profile,'about:blank'],{stdio:'ignore'});
 try{
  const portFile=path.join(profile,'DevToolsActivePort');
  for(let i=0;i<100&&!fs.existsSync(portFile);i++)await new Promise(r=>setTimeout(r,50));
  const port=fs.readFileSync(portFile,'utf8').split('\n')[0];
  // Стандартный Playwright принудительно оставляет вкладки видимыми: этот замер без его override.
  const browser=await chromium.connectOverCDP('http://127.0.0.1:'+port,{noDefaults:true});
  return {browser,cleanup:()=>{child.kill('SIGTERM');fs.rmSync(profile,{recursive:true,force:true});}};
 }catch(e){child.kill('SIGTERM');throw e;}
}
(async()=>{const {browser,cleanup}=await launch();try{
 const context=process.env.BENDER_HEADED?browser.contexts()[0]:await browser.newContext({viewport:{width:1280,height:1000}});
 const page=await context.newPage(),errors=[];await page.setViewportSize({width:1280,height:1000});
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto(process.env.BENDER_URL+'/#/self');
 await page.getByLabel('Форма',{exact:true}).selectOption('square');
 await page.getByLabel('Глаза',{exact:true}).selectOption('slit');
 await page.getByRole('button',{name:'Сохранить лицо',exact:true}).click();
 await page.waitForFunction(()=>document.querySelector('.brand svg')?.dataset.shape==='square');
 const headers={'X-Bender':'1'};
 for(const theme of ['light','dark','system']){
  await page.request.post(process.env.BENDER_URL+'/api/settings/appearance',{headers,data:{theme}});
  await page.waitForFunction(v=>document.documentElement.dataset.theme===v,theme);
 }
 await page.screenshot({path:process.argv[2]+'/face.png',fullPage:true});
 const cdp=await page.context().newCDPSession(page);await cdp.send('Performance.enable');
 const metrics=async()=>Object.fromEntries((await cdp.send('Performance.getMetrics')).metrics.map(m=>[m.name,m.value]));
 await page.waitForFunction(()=>document.querySelectorAll('.toast').length===0);
 await page.waitForTimeout(500);
 const first=await metrics();await page.waitForTimeout(3000);const idle=await metrics();
 if(idle.LayoutCount!==first.LayoutCount)throw Error('В простое продолжается раскладка');
 await page.evaluate(async()=>{const s=await import('/static/lib/store.js');s.setState({face:'thinking',st:{...s.state.st,running:true}});});
 await page.waitForTimeout(300);
 const other=await page.context().newPage();await other.goto('about:blank');await other.bringToFront();
 if(process.env.BENDER_HEADED){await page.waitForFunction(()=>document.hidden&&document.documentElement.dataset.hidden==='true');}
 else {await page.evaluate(()=>{document.documentElement.dataset.hidden='true';});}
 const hidden1=await metrics();await page.waitForTimeout(5000);const hidden2=await metrics();
 const report={backgroundVisibility:process.env.BENDER_HEADED?'real-hidden-tab':'css-pause-headless',idleTaskSeconds:idle.TaskDuration-first.TaskDuration,idleLayouts:idle.LayoutCount-first.LayoutCount,backgroundTaskSeconds:hidden2.TaskDuration-hidden1.TaskDuration,backgroundCPUPercent:(hidden2.TaskDuration-hidden1.TaskDuration)/5*100,backgroundLayouts:hidden2.LayoutCount-hidden1.LayoutCount};
 fs.writeFileSync(process.argv[2]+'/browser-idle.json',JSON.stringify(report,null,2));
 if(report.backgroundCPUPercent>2||report.backgroundLayouts>1)throw Error('Фоновая вкладка занята: '+JSON.stringify(report));
 const states=await page.evaluate(()=>[...document.querySelectorAll('.brand svg .face-eyes')].map(e=>getComputedStyle(e).animationPlayState));
 if(states.some(s=>s!=='paused'))throw Error('Анимация в фоне не остановлена');
 await other.close();await page.bringToFront();await page.emulateMedia({reducedMotion:'reduce'});
 if(await page.locator('.brand .face-eyes').evaluate(e=>getComputedStyle(e).animationName)!=='none')throw Error('reduced motion проигнорирован');
 await page.setViewportSize({width:390,height:844});await page.locator('.sidebar').waitFor({state:'hidden'});
 await page.screenshot({path:process.argv[2]+'/face-phone.png',fullPage:true});
 if(errors.length)throw Error(errors.join('\n'));
}finally{await browser.close();cleanup();}})().catch(e=>{console.error(e);process.exitCode=1;});
