// Потолок хозяина, отдельно от обычных предпочтений и разовых одобрений.
import {html,useEffect,useState,Button,Field,ErrorBox,toast} from '../lib/ui.js';
import {get,patch} from '../lib/api.js';
import {useApp} from '../lib/store.js';
const GROUPS={files:'Файлы',shell:'Команды',web:'Сеть',memory:'Память',heart:'Сердце',self:'О себе',team:'Команда',skills:'Навыки',mail:'Почта',modules:'Модули',workshop:'Мастерская',agents:'Общение агентов',channel:'Текущий канал'};
export function RightsScreen(){
 const {st,route}=useApp(),id=route.query.get('agent')||st?.agent_id||'main';
 const [data,setData]=useState(null),[draft,setDraft]=useState(null),[error,setError]=useState(null);
 const load=()=>get('/api/agents/'+id+'/rights').then(r=>{setData(r);setDraft({...r.effective});},setError);
 useEffect(()=>{load();},[id]);
 const change=(k,v)=>setDraft(d=>({...d,[k]:v}));
 const toggle=(key,value)=>change(key,(draft[key]||[]).includes(value)?draft[key].filter(x=>x!==value):[...(draft[key]||[]),value]);
 const choose=(key,label,options)=>html`<${Field} label=${label}><select class="select" value=${draft[key]??''} onChange=${e=>change(key,e.target.value)}>${Object.entries(options).map(([v,t])=>html`<option value=${v}>${t}</option>`)}</select></${Field}>`;
 const save=async()=>{try{const r=await patch('/api/agents/'+id+'/rights',draft);setData(r);setDraft({...r.effective});toast('Права сохранены хозяином');}catch(e){setError(e);}};
 return html`<div class="page"><div class="page-inner"><h2>Права агента</h2><p class="note">Это потолок: разовое разрешение и собственные настройки агента его не расширяют. Шаблон выбирает начальные права, отдельные ограничения сохраняются.</p><${ErrorBox} error=${error}/>
 ${draft&&data?html`<div class="card">
 <${Field} label="Шаблон прав"><select class="select" value=${draft.profile} onChange=${e=>setDraft(d=>({...d,...data.templates[e.target.value],profile:e.target.value}))}>${Object.entries(data.profiles).map(([v,t])=>html`<option value=${v}>${t}</option>`)}</select></${Field}>${choose('guard_profile','Профиль Предохранителя',{inherit:'Из настроек агента',careful:'Осторожный',normal:'Обычный',trust:'Доверие',off:'Выключен'})}
 <h3>Способности</h3><div class="form-grid">${Object.entries(data.capabilities).map(([k,v])=>html`<label><input type="checkbox" checked=${draft.capabilities.includes(k)} onChange=${()=>toggle('capabilities',k)}/> ${v}</label>`)}</div>
 <h3>Группы тулов</h3><label><input type="checkbox" checked=${draft.groups.includes('*')} onChange=${()=>toggle('groups','*')}/> Все группы</label><div class="form-grid">${data.groups.map(g=>html`<label><input type="checkbox" checked=${draft.groups.includes(g)} onChange=${()=>toggle('groups',g)}/> ${GROUPS[g]||g}</label>`)}</div>
 <h3>Отдельные тулы</h3><label><input type="checkbox" checked=${draft.tools===null} onChange=${e=>change('tools',e.target.checked?null:[])}/> Все в пределах групп и способностей</label>
 ${draft.tools!==null?html`<div class="form-grid">${data.tools.map(t=>html`<label><input type="checkbox" checked=${draft.tools.includes(t.id)} onChange=${()=>toggle('tools',t.id)}/> ${t.title}</label>`)}</div>`:null}
 <${Field} label="Дополнительные рабочие папки"><textarea class="input" value=${(draft.work_folders||[]).join('\n')} onInput=${e=>change('work_folders',e.target.value.split('\n').filter(x=>x.trim()))}/><p class="faint">Свои файлы доступны всегда. Каждая дополнительная папка — с новой строки. У Полного пустое поле сохраняет прежний доступ с обычным Предохранителем, пока не задано ограничение.</p></${Field}>
 <label><input type="checkbox" checked=${draft.network} onChange=${e=>change('network',e.target.checked)}/> Разрешить сеть</label>
 <label><input type="checkbox" checked=${draft.ssh} onChange=${e=>change('ssh',e.target.checked)}/> SSH: ключи только на чтение, known_hosts на запись, сокет ssh-agent</label>
 <label><input type="checkbox" checked=${draft.require_isolation} onChange=${e=>change('require_isolation',e.target.checked)}/> Требовать изоляцию команд</label><p class="note">Без bubblewrap команды видят файлы других агентов и домашние ключи. Выключение требования разрешает такой запуск.</p>
 <${Field} label="Разрешённые узлы сети"><textarea class="input" value=${draft.network_hosts.join('\n')} onInput=${e=>change('network_hosts',e.target.value.split('\n').map(x=>x.trim()).filter(Boolean))}/><p class="faint">Пусто — любые разрешённые сетевые действия. Можно *.example.org.</p></${Field}>
 <${Field} label="Потолок в сутки, $"><input class="input" type="number" min="0" step="0.01" value=${draft.daily_usd} onInput=${e=>change('daily_usd',Number(e.target.value))}/><p class="faint">0 — без отдельного потолка. Цена текущего запроса может превысить остаток.</p></${Field}>
 <label><input type="checkbox" checked=${draft.team_enabled} onChange=${e=>change('team_enabled',e.target.checked)}/> Может звать команду</label>
 <${Field} label="Задач команды одновременно"><input class="input" type="number" min="0" max="64" value=${draft.team_max} onInput=${e=>change('team_max',Number(e.target.value))}/></${Field}>
 ${choose('settings_free','Бытовые настройки',{allow:'Сам',ask:'Спросить хозяина',deny:'Запретить'})}
 ${choose('settings_independence','Безопасность и самостоятельность',{ask:'Спросить хозяина',deny:'Запретить'})}
 ${choose('settings_owner','Настройки только хозяина',{ask:'Предложить хозяину',deny:'Запретить'})}
 <section class="note api-rights"><h3>API-канал этого агента</h3><p>Отдельный потолок поверх прав агента. По умолчанию Помощник: безопасные группы и свой Предохранитель. Полный тоже не расширяет права самого агента. Фоновая работа обычного агента требует отдельного подтверждения хозяина.</p>
 <label><input type="checkbox" aria-label="Агент доступен по API" checked=${!!data.api_rights?.enabled} onChange=${async e=>{try{setData(await patch('/api/agents/'+id+'/api-rights',{enabled:e.target.checked}));}catch(e){setError(e);}}}/> Агент доступен по API</label>
 <${Field} label="Профиль API"><select class="select" name="api-profile" value=${data.api_rights?.profile||'helper'} onChange=${async e=>{try{setData(await patch('/api/agents/'+id+'/api-rights',{profile:e.target.value}));toast('Права API сохранены');}catch(e){setError(e);}}}>${Object.entries({helper:'Помощник',observer:'Наблюдатель',reader:'Чтец',full:'Полный'}).map(([key,title])=>html`<option value=${key}>${title}</option>`)}</select></${Field}></section>
 <div class="row"><${Button} kind="primary" onClick=${save}>Сохранить права</${Button}><a class="btn" href="#/agents">В стаю</a></div></div>`:null}</div></div>`;
}
