// Телефонные снимки каждого экрана и простой собственный аудит доступности.
const {chromium}=require('playwright'),fs=require('fs');
const routes=['','tasks','self','memory','heart','dreams','body','channels','models','mcp','skills','backups','versions','workshop','modules','guard','journal','polygon','doctor','settings'];
const before=process.env.BENDER_AUDIT_MODE==='before';
async function audit(page){return page.evaluate(()=>{
 const failures=[],visible=e=>{const s=getComputedStyle(e),r=e.getBoundingClientRect();return r.width>0&&r.height>0&&s.visibility!=='hidden'&&s.display!=='none';};
 const name=e=>(e.getAttribute('aria-label')||e.getAttribute('aria-labelledby')?.split(/\s+/).map(id=>document.getElementById(id)?.textContent||'').join(' ')||e.textContent||e.title||'').trim();
 for(const e of document.querySelectorAll('button,[role=button]'))if(visible(e)&&!name(e))failures.push({rule:'button-name',html:e.outerHTML.slice(0,300)});
 for(const e of document.querySelectorAll('input:not([type=hidden]),textarea,select'))if(visible(e)&&!e.disabled&&!e.getAttribute('aria-label')&&!e.getAttribute('aria-labelledby')&&![...(e.labels||[])].some(l=>l.textContent.trim()))failures.push({rule:'field-label',html:e.outerHTML.slice(0,300)});
 const rgb=s=>{const v=s.match(/[\d.]+/g);if(!v)return [0,0,0,0];const k=s.startsWith('color(srgb')?255:1;return [Number(v[0])*k,Number(v[1])*k,Number(v[2])*k,v[3]===undefined?1:Number(v[3])];};
 const over=(a,b)=>[...a.slice(0,3).map((v,i)=>v*a[3]+b[i]*(1-a[3])),1];
 const background=e=>{let bg=[255,255,255,1];const chain=[];for(let p=e;p;p=p.parentElement)chain.push(p);for(const p of chain.reverse())bg=over(rgb(getComputedStyle(p).backgroundColor),bg);return bg;};
 const lum=c=>c.slice(0,3).map(v=>{v/=255;return v<=.04045?v/12.92:((v+.055)/1.055)**2.4;}).reduce((s,v,i)=>s+v*[.2126,.7152,.0722][i],0);
 const walk=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT);let node;
 while(node=walk.nextNode()){
  const e=node.parentElement;if(!node.textContent.trim()||!visible(e)||e.closest('svg,option,script,style,button:disabled,[aria-disabled=true]'))continue;
  const s=getComputedStyle(e),bg=background(e);let fg=rgb(s.color),opacity=1;for(let p=e;p;p=p.parentElement)opacity*=Number(getComputedStyle(p).opacity);fg[3]*=opacity;fg=over(fg,bg);
  const a=lum(fg),b=lum(bg),ratio=(Math.max(a,b)+.05)/(Math.min(a,b)+.05),large=parseFloat(s.fontSize)>=24||(parseFloat(s.fontSize)>=18.66&&Number(s.fontWeight)>=700);
  if(ratio<(large?3:4.5)-.03)failures.push({rule:'contrast',ratio:Math.round(ratio*100)/100,text:node.textContent.trim().slice(0,90),color:s.color,bg:bg.slice(0,3),html:e.outerHTML.slice(0,180)});
 }
 const overflow=[...document.querySelectorAll('main .page,main .chat')].filter(visible).filter(e=>e.scrollWidth>e.clientWidth+2).map(e=>({rule:'overflow',width:e.clientWidth,scroll:e.scrollWidth}));
 return [...failures,...overflow];
 });}
(async()=>{const browser=await chromium.launch();try{
 const context=await browser.newContext({viewport:{width:390,height:844}});const page=await context.newPage(),report=[],errors=[];page.on('pageerror',e=>errors.push(e.message));
 for(const theme of ['dark','light']){
  await page.goto(process.env.BENDER_URL+'/#/');await page.waitForSelector('.composer');
  await page.request.post(process.env.BENDER_URL+'/api/settings/appearance',{headers:{'X-Bender':'1'},data:{theme}});
  await page.waitForFunction(v=>document.documentElement.dataset.theme===v,theme);
  for(const route of routes){
   await page.goto(process.env.BENDER_URL+'/#/'+route);await page.waitForTimeout(300);await page.locator('.sidebar').waitFor({state:'hidden'});
   await page.screenshot({path:process.argv[2]+'/'+(before?'before':'after')+'-'+theme+'-'+(route||'chat')+'.png',fullPage:true});
   const failures=await audit(page);report.push({route:route||'chat',theme,failures});
  }
 }
 await page.goto(process.env.BENDER_URL+'/#/self');
 const opener=page.getByRole('button',{name:'Переселить',exact:true}).first();await opener.click();
 await page.getByRole('dialog').waitFor();
 for(let n=0;n<25;n++){await page.keyboard.press('Tab');if(!await page.evaluate(()=>!!document.activeElement?.closest('[role="dialog"]')))throw Error('Фокус ушёл из диалога');}
 await page.keyboard.press('Escape');await page.getByRole('dialog').waitFor({state:'hidden'});
 if(!await opener.evaluate(e=>e===document.activeElement))throw Error('Фокус не вернулся на кнопку');
 fs.writeFileSync(process.argv[2]+'/accessibility.json',JSON.stringify({before,report,errors},null,2));
 if(errors.length)throw Error(errors.join('\n'));
 const failures=report.flatMap(r=>r.failures.map(f=>({...f,route:r.route,theme:r.theme})));
 console.log('Экранов:',report.length,'нарушений:',failures.length);
 if(!before&&failures.length)throw Error(JSON.stringify(failures.slice(0,12),null,2));
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1;});
