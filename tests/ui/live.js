// Мысли в живом ходе и сохранённом сообщении свёрнуты, раскрываются хозяином.
const {chromium}=require('playwright');
const path=require('path');
async function main(){
 const browser=await chromium.launch();
 try{
  const page=await browser.newPage({viewport:{width:390,height:844}});const errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.BENDER_URL+'/#/');
  await fetch(process.env.FAKE_URL+'/fake/script',{method:'POST',body:JSON.stringify([{reasoning:'Проверяю условия перед ответом.',text:'Готово: условия проверены, можно продолжать работу.',delay:.15}])});
  await page.locator('.composer textarea').fill('Проверь условия');
  await page.locator('.composer .btn.primary').click();
  const thinking=page.locator('details.reasoning').first();await thinking.waitFor();
  if(await thinking.getAttribute('open')!==null)throw Error('Мысли открылись сами');
  await page.getByText('Готово: условия проверены, можно продолжать работу.',{exact:true}).waitFor();
  const saved=page.locator('details.reasoning').first();
  if(await saved.getAttribute('open')!==null)throw Error('Сохранённые мысли открыты');
  await saved.locator('summary').click();
  await saved.getByText('Проверяю условия перед ответом.',{exact:true}).waitFor();
  await page.screenshot({path:path.join(process.argv[2],'thoughts-phone.png'),fullPage:true});
  await page.goto(process.env.BENDER_URL+'/#/settings');
  await page.getByText('Живой ответ',{exact:true}).waitFor();
  await page.getByText('✓ агент меняет сам',{exact:false}).first().waitFor();
  await page.getByText('🔒 только хозяин',{exact:false}).first().waitFor();
  await page.getByText('? агент спрашивает',{exact:false}).first().waitFor();
  if(errors.length)throw Error(errors.join('\n'));
  console.log('Мысли свёрнуты в потоке/истории, раскрываются; настройки live и прав — OK');
 }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exit(1);});
