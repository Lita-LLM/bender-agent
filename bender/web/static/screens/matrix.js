// Данные/управление хозяина; пакет Matrix исполняется отдельным процессом.
import {html,useEffect,useState,Button,ErrorBox,Field} from '../lib/ui.js';
import {get,post} from '../lib/api.js';
import {useEvents} from '../lib/store.js';
import {PendingCards} from './chat.js';

export function MatrixPanel(){
 const [state,setState]=useState(null),[form,setForm]=useState(null),[agents,setAgents]=useState([]),[token,setToken]=useState(''),[error,setError]=useState(null),[busy,setBusy]=useState(false),[note,setNote]=useState('');
 const reload=()=>get('/api/channels/matrix').then(value=>{setState(value);setForm(old=>old||value.settings);},setError);
 useEffect(()=>{reload();get('/api/agents').then(value=>setAgents(value.agents||[]),setError);},[]);
 useEvents(event=>{if(event.type==='channel'&&event.channel==='matrix'||event.type==='settings'&&['matrix','module_trust','mcp_trust','modules'].includes(event.group))reload();},[]);
 const run=async(action,data={})=>{setBusy(true);setError(null);try{const value=await post('/api/channels/matrix/'+action,data);setState(value.channel||value);if(action==='configure')setToken('');if(action==='check')setNote('Сервер и комната доступны.');}catch(e){setError(e);}finally{setBusy(false);}};
 const change=key=>event=>setForm({...form,[key]:event.target.value});
 return html`<section class="card matrix-channel"><h3>Matrix</h3><p class="faint">Отдельный модуль, одна комната с ботом и хозяином. E2EE не поддерживается: создай отдельную комнату без шифрования. Токен хранится в Сейфе.</p>
  <${PendingCards}/><${ErrorBox} error=${error}/>
  <div class="row"><${Button} small busy=${busy} onClick=${async()=>{setBusy(true);setError(null);try{await post('/api/modules/bundled/matrix');setNote('Пакет в карантине. В Модулях просмотри/одобри пакет, обнаружь и одобри определения инструментов.');await reload();}catch(e){setError(e);}finally{setBusy(false);}}}>Установить Matrix в карантин</${Button}><a href="#/modules">Просмотреть модуль и определения</a></div>
  ${note?html`<p class="note">${note}</p>`:null}
  ${(state?.import_proposals||[]).map(item=>html`<div class="note"><p>Из переноса OpenClaw (${item.account}): ${item.homeserver}${item.encrypted?' · источник использовал E2EE, эта комната не подойдёт':''}</p>
    <p class="faint">Токен — отдельно в Сейфе, если перенос ключей разрешён. Одобрения, привязка хозяина и ключи E2EE не переносятся.</p>
    <${Button} small onClick=${()=>setForm({...form,homeserver:item.homeserver,room_id:item.rooms.length===1?item.rooms[0]:form.room_id})}>Подставить адрес из переноса</${Button}>
  </div>`)}
  ${form?html`<${Field} label="Домашний сервер Matrix"><input class="input" name="matrix-homeserver" placeholder="https://matrix.example.org" value=${form.homeserver} onInput=${change('homeserver')}/></${Field}>
    <${Field} label="ID комнаты Matrix"><input class="input" name="matrix-room" placeholder="!комната:example.org" value=${form.room_id} onInput=${change('room_id')}/></${Field}>
    <${Field} label="Агент Matrix"><select class="select" name="matrix-agent" value=${form.agent_id} onChange=${change('agent_id')}>${agents.filter(item=>!item.archived).map(item=>html`<option value=${item.id}>${item.name||item.id}</option>`)}</select></${Field}>
    <${Field} label="Токен доступа бота Matrix (access_token)" hint=${state?.has_token?'Токен сохранён; оставь пустым, чтобы сохранить прежний.':'Создай отдельную учётную запись бота и выдай ей access_token.'}><input class="input" name="matrix-token" type="password" autocomplete="new-password" value=${token} onInput=${event=>setToken(event.target.value)}/></${Field}>
    <div class="row"><${Button} busy=${busy} onClick=${()=>run('configure',{...form,token})}>Сохранить Matrix выключенным</${Button}>
      <${Button} small busy=${busy} disabled=${!state?.ready} onClick=${()=>run('check')}>Проверить Matrix</${Button}>
      ${state?.settings.enabled?html`<${Button} small busy=${busy} onClick=${()=>run('stop')}>Выключить Matrix</${Button}>`:html`<${Button} small busy=${busy} disabled=${!state?.ready} onClick=${()=>run('connect')}>Подключить комнату Matrix</${Button}>`}</div>
  `:null}
  ${state?html`<p>Состояние: ${state.state}${state.owner?' · хозяин '+state.owner.user_id:' · хозяин не привязан'}</p>${state.notice?html`<p class="note">${state.notice}</p>`:null}${!state.ready?html`<p class="faint">${state.reason}</p>`:null}
    ${state.settings.enabled&&!state.owner?html`<${Button} small busy=${busy} disabled=${!state.pair_ready||!['ok','unpaired'].includes(state.state)} onClick=${()=>run('pair')}>Получить код Matrix</${Button}>`:null}
    ${state.code?html`<p class="note">Напиши боту в выбранной комнате: <code>/pair ${state.code}</code>. Код живёт 10 минут. /sync — не чаще минуты; дождись привязки.</p>`:null}
    ${state.owner?html`<${Button} small busy=${busy} onClick=${()=>run('unpair')}>Забыть хозяина Matrix</${Button}>`:null}
  `:null}
  <p class="faint">После входа бот должен состоять только с тобой в этой комнате. Настройки предела файлов/интервала — Настройки → Matrix. Проверка без сети: bender try matrix.</p>
 </section>`;
}
