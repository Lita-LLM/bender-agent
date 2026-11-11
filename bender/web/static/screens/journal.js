// Серверные фильтры, экспорт и переход к настоящим запросам модели.
import {html,useEffect,useState,Button,ErrorBox,Field,Seg,Visor} from '../lib/ui.js';
import {get} from '../lib/api.js';
import {useEvents,useApp} from '../lib/store.js';
import {Trace} from './inspector.js';
const KINDS=[['','всё'],['decision','решения'],['exec','команды'],['file','файлы'],['memory','память'],['settings','настройки'],['system','система']];
export function JournalScreen(){
 const {st}=useApp(),[items,setItems]=useState(null),[kind,setKind]=useState(''),[verify,setVerify]=useState(null),[error,setError]=useState(null),[filters,setFilters]=useState({actor:'',capability:'',verdict:'',channel:'',session:''}),[applied,setApplied]=useState({}),[trace,setTrace]=useState(null);
 const query=new URLSearchParams({limit:'300',kind,...applied}).toString();
 const load=()=>get('/api/journal?'+query).then(r=>setItems(r.entries),setError);
 useEffect(()=>{load();},[kind,applied]);
 useEvents(ev=>{if(ev.type==='journal'&&(!kind||ev.entry.kind===kind)&&Object.entries(applied).every(([k,v])=>!v||(k==='actor'?ev.entry.actor:ev.entry.data?.[k])===v))setItems(xs=>[ev.entry,...(xs||[])].slice(0,300));},[kind,applied]);
 return html`<div class="page"><div class="page-inner"><p class="note">Фильтры используют записанные поля; старые события могут не иметь канала или сессии. Выборка до 300 записей, экспорт до 5000.</p>
 <div class="row wrap"><${Seg} value=${kind} options=${KINDS} onChange=${setKind}/><${Button} small onClick=${()=>get('/api/journal/verify').then(setVerify,setError)}>Проверить цепочку</${Button}></div>
 ${st?.inspectors?.enabled!==false?html`<details class="card"><summary>Фильтры и экспорт</summary><div class="grid2">${[['actor','Кто'],['capability','Способность'],['verdict','Решение'],['channel','Канал'],['session','Сессия']].map(([key,label])=>html`<${Field} label=${label}><input class="input" aria-label=${label} value=${filters[key]} onInput=${e=>setFilters({...filters,[key]:e.target.value})}/></${Field}>`)}</div>
 <p class="faint">Решение: allow / ask / deny; способность, например fs.write. Пустое поле — все значения.</p><div class="row wrap"><${Button} onClick=${()=>setApplied({...filters})}>Применить фильтры</${Button}><${Button} onClick=${()=>{setFilters({actor:'',capability:'',verdict:'',channel:'',session:''});setApplied({});}}>Сбросить</${Button}><a class="btn" href=${'/api/journal/export?'+query} download="journal.json">Экспорт выборки</a></div></details>`:null}
 ${verify?html`<p class=${'note '+(verify.ok?'green':'red')}>${verify.ok?`Цепочка цела: проверено записей — ${verify.checked}.`:verify.truncated?'Хвост журнала обрезан. Проверь восстановление.':`Цепочка разорвана на записи ${verify.broken_at}.`}</p>`:null}
 <${ErrorBox} error=${error}/>${!items?html`<${Visor} mode="busy"/>`:!items.length?html`<div class="note">Подходящих записей нет. Сбрось фильтры или выполни действие в Полигоне.</div>`:html`<div class="table-scroll"><table class="t"><thead><tr><th>Когда</th><th>Что</th><th>Кто</th><th>Подробности</th></tr></thead><tbody>${items.map(e=>html`<tr><td class="faint">${new Date(e.ts*1000).toLocaleString('ru')}</td><td><span class="badge">${e.kind}</span></td><td>${e.actor}</td><td>${e.summary}${e.data?.session?html`<p><${Button} small onClick=${()=>setTrace({session:e.data.session,turn:e.data.first})}>Открыть ход и запросы</${Button}></p>`:null}${e.data&&Object.keys(e.data).length?html`<details><summary>Подробности</summary><pre class="block">${JSON.stringify(e.data,null,2)}</pre></details>`:null}</td></tr>`)}</tbody></table></div>`}
 ${trace?html`<${Trace} ...${trace} onClose=${()=>setTrace(null)}/>`:null}</div></div>`;
}
