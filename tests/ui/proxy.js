// Выключатель, карточка доменов и отдельное одобрение сети в настоящем пульте.
const {chromium}=require('playwright');
async function main(){
 const browser=await chromium.launch();
 try{
  const page=await browser.newPage({viewport:{width:1280,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  const base=process.env.BENDER_URL;
  await page.goto(base+'/#/workshop');
  await page.getByText('Сеть тулов-кода закрыта',{exact:true}).waitFor();
  const card=page.locator('.proposal-card').first();
  await card.getByText('example.org',{exact:true}).waitFor();
  await card.getByRole('button',{name:'Просмотреть код и проверки',exact:true}).click();
  await card.getByText('bender-module.json',{exact:true}).waitFor();
  if(!(await card.innerText()).includes('network_hosts'))throw Error('Нет доменов в просмотре пакета');
  await page.screenshot({path:process.argv[2]+'/proxy-review.png',fullPage:true});
  await page.goto(base+'/#/settings');
  const toggle=page.getByRole('checkbox',{name:'Сетевой посредник тулов-кода',exact:true});
  await page.getByText('Сетевой посредник тулов-кода',{exact:true}).click();
  if(!await toggle.isChecked())throw Error('Посредник не включён');
  await page.waitForFunction(async()=>{const r=await(await fetch('/api/settings')).json();return r.groups.find(g=>g.name==='workshop').fields.find(f=>f.key==='network_proxy').value;});
  await page.goto(base+'/#/workshop');
  await page.getByText('Сеть через посредник',{exact:true}).waitFor();
  await card.getByRole('button',{name:'Просмотреть код и проверки',exact:true}).click();
  await card.getByRole('button',{name:'Одобрить код и способности',exact:true}).click();
  await page.locator('.approval:has-text("одобрить просмотренный код тула")').waitFor();
  await page.getByRole('button',{name:'Разрешить один раз',exact:true}).click();
  await card.getByText('Код одобрен',{exact:true}).waitFor();
  const state=await(await page.request.get(base+'/api/mcp')).json();
  const server=state.servers.find(s=>s.module==='ws-net-preview');
  if(server?.workshop_runtime!=='bwrap-v2'||!server.enabled)throw Error('Нет безопасного сетевого runtime');
  await page.setViewportSize({width:390,height:844});
  await page.locator('.sidebar').waitFor({state:'hidden'});
  await page.screenshot({path:process.argv[2]+'/proxy-phone.png',fullPage:true});
  if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth+1))throw Error('Переполнение телефона');
  await page.goto(base+'/#/settings');await page.getByText('Сетевой посредник тулов-кода',{exact:true}).click();
  await page.goto(base+'/#/workshop');await page.getByText('Сеть тулов-кода закрыта',{exact:true}).waitFor();
  if(errors.length)throw Error(errors.join('\n'));
 }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
