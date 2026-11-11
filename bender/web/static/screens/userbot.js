// Аккаунт Telegram выбранного агента (юзербот). Всё здесь делает только хозяин.
import {html,useEffect,useState,Button,ErrorBox,Field,Toggle,Seg} from '../lib/ui.js';
import {get,post} from '../lib/api.js';
import {useEvents} from '../lib/store.js';

const STATE={off:'выключен',ok:'подключён',login:'нужен вход',error:'ошибка'};
const KIND={user:'человек',bot:'бот',group:'группа',supergroup:'группа',channel:'канал'};
const WAKE=[['never','Не будить'],['mention','По упоминанию'],['always','Каждое сообщение']];

export function UserbotPanel(){
 const [s,setS]=useState(null),[error,setError]=useState(null),[busy,setBusy]=useState(''),[form,setForm]=useState({api_id:'',api_hash:'',phone:'',code:'',password:'',risk:false}),[chats,setChats]=useState(null),[q,setQ]=useState('');
 const reload=()=>get('/api/channels/userbot').then(setS,setError);
 useEffect(()=>{reload();},[]);
 useEvents(event=>{if(event.type==='channel'&&event.channel==='userbot'||event.type==='settings'&&event.group==='userbot')reload();},[]);
 const run=async(action,data={},label=action)=>{setBusy(label);setError(null);try{const value=await post('/api/channels/userbot/'+action,data);setS(value);return value;}catch(e){setError(e);}finally{setBusy('');}};
 const field=key=>event=>setForm({...form,[key]:event.target.value});
 const find=async()=>{setBusy('chats');setError(null);try{setChats((await get('/api/channels/userbot/chats?q='+encodeURIComponent(q))).chats);}catch(e){setError(e);}finally{setBusy('');}};
 const setChat=async(chat,changes)=>{const value=await run('chat',{id:chat.id,title:chat.title,...changes},'chat');if(value&&chats)setChats(chats.map(c=>c.id===chat.id?{...c,...(value.settings.chats.find(r=>r.id===chat.id)||{see:false,wake:'never',reply:false})}:c));};
 if(!s)return html`<section class="card userbot-channel"><h3>Аккаунт Telegram агента</h3><${ErrorBox} error=${error}/></section>`;
 const rt=s.runtime,listed=s.settings.chats||[],rows=chats||listed;
 return html`<section class="card userbot-channel"><h3>Аккаунт Telegram агента</h3>
  <p class="faint">Настоящий аккаунт Telegram у агента «${s.agent_id}»: пишет в личках, группах и каналах как человек. Чтобы настроить другого агента, выбери его вверху пульта. По умолчанию аккаунт никого не видит и никому не отвечает.</p>
  <${ErrorBox} error=${error}/>
  <h4>1. Telethon</h4>
  <p class="faint">Аккаунту нужна библиотека Telethon. Она ставится в отдельную папку Бендера (${rt.path}) и не трогает системный Python. Удаление стирает эту папку целиком.</p>
  <div class="row">${rt.installed?html`<span>Установлен Telethon ${rt.version}</span><${Button} small busy=${busy==='remove'} onClick=${()=>confirm('Удалить Telethon? Аккаунты Telegram всех агентов остановятся. Входы останутся в Сейфе.')&&run('remove')}>Удалить Telethon</${Button}>`
   :html`<${Button} busy=${busy==='install'||rt.busy} onClick=${()=>run('install')}>Установить Telethon</${Button}>`}</div>
  ${rt.busy?html`<p class="note">Идёт установка, это может занять несколько минут.</p>`:null}${rt.error?html`<p class="note">${rt.error}</p>`:null}
  <h4>2. Ключи приложения</h4>
  <p class="faint">Telegram требует api_id и api_hash: их выдают на my.telegram.org → API development tools. Хранятся в Сейфе, агент их не видит.</p>
  <div class="row"><input class="input" name="userbot-api-id" placeholder="api_id" value=${form.api_id} onInput=${field('api_id')}/><input class="input" name="userbot-api-hash" type="password" autocomplete="new-password" placeholder="api_hash" value=${form.api_hash} onInput=${field('api_hash')}/>
   <${Button} small busy=${busy==='api'} onClick=${()=>run('api',{api_id:form.api_id,api_hash:form.api_hash}).then(v=>v&&setForm({...form,api_id:'',api_hash:''}))}>${s.has_api?'Заменить ключи':'Сохранить ключи'}</${Button}></div>
  ${s.has_api?html`<p class="faint">Ключи сохранены.</p>`:null}
  <h4>3. Вход</h4>
  <p>Состояние: ${STATE[s.state]||s.state}${s.me?html` · ${s.me.name}${s.me.username?' @'+s.me.username:''}`:''}</p>${s.notice?html`<p class="note">${s.notice}</p>`:null}
  ${s.has_session&&!s.stage?html`<div class="row">${s.settings.enabled?html`<${Button} small busy=${busy==='settings'} onClick=${()=>run('settings',{enabled:false},'settings')}>Выключить аккаунт</${Button}>`:html`<${Button} small busy=${busy==='settings'} disabled=${!rt.installed} onClick=${()=>run('settings',{enabled:true},'settings')}>Включить аккаунт</${Button}>`}
    <${Button} small busy=${busy==='logout'} onClick=${()=>confirm('Выйти из аккаунта? Вход сотрётся из Сейфа, для возврата понадобится код из Telegram.')&&run('logout')}>Выйти из аккаунта</${Button}></div>`
  :s.stage==='code'?html`<${Field} label="Код из Telegram" hint="Telegram прислал его в приложение или по SMS."><input class="input" name="userbot-code" inputmode="numeric" value=${form.code} onInput=${field('code')}/></${Field}>
    <div class="row"><${Button} busy=${busy==='code'} onClick=${()=>run('code',{code:form.code}).then(()=>setForm({...form,code:''}))}>Войти</${Button}><${Button} small onClick=${()=>run('cancel')}>Отмена</${Button}></div>`
  :s.stage==='password'?html`<${Field} label="Пароль двухэтапной проверки" hint="Пароль уходит только в Telegram и нигде не сохраняется."><input class="input" name="userbot-password" type="password" autocomplete="new-password" value=${form.password} onInput=${field('password')}/></${Field}>
    <div class="row"><${Button} busy=${busy==='code'} onClick=${()=>run('code',{password:form.password},'code').then(()=>setForm({...form,password:''}))}>Войти</${Button}><${Button} small onClick=${()=>run('cancel')}>Отмена</${Button}></div>`
  :html`<p class="note">Telegram может ограничить или заблокировать аккаунт за автоматизацию. Лучше завести для агента отдельный номер, а не использовать свой основной.</p>
    <${Toggle} label="Я понимаю риск для аккаунта" checked=${form.risk} onChange=${v=>setForm({...form,risk:v})}/>
    <div class="row"><input class="input" name="userbot-phone" placeholder="+79001234567" value=${form.phone} onInput=${field('phone')}/>
     <${Button} busy=${busy==='login'} disabled=${!rt.installed||!s.has_api||!form.risk} onClick=${()=>run('login',{phone:form.phone,risk:form.risk})}>Получить код</${Button}></div>`}
  ${s.state==='ok'?html`
  <h4>4. Хозяин</h4>
  ${s.owner?html`<div class="row"><span>Хозяин: ${s.owner.name}${s.owner.username?' @'+s.owner.username:''}</span><${Button} small onClick=${()=>run('unpair')}>Забыть хозяина</${Button}></div>`
   :html`<p class="faint">Привяжи свой личный аккаунт: тогда агент будет узнавать тебя и слушаться команд.</p><${Button} small onClick=${()=>run('pair')}>Получить код привязки</${Button}>`}
  ${s.code?html`<p class="note">Напиши со своего аккаунта в личку аккаунту агента: <code>${s.code}</code>. Код живёт 15 минут.</p>`:null}
  <h4>5. Кого видит и кому отвечает</h4>
  <p class="faint">«Видит» — агент получает сообщения чата как недоверенный текст. «Будить» — когда сообщение запускает ход агента. «Отвечает» — итог хода уходит в этот чат.</p>
  <div class="row"><input class="input" name="userbot-chat-query" placeholder="Название чата или @имя" value=${q} onInput=${e=>setQ(e.target.value)}/><${Button} small busy=${busy==='chats'} onClick=${find}>Найти чаты</${Button}>${chats?html`<${Button} small onClick=${()=>setChats(null)}>Только список</${Button}>`:null}</div>
  ${rows.length?rows.map(c=>html`<div class="userbot-chat" style="display:flex;flex-wrap:wrap;gap:8px 14px;align-items:center;padding:8px 0;border-top:1px solid var(--line,#ddd)">
    <div style="flex:1 1 160px;min-width:0"><b>${c.title||c.id}</b><div class="faint">${KIND[c.type]||''} ${c.id}</div></div>
    <${Toggle} label="Видит" checked=${c.see} onChange=${v=>setChat(c,{see:v})}/>
    <${Toggle} label="Отвечает" checked=${c.reply} onChange=${v=>setChat(c,{reply:v})}/>
    ${c.see?html`<div style="flex:1 1 100%;min-width:0;overflow-x:auto"><${Seg} value=${c.wake||'never'} options=${WAKE} onChange=${v=>setChat(c,{wake:v})}/></div>`:null}
    ${listed.some(r=>r.id===c.id)?html`<${Button} small onClick=${()=>setChat(c,{remove:true})}>Убрать</${Button}>`:null}</div>`):html`<p class="faint">Список пуст: аккаунт никого не видит. Найди чат и включи его.</p>`}
  <h4>6. Свобода и служебные сообщения</h4>
  <${Field} label="Что агент делает в аккаунте сам" hint="В чатах из списка — сам. Остальные чаты, смена профиля, вступление и выход — с твоего одобрения. Прямые вызовы API спрашивают всегда.">
   <${Seg} value=${s.settings.trust} options=${[['listed','Только чаты из списка'],['all','Весь аккаунт']]} onChange=${v=>run('settings',{trust:v},'settings')}/></${Field}>
  <${Field} label="Одобрения, вопросы и уведомления этого агента" hint="В общем боте есть кнопки. В чате аккаунта кнопок нет: отвечаешь словами «да», «да всегда», «нет». Без привязанного хозяина всё идёт в общий бот.">
   <${Seg} value=${s.settings.service} options=${[['bot','В общий бот'],['userbot','В чат аккаунта']]} onChange=${v=>run('settings',{service:v},'settings')}/></${Field}>`:null}
 </section>`;
}
