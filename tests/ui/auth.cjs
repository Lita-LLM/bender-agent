// Тестовый хозяин передаёт настоящую куку пульта; обработчики авторизации не подменяются.
const {chromium}=require('playwright');
const token=process.env.BENDER_UI_TOKEN,base=process.env.BENDER_URL;
if(token&&base){
 const cookies=[{name:'bender_token',value:token,url:base,httpOnly:true,sameSite:'Strict'}];
 const launch=chromium.launch.bind(chromium);
 chromium.launch=async(...args)=>{
  const browser=await launch(...args);
  for(const method of ['newContext','newPage']){
   const original=browser[method].bind(browser);
   browser[method]=options=>original({...options,storageState:{cookies,origins:[]}});
  }
  return browser;
 };
 const connect=chromium.connectOverCDP.bind(chromium);
 chromium.connectOverCDP=async(...args)=>{const browser=await connect(...args);for(const context of browser.contexts())await context.addCookies(cookies);return browser;};
 const fetch=globalThis.fetch;
 globalThis.fetch=(input,options={})=>{
  if(new URL(input.url||input,base).origin===new URL(base).origin){const headers=new Headers(options.headers);headers.set('Cookie','bender_token='+token);options={...options,headers};}
  return fetch(input,options);
 };
}
