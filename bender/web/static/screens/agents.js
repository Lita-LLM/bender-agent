// Стая хозяина: переключение, архив, перенос одним ZIP и список чатов.
import {html,useEffect,useState,Button,Field,ErrorBox,toast} from '../lib/ui.js';
import {get,post,patch,api} from '../lib/api.js';
const PROFILES={full:'Полный',helper:'Помощник',observer:'Наблюдатель',reader:'Чтец',custom:'Свой'};
export function switchAgent(id){sessionStorage.setItem('bender.agent',id);location.reload();}
export function AgentsScreen(){
 const [data,setData]=useState(null),[error,setError]=useState(null),[name,setName]=useState(''),[profile,setProfile]=useState('helper'),[awaken,setAwaken]=useState(true),[templates,setTemplates]=useState({}),[role,setRole]=useState('');
 const load=()=>get('/api/agents').then(setData,setError);useEffect(()=>{load();get('/api/agent-templates').then(r=>setTemplates(r.roles),setError);},[]);
 const run=async fn=>{try{await fn();load();}catch(e){setError(e);}};
 const upload=async e=>{const file=e.target.files?.[0];if(!file)return;await run(async()=>{const response=await fetch('/api/agents/import',{method:'POST',headers:{'X-Bender':'1'},body:await file.arrayBuffer()});const result=await response.json();if(!response.ok)throw Error(result.error?.title||'Не удалось импортировать');toast('Агент импортирован; фон и голос требуют просмотра.');});};
 return html`<div class="page"><div class="page-inner"><h2>Стая</h2><p class="note">У каждого своя память и чаты. Провайдеры и сейф общие. Экспорт не включает ключи; импорт создаёт нового агента с ограниченными правами.</p>
 <p class="note">Все агенты в начале разговора читают твою карточку «О хозяине». Пишешь её только ты: <a href="#/settings">Настройки → О хозяине</a>.</p>
 <p><a class="btn" href="#/correspondence">Общение и переписка</a></p><${ErrorBox} error=${error}/><div class="card"><h3>Новый агент</h3><${Field} label="Имя нового агента"><input class="input" value=${name} onInput=${e=>setName(e.target.value)}/></${Field}>
 <${Field} label="Шаблон прав"><select class="select" value=${profile} onChange=${e=>setProfile(e.target.value)}><option value="helper">Помощник</option><option value="observer">Наблюдатель</option><option value="reader">Чтец</option><option value="full">Полный</option></select></${Field}>
 <${Field} label="Шаблон из роли команды"><select class="select" value=${role} onChange=${e=>setRole(e.target.value)}><option value="">Без шаблона команды</option>${Object.entries(templates).map(([k,r])=>html`<option value=${k}>${r.title}</option>`)}</select></${Field}>
 <label><input type="checkbox" checked=${awaken} onChange=${e=>setAwaken(e.target.checked)}/> С Пробуждением</label><div class="row"><${Button} kind="primary" disabled=${!name.trim()} onClick=${()=>run(async()=>{await post('/api/agents',{name,profile,awaken,role});setName('');})}>Создать агента</${Button}></div>
 <${Field} label="Импорт архива агента"><input type="file" accept=".zip" onChange=${upload}/></${Field}></div>
 <p>Всего сегодня: $${(data?.agents||[]).reduce((sum,a)=>sum+a.cost_today,0).toFixed(4)}</p>
 ${(data?.agents||[]).map(a=>html`<div class="card agent-row"><h3>${a.name} <span class="badge">${a.id}</span> ${a.archived?'· в архиве':''}</h3><p class="faint">${PROFILES[a.profile]||'Свой'} · сегодня $${a.cost_today.toFixed(4)}</p><p class="faint">${a.description?'Для стаи: '+a.description+(a.described_by==='agent'?' — написал сам агент':' — задал хозяин'):'Описания пока нет: задай его или агент напишет сам.'}</p><div class="row">
 <${Button} disabled=${a.archived} onClick=${()=>switchAgent(a.id)}>Перейти</${Button}><a class="btn small" href=${'#/rights?agent='+a.id}>Права</a><${Button} small onClick=${()=>{const name=prompt('Новое имя',a.name);if(name)run(()=>patch('/api/agents/'+a.id,{name}));}}>Переименовать</${Button}><${Button} small onClick=${()=>{const text=prompt('Короткое описание для стаи, до 200 знаков. Пустое поле очистит описание.',a.description||'');if(text!==null)run(()=>patch('/api/agents/'+a.id,{description:text}));}}>Описание</${Button}>
 ${a.id!=='main'?html`<${Button} small onClick=${()=>run(async()=>{await patch('/api/agents/'+a.id,{archived:!a.archived});if(a.archived===0&&sessionStorage.getItem('bender.agent')===a.id)switchAgent('main');})}>${a.archived?'Разбудить':'В архив'}</${Button}>`:null}
 <a class="btn small" href=${'/api/agents/'+a.id+'/export'}>Экспорт</a>${a.id!=='main'?html`<${Button} small kind="danger" onClick=${()=>{if(confirm('Удалить агента? Перед удалением сохранится ZIP для восстановления.'))run(async()=>{const r=await api('DELETE','/api/agents/'+a.id,{confirmed:true});toast('Архив: '+r.backup);if(sessionStorage.getItem('bender.agent')===a.id)switchAgent('main');});}}>Удалить</${Button}>`:null}</div></div>`)}
 <p class="faint">Терминал: bender chat --agent id; Полигон: bender try agents.</p></div></div>`;
}
