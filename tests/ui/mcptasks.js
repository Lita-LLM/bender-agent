// Настоящие HTTP Tasks и поток подписки, действия через пульт и Полигон.
const {chromium}=require('playwright');
const http=require('http'),fs=require('fs'),path=require('path');
const URL=process.env.BENDER_URL,OUT=process.argv[2];
const task={taskId:'opaque-background-task',status:'working',createdAt:'2026-10-26T00:00:00Z',lastUpdatedAt:'2026-10-26T00:00:00Z',ttlMs:3600000};
const metrics={calls:0,gets:0,cancels:0,subscriptions:0,reads:0};let stream=null,subscriptionId=null,phase='working';
function event(method,params={}){if(!stream)throw Error('Нет потока');stream.write('data: '+JSON.stringify({jsonrpc:'2.0',method,params:{_meta:{'io.modelcontextprotocol/subscriptionId':subscriptionId},...params}})+'\n\n');}
const sockets=new Set();
const server=http.createServer(async(req,res)=>{
  let raw='';for await(const part of req)raw+=part;
  const msg=JSON.parse(raw),method=msg.method;let result={};
  if(method==='server/discover')result={supportedVersions:['2026-07-28'],capabilities:{tools:{},resources:{},prompts:{},extensions:{'io.modelcontextprotocol/tasks':{}}}};
  else if(method==='tools/list')result={tools:[{name:'work',description:'Фоновая работа',inputSchema:{type:'object',properties:{},additionalProperties:false}}]};
  else if(method==='tools/call'){
    if(!msg.params._meta['io.modelcontextprotocol/clientCapabilities'].extensions?.['io.modelcontextprotocol/tasks'])throw Error('Не объявлены Tasks');
    metrics.calls++;result={resultType:'task',...task};
  }else if(method==='tasks/get'){
    metrics.gets++;if(req.headers['mcp-name']!==task.taskId)throw Error('Нет заголовка маршрутизации Tasks');
    result={resultType:'complete',...task,status:phase};if(phase==='completed')result.result={content:[{type:'text',text:'РЕЗУЛЬТАТ ФОНОВОЙ РАБОТЫ'}]};
  }else if(method==='tasks/cancel'){metrics.cancels++;result={resultType:'complete'};}
  else if(method==='resources/list')result={resources:[{name:'Изменяемая заметка',uri:'notes:///background'}]};
  else if(method==='resources/templates/list')result={resourceTemplates:[]};
  else if(method==='prompts/list')result={prompts:[]};
  else if(method==='resources/read'){metrics.reads++;result={contents:[]};}
  else if(method==='subscriptions/listen'){
    metrics.subscriptions++;stream=res;subscriptionId=msg.id;
    res.writeHead(200,{'Content-Type':'text/event-stream','Cache-Control':'no-cache'});
    event('notifications/subscriptions/acknowledged',{notifications:msg.params.notifications});
    res.on('close',()=>{if(stream===res)stream=null;});return;
  }
  res.writeHead(200,{'Content-Type':'application/json'});res.end(JSON.stringify({jsonrpc:'2.0',id:msg.id,result}));
});
server.on('connection',socket=>{sockets.add(socket);socket.on('close',()=>sockets.delete(socket));});
async function main(){
 await new Promise(resolve=>server.listen(0,'127.0.0.1',resolve));const mcp=`http://127.0.0.1:${server.address().port}/mcp`;
 const browser=await chromium.launch(),errors=[];
 try{
  const context=await browser.newContext({viewport:{width:1280,height:900}}),page=await context.newPage();
  page.on('pageerror',e=>errors.push(e.message));
  await page.goto(URL+'/#/mcp');
  await page.getByRole('checkbox',{name:'Фоновые задачи MCP 2026',exact:true}).click();
  await page.getByRole('heading',{name:'Фоновые задачи MCP',exact:true}).waitFor();
  await page.getByRole('checkbox',{name:'Подписки MCP 2026',exact:true}).click();await page.waitForFunction(()=>document.querySelector('[aria-label="Подписки MCP 2026"]').checked);
  await page.getByRole('button',{name:'Добавить сервер',exact:true}).click();
  await page.fill('[name="mcp-id"]','background');await page.selectOption('[name="mcp-transport"]','http');await page.fill('[name="mcp-url"]',mcp);
  await page.locator('[name="mcp-task-support"]').check();
  await page.getByRole('button',{name:'Сохранить',exact:true}).click();
  const card=page.locator('[data-server="background"]');await card.waitFor();await card.getByRole('checkbox',{name:'Включить background'}).click();await page.waitForFunction(()=>document.querySelector('[aria-label="Включить background"]').checked);
  await card.getByRole('button',{name:'Обнаружить тулы',exact:true}).click();
  await card.getByRole('button',{name:'Одобрить просмотренные определения',exact:true}).click();
  await page.goto(URL+'/#/polygon');await page.selectOption('select.select','mcp__background__work');
  await page.fill('textarea.textarea.mono','{}');await page.getByRole('button',{name:'Выполнить',exact:true}).click();
  const guard=await context.newPage();await guard.goto(URL+'/#/guard');
  await guard.locator('.approval').waitFor();await guard.getByRole('button',{name:'Разрешить один раз',exact:true}).click();await guard.close();
  await page.waitForSelector('text=opaque-background-task');
  await page.goto(URL+'/#/mcp');const record=page.locator('.mcp-task');await record.waitFor();await record.locator('summary').click();
  const approve=async()=>{const g=await context.newPage();await g.goto(URL+'/#/guard');await g.locator('.approval').waitFor();await g.getByRole('button',{name:'Разрешить один раз',exact:true}).click();await g.close();};
  phase='completed';await record.getByRole('button',{name:'Проверить статус',exact:true}).click();await approve();
  await page.waitForSelector('text=РЕЗУЛЬТАТ ФОНОВОЙ РАБОТЫ');
  if(metrics.calls!==1||metrics.gets!==1)throw Error('Проверка статуса повторила действие');
  await card.locator('.mcp-subscriptions summary').click();
  await card.getByRole('checkbox',{name:'Состояния своих фоновых задач',exact:true}).check();
  await card.getByRole('button',{name:'Открыть подписку',exact:true}).click();await approve();await card.getByText('Подписка открыта',{exact:true}).waitFor();
  await page.screenshot({path:path.join(OUT,'tasks-stream.png'),fullPage:true});
  event('notifications/tasks',{...task,status:'completed',result:{content:[{type:'text',text:'РЕЗУЛЬТАТ ИЗ ПОДПИСКИ'}]}});
  await page.waitForSelector('text=РЕЗУЛЬТАТ ИЗ ПОДПИСКИ');
  event('notifications/tools/list_changed');await card.getByText('нужно одобрение',{exact:true}).waitFor();
  if(metrics.calls!==1||metrics.gets!==1||metrics.reads||metrics.subscriptions!==1)throw Error('Событие вызвало лишнее действие');
  await page.setViewportSize({width:390,height:844});const scrim=page.locator('.scrim');if(await scrim.isVisible())await scrim.click({position:{x:380,y:20}});
  await page.locator('.sidebar').waitFor({state:'hidden'});await page.locator('.page').evaluate(node=>node.scrollTop=0);await page.screenshot({path:path.join(OUT,'tasks-phone.png'),fullPage:true});
  const size=await page.evaluate(()=>{const p=document.querySelector('.page');return [p.scrollWidth,p.clientWidth];});if(size[0]>size[1]+1)throw Error('Страница шире телефона '+size);
  await card.getByRole('button',{name:'Закрыть подписку',exact:true}).click();await card.getByRole('button',{name:'Открыть подписку',exact:true}).waitFor();
  if(errors.length)throw Error(errors.join('\n'));fs.writeFileSync(path.join(OUT,'background-metrics.json'),JSON.stringify(metrics,null,2));console.log('Tasks/подписки MCP: Chromium зелёный',metrics);
 }finally{await browser.close();for(const socket of sockets)socket.destroy();await new Promise(resolve=>server.close(resolve));}
}
main().catch(error=>{console.error(error);for(const socket of sockets)socket.destroy();server.close();process.exitCode=1;});
