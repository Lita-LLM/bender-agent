// Потолок прав хозяина: шаблон, деньги, сеть и сохранение с телефона.
const {chromium}=require('playwright');
const path=require('path');
async function main(){const browser=await chromium.launch();try{
 const page=await browser.newPage({viewport:{width:390,height:844}}),errors=[];page.on('pageerror',e=>errors.push(e.message));
 const response=await fetch(process.env.BENDER_URL+'/api/agents',{method:'POST',headers:{'X-Bender':'1','Content-Type':'application/json'},body:JSON.stringify({name:'Испытатель',id:'tester',awaken:false})});
 if(!response.ok)throw Error('Не создан агент');
 await page.goto(process.env.BENDER_URL+'/#/rights?agent=tester');
 await page.getByLabel('Шаблон прав',{exact:true}).selectOption('observer');
 await page.getByLabel('Потолок в сутки, $',{exact:true}).fill('1.25');
 await page.getByLabel('Разрешённые узлы сети',{exact:true}).fill('example.org');
 await page.getByRole('button',{name:'Сохранить права',exact:true}).click();
 await page.getByText('Права сохранены хозяином',{exact:true}).waitFor();
 const info=await(await fetch(process.env.BENDER_URL+'/api/agents/tester/rights')).json();
 if(info.effective.daily_usd!==1.25||info.effective.profile!=='observer'||info.effective.capabilities.includes('exec'))throw Error('Сохранился другой потолок');
 await page.reload();await page.getByLabel('Разрешённые узлы сети',{exact:true}).waitFor();
 if(await page.getByLabel('Потолок в сутки, $',{exact:true}).inputValue()!=='1.25')throw Error('Потолок не пережил обновление');
 if(await page.locator('.page').evaluate(e=>e.scrollWidth>e.clientWidth+1))throw Error('Права шире телефона');
 await page.screenshot({path:path.join(process.argv[2],'rights-phone.png'),fullPage:true});
 if(errors.length)throw Error(errors.join('\n'));
 console.log('Права: шаблон, способности, сеть, бюджет, сохранение и телефон — OK');
}finally{await browser.close();}}main().catch(e=>{console.error(e);process.exit(1);});
