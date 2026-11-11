// Настоящий Сторож: просмотр → новый слот → здоровье → прежний слот.
const {chromium}=require('playwright');
const {execFileSync}=require('child_process');
const path=require('path');
async function main(){
 const out=process.argv[2],source=path.resolve(out,'source-version');
 execFileSync('python3',['-c','from bender.updates.manager import snapshot; from bender.core.paths import PROGRAM_DIR; import sys,pathlib,shutil; p=pathlib.Path(sys.argv[1]); shutil.rmtree(p,ignore_errors=True); snapshot(PROGRAM_DIR,p)',source]);
 const browser=await chromium.launch();
 try{
  const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  async function waitState(predicate){for(let i=0;i<150;i++){try{const r=await page.request.get(process.env.BENDER_URL+'/api/versions',{timeout:1000});if(r.ok()&&predicate(await r.json()))return;}catch{}await page.waitForTimeout(100);}throw Error('Состояние версии не достигнуто');}
  await page.goto(process.env.BENDER_URL+'/#/versions');
  await page.getByLabel('Источник версии',{exact:true}).fill(source);
  await page.getByLabel('Собственная разработка',{exact:true}).check();
  await page.getByRole('button',{name:'Подготовить и просмотреть',exact:true}).click();
  await page.waitForSelector('.approval:has-text("прочитать пакет версии")');
  await page.getByRole('button',{name:'Разрешить один раз',exact:true}).click();
  await page.waitForSelector('.version-card:has-text("БЕЗ ПОДПИСИ")');
  await page.getByRole('button',{name:'Применить просмотренную версию',exact:true}).click();
  await page.waitForSelector('.approval:has-text("применить просмотренную версию")');
  await page.getByRole('button',{name:'Разрешить один раз',exact:true}).click();
  await waitState(d=>!!d.health&&!d.pending);
  await page.getByRole('button',{name:'Обновить состояние',exact:true}).click();
  await page.waitForSelector('text=Самопроверка пройдена');
  await page.screenshot({path:path.join(out,'version-healthy.png'),fullPage:true});
  await page.getByRole('button',{name:'Откатить на прежнюю',exact:true}).click();
  await page.waitForSelector('.approval:has-text("вернуться на прежний слот")');
  await page.getByRole('button',{name:'Разрешить один раз',exact:true}).click();
  await waitState(d=>!!d.last_rollback);
  await page.getByRole('button',{name:'Обновить состояние',exact:true}).click();
  await page.waitForSelector('text=Сработал откат');
  await page.setViewportSize({width:390,height:844});
  await page.locator('.sidebar').waitFor({state:'hidden'});
  if(await page.locator('.page').evaluate(e=>e.scrollWidth>e.clientWidth+1))throw Error('Версии шире телефона');
  if(errors.length)throw Error(errors.join('\n'));
  console.log('Версии: просмотр, реальный слот/самопроверка, откат — OK');
 }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exit(1);});
