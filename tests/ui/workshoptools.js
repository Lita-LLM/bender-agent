// Просмотр → карточка кода → обнаружение → определения → выключение.
const {chromium}=require('playwright');
async function main(){
 const browser=await chromium.launch();
 try{
  const page=await browser.newPage({viewport:{width:1280,height:1000}}),errors=[];
  page.on('pageerror',e=>{errors.push(e.message);console.error(e.message);});
  await page.goto(process.env.BENDER_URL+'/#/workshop');
  const card=page.locator('.proposal-card').first();
  await card.getByRole('button',{name:'Просмотреть код и проверки',exact:true}).click();
  await card.getByText('server.py',{exact:true}).waitFor();
  await page.screenshot({path:process.argv[2]+'/tool-review.png',fullPage:true});
  await card.getByRole('button',{name:'Одобрить код и способности',exact:true}).click();
  await page.locator('.approval:has-text("одобрить просмотренный код тула")').waitFor();
  await page.getByRole('button',{name:'Разрешить один раз',exact:true}).click();
  await card.getByText('Код одобрен',{exact:true}).waitFor();
  await card.getByRole('button',{name:'Проверить определения MCP',exact:true}).click();
  await page.locator('.approval:has-text("подключить MCP")').waitFor();
  await page.getByRole('button',{name:'Разрешить один раз',exact:true}).click();
  await card.getByRole('button',{name:'Одобрить просмотренные определения',exact:true}).click();
  await card.getByText('Применено',{exact:true}).waitFor();
  const tools=await(await page.request.get(process.env.BENDER_URL+'/api/tools')).json();
  if(!tools.tools.some(t=>t.name.startsWith('mcp__')&&t.name.endsWith('__sum')))throw Error('Тул не появился в реестре');
  await card.getByRole('button',{name:'Выключить тул',exact:true}).click();
  await card.getByText('Выключен',{exact:true}).waitFor();
  await page.setViewportSize({width:390,height:844});
  await page.locator('.sidebar').waitFor({state:'hidden'});
  await page.screenshot({path:process.argv[2]+'/tool-disabled-phone.png',fullPage:true});
  if(errors.length)throw Error(errors.join('\n'));
 }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
