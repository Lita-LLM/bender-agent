// Экзамены: каталог → настоящий прогон → история → телефон.
const {chromium}=require('playwright');
async function main(){
 const browser=await chromium.launch();
 try{
  const page=await browser.newPage({viewport:{width:1280,height:900}}),errors=[];
  page.on('pageerror',e=>{errors.push(e.message);console.error(e.message);});
  await page.goto(process.env.BENDER_URL+'/#/workshop');
  await page.getByText('Запись и чтение памяти').waitFor();
  await page.getByRole('button',{name:'Запустить экзамены',exact:true}).click();
  await page.getByText(/Последний прогон: пройден/).waitFor({timeout:60000});
  await page.locator('.exam-report summary').first().click();
  await page.screenshot({path:process.argv[2]+'/exams.png',fullPage:true});
  await page.reload();
  await page.getByText(/Последний прогон: пройден/).waitFor();
  await page.setViewportSize({width:390,height:844});
  await page.locator('.sidebar').waitFor({state:'hidden'});
  await page.screenshot({path:process.argv[2]+'/exams-phone.png',fullPage:true});
  if(errors.length)throw Error(errors.join('\n'));
 }finally{await browser.close();}
}
main().catch(e=>{console.error(e);process.exitCode=1;});
