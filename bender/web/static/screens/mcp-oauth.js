// Вход только хозяина: издатель, права и способ регистрации видны до перехода.
import {html,useEffect,useState} from '../lib/ui.js';
import {post} from '../lib/api.js';
import {Button,ErrorBox,Field} from '../lib/ui.js';

export function McpOAuthPanel({server,enabled,onChanged}){
  const status=server.oauth_status||{};
  const [plan,setPlan]=useState(null),[entry,setEntry]=useState(null),[busy,setBusy]=useState(false),[error,setError]=useState(null);
  const [issuer,setIssuer]=useState(''),[mode,setMode]=useState('pre'),[client,setClient]=useState(''),[secret,setSecret]=useState(''),[method,setMethod]=useState('none'),[scope,setScope]=useState('');
  const api='/api/mcp/'+server.id+'/oauth/';
  const run=async fn=>{setBusy(true);setError(null);try{await fn();}catch(error){setError(error);}finally{setBusy(false);}};
  useEffect(()=>{if(!status.pending&&status.authorized){setEntry(null);setPlan(null);setSecret('');}},[status.revision]);
  const choose=()=>run(async()=>{
    const next=await post(api+'issuer',{flow:plan.id,issuer,return_origin:location.origin});setPlan(next);setScope(next.scope||'');
    setClient(next.registration?.client_id||'');setSecret('');setMethod(next.registration?.method||['none','client_secret_basic','client_secret_post'].find(value=>next.authorization.auth_methods.includes(value))||'none');
    setMode(next.registration?'existing':next.authorization.client_metadata_supported&&location.protocol==='https:'?'metadata_local':next.authorization.registration_endpoint?'register':'pre');
  });
  return html`<section class="mcp-oauth" data-oauth=${server.id}><h4>Вход OAuth</h4>
    <p class="faint">${status.authorized?(status.expired?'Срок токена истёк':'Вход сохранён'):status.configured?'Подключение изменилось — нужен новый вход':'Вход не настроен'}${status.issuer?' · '+status.issuer:''}</p>
    ${status.scope?html`<p class="faint">Права сервиса: ${status.scope}</p>`:null}
    <div class="row"><${Button} small busy=${busy} disabled=${!enabled} onClick=${()=>run(async()=>{setEntry(null);const next=await post(api+'discover');setPlan(next);setIssuer(next.issuers?.[0]||'');})}>Настроить вход OAuth</${Button}>
      ${status.can_refresh?html`<${Button} small busy=${busy} disabled=${!enabled} onClick=${()=>run(async()=>onChanged(await post(api+'refresh',{revision:status.revision})))}>Обновить токен</${Button}>`:null}
      ${status.configured?html`<${Button} small busy=${busy} onClick=${()=>run(async()=>{onChanged(await post(api+'forget',{revision:status.revision}));setPlan(null);setEntry(null);})}>Убрать вход из Бендера</${Button}>`:null}</div>
    <${ErrorBox} error=${error}/>
    ${plan&&enabled?html`<div class="note"><b>Подготовка входа</b>
      ${plan.stage==='metadata'?html`<p>Сервис указал отдельный адрес метаданных: ${(plan.candidates||[]).join(', ')}</p><${Button} small busy=${busy} onClick=${()=>run(async()=>{const next=await post(api+'resource',{flow:plan.id});setPlan(next);setIssuer(next.issuers?.[0]||'');})}>Прочитать эти метаданные</${Button}>`:null}
      ${plan.stage==='issuer'?html`<${Field} label="Сервер входа"><select class="select" value=${issuer} onChange=${event=>setIssuer(event.target.value)}>${plan.issuers.map(value=>html`<option value=${value}>${value}</option>`)}</select></${Field}>
        <p>Ресурс MCP: ${plan.resource}</p><${Button} small busy=${busy} onClick=${choose}>Проверить выбранный сервер входа</${Button}>`:null}
      ${plan.stage==='ready'&&!entry?html`<p>Сервис входа: ${plan.authorization.issuer}</p><p>Ресурс: ${plan.resource}</p><p>Возврат: <code>${plan.redirect_uri}</code></p>
        <details><summary>Адреса сервиса</summary><p>Вход: ${plan.authorization.authorization_endpoint}</p><p>Токены: ${plan.authorization.token_endpoint}</p>${plan.authorization.registration_endpoint?html`<p>Регистрация: ${plan.authorization.registration_endpoint}</p>`:null}</details>
        <${Field} label="Права сервиса (scope)"><input class="input" name="oauth-scope" value=${scope} onInput=${event=>setScope(event.target.value)}/></${Field}>
        <${Field} label="Клиент OAuth"><select class="select" name="oauth-mode" value=${mode} onChange=${event=>setMode(event.target.value)}>
          ${plan.registration?html`<option value="existing">Сохранённый клиент этого сервиса</option>`:null}
          <option value="pre">Клиент, выданный сервисом</option>
          ${plan.authorization.registration_endpoint?html`<option value="register">Зарегистрировать клиента автоматически</option>`:null}
          ${plan.authorization.client_metadata_supported?html`${location.protocol==='https:'?html`<option value="metadata_local">Описание по HTTPS на этом пульте</option>`:null}<option value="metadata_url">Моё описание клиента по HTTPS</option>`:null}
        </select></${Field}>
        ${mode==='pre'||mode==='metadata_url'?html`<${Field} label=${mode==='metadata_url'?'HTTPS-адрес описания (client_id)':'Публичное имя клиента (client_id)'}><input class="input" name="oauth-client" value=${client} onInput=${event=>setClient(event.target.value)}/></${Field}>`:null}
        ${mode==='pre'?html`<${Field} label="Проверка клиента"><select class="select" name="oauth-method" value=${method} onChange=${event=>setMethod(event.target.value)}>${plan.authorization.auth_methods.filter(value=>['none','client_secret_basic','client_secret_post'].includes(value)).map(value=>html`<option value=${value}>${value==='none'?'Публичный клиент с PKCE (none)':value}</option>`)}</select></${Field}>
          ${method!=='none'?html`<${Field} label="Секрет клиента OAuth (client_secret)" hint="Сохраняется в Сейфе с правами 0600."><input type="password" class="input" name="oauth-secret" autocomplete="new-password" value=${secret} onInput=${event=>setSecret(event.target.value)}/></${Field}>`:null}`:null}
        ${mode==='metadata_local'?html`<p>Пульт опубликует общедоступный JSON с именем «Бендер» и адресом возврата. Этот адрес HTTPS должен быть доступен серверу входа.</p>`:null}
        ${mode==='register'?html`<p>Сервис создаст регистрацию клиента с именем «Бендер» и указанным адресом возврата.</p>`:null}
        <${Button} busy=${busy} onClick=${()=>run(async()=>{const next=await post(api+'authorize',{flow:plan.id,review:plan.review,mode,client_id:client,client_secret:secret,auth_method:method,scope});setSecret('');setEntry(next);})}>Подготовить вход</${Button}>`:null}
      ${entry?html`<p><a class="btn primary" href=${entry.url} target="_blank" rel="noopener noreferrer">Открыть вход на сайте сервиса</a></p><p>Вход ожидается до 10 минут. После сохранения токена заново обнаружь и одобри определения MCP. Вызовы сами не повторяются.</p>`:null}
      <${Button} small busy=${busy} onClick=${()=>run(async()=>{onChanged(await post(api+'cancel'));setPlan(null);setEntry(null);setSecret('');})}>Закрыть подготовку</${Button}>
    </div>`:null}
  </section>`;
}
