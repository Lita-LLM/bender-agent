// Версия навыка: редактор → карантин → дифф → откат → закрепление/архив → убрать в чате.
const {chromium}=require('playwright');
async function main(){
 const browser=await chromium.launch();
 try{
  const page=await browser.newPage({viewport:{width:1280,height:1000}}),errors=[];
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(process.env.BENDER_URL+'/#/skills');
  const card=page.locator('[data-skill="report"]');
  await card.getByRole('button',{name:'Просмотреть',exact:true}).click();
  const editor=card.getByLabel('Редактор навыка',{exact:true}),text=await editor.inputValue();
  await editor.fill(text+'\n4. Покажи результат хозяину.\n');
  await card.getByRole('button',{name:'Сохранить новую версию',exact:true}).click();
  await card.getByText('Изменён — карантин',{exact:true}).waitFor();
  await card.getByText('Версии и откат (1)',{exact:true}).click();
  await card.getByText('+4. Покажи результат хозяину.',{exact:false}).waitFor();
  await page.screenshot({path:process.argv[2]+'/skill-diff.png',fullPage:true});
  await card.getByRole('button',{name:'Вернуть эту версию',exact:true}).click();
  await card.getByText('Включён',{exact:true}).waitFor();
  await card.getByRole('button',{name:'Закрепить',exact:true}).click();
  await card.getByRole('button',{name:'Открепить',exact:true}).waitFor();
  await card.getByRole('button',{name:'В архив',exact:true}).click();
  await card.getByRole('button',{name:'Вернуть из архива',exact:true}).waitFor();
  await card.getByRole('button',{name:'Вернуть из архива',exact:true}).click();
  await page.goto(process.env.BENDER_URL+'/#/');
  await page.getByRole('button',{name:'Убрать навык',exact:true}).click();
  await page.getByRole('button',{name:'Убран',exact:true}).waitFor();
  await page.setViewportSize({width:390,height:844});
  await page.locator('.sidebar').waitFor({state:'hidden'});
  await page.goto(process.env.BENDER_URL+'/#/skills');
  await page.getByRole('button',{name:'Вернуть из архива',exact:true}).waitFor();
  await page.screenshot({path:process.argv[2]+'/skill-archive-phone.png',fullPage:true});
  if(errors.length)throw Error(errors.join('\n'));
 }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
