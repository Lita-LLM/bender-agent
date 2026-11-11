// Настоящий браузер + HTTPS OAuth/MCP + HTTPS-посредник пульта: без внешних аккаунтов.
const {chromium}=require('playwright');
const fs=require('fs'),path=require('path'),http=require('http'),https=require('https'),crypto=require('crypto');
const OUT=process.argv[2],backend=new URL(process.env.BENDER_URL);
const cert=fs.readFileSync(path.resolve('tests/fixtures/mcp-oauth-cert.pem')),key=fs.readFileSync(path.resolve('tests/fixtures/mcp-oauth-key.pem'));
const metrics={exchanges:0,refreshes:0,toolCalls:0,pkce:false,metadata:false,callbackCookie:false};
const codes=new Map(),tickets=new Map(),servers=[];
let ownerOrigin,serviceOrigin,access,refresh;
function track(server){const sockets=new Set();server.on('connection',socket=>{sockets.add(socket);socket.on('close',()=>sockets.delete(socket));});servers.push({server,sockets});return server;}
async function listen(server,host){await new Promise(resolve=>server.listen(0,host,resolve));return server.address().port;}
async function body(req){let data='';for await(const chunk of req){data+=chunk;if(data.length>1000000)throw Error('Слишком большой запрос фикстуры');}return data;}
function json(res,data,status=200,extra={}){res.writeHead(status,{'Content-Type':'application/json',...extra});res.end(JSON.stringify(data));}
function fetchJson(url){return new Promise((resolve,reject)=>{https.get(url,{ca:cert},res=>{let text='';res.on('data',chunk=>text+=chunk);res.on('end',()=>{try{if(res.statusCode!==200)throw Error('Документ недоступен: '+res.statusCode);resolve(JSON.parse(text));}catch(error){reject(error);}});}).on('error',reject);});}
async function main(){
 let browser;
 try{
  const proxy=track(https.createServer({cert,key},(req,res)=>{
   if(req.url.startsWith('/api/mcp/oauth/callback'))metrics.callbackCookie=!!req.headers.cookie;
   const upstream=http.request({hostname:backend.hostname,port:backend.port,path:req.url,method:req.method,headers:req.headers},response=>{res.writeHead(response.statusCode,response.headers);response.pipe(res);});
   upstream.on('error',error=>{if(!res.headersSent)res.writeHead(502);res.end(error.message);});req.pipe(upstream);res.on('close',()=>upstream.destroy());
  }));
  ownerOrigin='https://127.0.0.1:'+await listen(proxy,'127.0.0.1');
  const service=track(https.createServer({cert,key},async(req,res)=>{
   try{
    const url=new URL(req.url,serviceOrigin),p=url.pathname;
    if(p==='/mcp'&&req.headers.authorization!=='Bearer '+access){json(res,{error:'нужен вход'},401,{'WWW-Authenticate':'Bearer resource_metadata="'+serviceOrigin+'/.well-known/oauth-protected-resource/mcp", scope="read"'});return;}
    if(p==='/.well-known/oauth-protected-resource/mcp'){json(res,{resource:serviceOrigin+'/mcp',authorization_servers:[serviceOrigin+'/issuer'],scopes_supported:['read']});return;}
    if(p==='/.well-known/oauth-authorization-server/issuer'){json(res,{issuer:serviceOrigin+'/issuer',authorization_endpoint:serviceOrigin+'/authorize',token_endpoint:serviceOrigin+'/token',code_challenge_methods_supported:['S256'],authorization_response_iss_parameter_supported:true,client_id_metadata_document_supported:true,token_endpoint_auth_methods_supported:['none']});return;}
    if(p==='/authorize'){
     const params=Object.fromEntries(url.searchParams),doc=await fetchJson(params.client_id);
     if(doc.client_id!==params.client_id||!doc.redirect_uris.includes(params.redirect_uri)||doc.token_endpoint_auth_method!=='none')throw Error('Описание клиента не совпало');
     metrics.metadata=true;const ticket=crypto.randomBytes(16).toString('hex');tickets.set(ticket,params);
     res.writeHead(200,{'Content-Type':'text/html; charset=utf-8','Cache-Control':'no-store'});
     res.end('<!doctype html><html lang="ru"><h1>Проверочный сервис входа</h1><p>Бендер просит право read.</p><form method="post" action="/approve"><input type="hidden" name="ticket" value="'+ticket+'"><button>Разрешить вход Бендеру</button></form></html>');return;
    }
    if(p==='/approve'){
     const ticket=new URLSearchParams(await body(req)).get('ticket'),params=tickets.get(ticket);tickets.delete(ticket);if(!params)throw Error('Нет запроса согласия');
     const code='oauth-ui-code-'+crypto.randomBytes(12).toString('hex');codes.set(code,params);
     const target=new URL(params.redirect_uri);target.searchParams.set('code',code);target.searchParams.set('state',params.state);target.searchParams.set('iss',serviceOrigin+'/issuer');
     res.writeHead(302,{Location:target.href,'Cache-Control':'no-store'});res.end();return;
    }
    if(p==='/token'){
     const data=new URLSearchParams(await body(req));
     if(data.get('resource')!==serviceOrigin+'/mcp')throw Error('Нет правильного resource');
     if(data.get('grant_type')==='authorization_code'){
      const params=codes.get(data.get('code'));codes.delete(data.get('code'));
      const challenge=crypto.createHash('sha256').update(data.get('code_verifier')||'').digest('base64url');
      if(!params||challenge!==params.code_challenge||params.client_id!==data.get('client_id')||params.redirect_uri!==data.get('redirect_uri'))throw Error('PKCE/клиент/возврат не совпал');
      metrics.pkce=true;metrics.exchanges++;
     }else if(data.get('grant_type')==='refresh_token'&&data.get('refresh_token')===refresh){metrics.refreshes++;}else throw Error('Неверный запрос токена');
     const index=metrics.exchanges+metrics.refreshes;access='oauth-ui-access-'+index;refresh='oauth-ui-refresh-'+index;
     json(res,{token_type:'Bearer',access_token:access,refresh_token:refresh,expires_in:3600,scope:'read'});return;
    }
    if(p==='/mcp'){
     const msg=JSON.parse(await body(req));let result;
     if(msg.method==='server/discover')result={supportedVersions:['2026-07-28'],capabilities:{tools:{}}};
     else if(msg.method==='tools/list')result={tools:[{name:'echo',description:'Проверка входа',inputSchema:{type:'object',properties:{text:{type:'string'}}}}]};
     else if(msg.method==='tools/call'){metrics.toolCalls++;result={content:[{type:'text',text:msg.params.arguments.text+' '+access}]};}
     else{json(res,{jsonrpc:'2.0',id:msg.id,error:{code:-32601,message:'Нет метода'}});return;}
     json(res,{jsonrpc:'2.0',id:msg.id,result});return;
    }
    json(res,{error:'Нет адреса'},404);
   }catch(error){json(res,{error:error.message},400);}
  }));
  serviceOrigin='https://localhost:'+await listen(service,'::');
  browser=await chromium.launch();const context=await browser.newContext({viewport:{width:1280,height:900},ignoreHTTPSErrors:true});
  const page=await context.newPage(),errors=[];page.on('pageerror',error=>errors.push(error.message));
  await page.goto(ownerOrigin+'/#/mcp');
  await page.getByRole('checkbox',{name:'Вход OAuth в MCP',exact:true}).click();
  await page.waitForFunction(()=>document.querySelector('[aria-label="Вход OAuth в MCP"]').checked);
  await page.getByRole('button',{name:'Добавить сервер',exact:true}).click();
  await page.fill('[name="mcp-id"]','oauth');await page.selectOption('[name="mcp-transport"]','http');await page.fill('[name="mcp-url"]',serviceOrigin+'/mcp');
  await page.getByRole('button',{name:'Сохранить',exact:true}).click();
  await page.getByRole('checkbox',{name:'Включить oauth',exact:true}).click();await page.waitForFunction(()=>document.querySelector('[aria-label="Включить oauth"]').checked);
  await page.getByRole('button',{name:'Настроить вход OAuth',exact:true}).click();
  await page.getByRole('button',{name:'Проверить выбранный сервер входа',exact:true}).click();
  await page.locator('[name="oauth-mode"]').waitFor();
  if(await page.locator('[name="oauth-mode"]').inputValue()!=='metadata_local')throw Error('Не предложен публичный документ HTTPS');
  await page.screenshot({path:path.join(OUT,'oauth-review.png'),fullPage:true});
  await page.getByRole('button',{name:'Подготовить вход',exact:true}).click();
  const [popup]=await Promise.all([context.waitForEvent('page'),page.getByRole('link',{name:'Открыть вход на сайте сервиса',exact:true}).click()]);
  await popup.getByRole('button',{name:'Разрешить вход Бендеру',exact:true}).click();
  await popup.getByRole('heading',{name:'Вход MCP сохранён',exact:true}).waitFor();await popup.close();
  await page.getByText('Вход сохранён',{exact:false}).first().waitFor();
  if(metrics.exchanges!==1||metrics.toolCalls!==0||!metrics.metadata||!metrics.pkce||metrics.callbackCookie)throw Error('Вход/PKCE/кука или отсутствие повтора не подтверждены: '+JSON.stringify(metrics));
  const state=await page.evaluate(async()=>await(await fetch('/api/mcp')).json());
  if(JSON.stringify(state).includes('oauth-ui-access-1'))throw Error('Токен попал в настройки API');
  await page.getByRole('button',{name:'Обнаружить тулы',exact:true}).click();await page.getByRole('button',{name:'Одобрить просмотренные определения',exact:true}).click();
  await page.goto(ownerOrigin+'/#/polygon');await page.selectOption('select.select','mcp__oauth__echo');await page.fill('textarea.textarea.mono',JSON.stringify({text:'Работает через OAuth'}));
  await page.getByRole('button',{name:'Выполнить',exact:true}).click();
  const guard=await context.newPage();await guard.goto(ownerOrigin+'/#/guard');await guard.waitForSelector('.approval:has-text("MCP oauth: echo")');await guard.getByRole('button',{name:'Разрешить один раз',exact:true}).click();await guard.close();
  await page.waitForSelector('text=Работает через OAuth');await page.waitForSelector('text=секрет скрыт');
  if((await page.locator('body').innerText()).includes('oauth-ui-access-1'))throw Error('Вывод тула раскрыл токен');
  await page.goto(ownerOrigin+'/#/mcp');await page.getByRole('button',{name:'Обновить токен',exact:true}).click();
  await page.waitForSelector('.mcp-tool .badge.amber');
  if(metrics.refreshes!==1||metrics.toolCalls!==1)throw Error('Обновление повторило действие или запрос токена');
  await page.setViewportSize({width:390,height:844});if(await page.locator('.scrim').isVisible())await page.locator('.scrim').click({position:{x:380,y:20}});await page.waitForSelector('.sidebar',{state:'hidden'});
  await page.locator('.mcp-oauth').scrollIntoViewIfNeeded();if(await page.locator('.page').evaluate(element=>element.scrollWidth>element.clientWidth))throw Error('OAuth шире телефона');
  await page.screenshot({path:path.join(OUT,'oauth-phone.png')});
  fs.writeFileSync(path.join(OUT,'oauth-metrics.json'),JSON.stringify(metrics,null,2));if(errors.length)throw Error(errors.join('\n'));
  console.log('MCP OAuth: HTTPS, публичный клиент, PKCE, callback без куки, сейф, тул и обновление без повтора — OK');
 }finally{
  if(browser)await browser.close();
  for(const {server,sockets} of servers){server.close();for(const socket of sockets)socket.destroy();}
 }
}
main().catch(error=>{console.error(error);process.exitCode=1;});
