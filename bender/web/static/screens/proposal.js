// Просмотр кода, условий и результатов до одобрения; определения MCP отдельно.
import {html,useState,Button} from '../lib/ui.js';
import {get,post} from '../lib/api.js';
const LABELS={testing:'Проверяется',waiting:'Ждёт хозяина',failed:'Провалено',approved:'Код одобрен',installed:'Самопроверка',applied:'Применено',archived:'Прежняя версия',disabled:'Выключен',rolled_back:'Откачено',rejected:'Отклонено'};
export function DiffView({text}){
 const [only,setOnly]=useState(true),[wrap,setWrap]=useState(true);
 const lines=(text||'').split('\n');
 return html`<div class="diff-view"><label><input type="checkbox" checked=${only} onChange=${e=>setOnly(e.target.checked)}/> Только изменения</label> <label><input type="checkbox" checked=${wrap} onChange=${e=>setWrap(e.target.checked)}/> Перенос строк</label>
 ${only?html`<details><summary>Неизменённые строки</summary><pre class="module-text mono">${lines.filter(l=>!l.startsWith('+')&&!l.startsWith('-')).join('\n')}</pre></details>`:null}
 <pre class=${`module-text mono proposal-diff${wrap?' wrap-diff':''}`}>${lines.filter(l=>!only||l.startsWith('+')||l.startsWith('-')||l.startsWith('@@')).map(l=>html`<span class=${l.startsWith('+')?'green':l.startsWith('-')?'red':''}>${l+'\n'}</span>`)}</pre></div>`;
}
export function ProposalCard({record,run}){
 const [detail,setDetail]=useState(null),[mcp,setMcp]=useState(null);
 const r=record;
 const preview=()=>run(async()=>{setDetail(await get(`/api/workshop/${r.id}`));});
 if(r.kind==='core')return html`<details class="card proposal-card legacy-core" data-proposal=${r.id}><summary>История: ${r.name||r.description} · ${LABELS[r.status]||r.status}</summary><p>Правка ядра агентом удалена по решению автора. Это сохранённая история без применения.</p><${DiffView} text=${r.diff}/></details>`;
 return html`<div class="card proposal-card" data-proposal=${r.id}><div class="row"><h3 class="grow">${r.name||r.description}</h3><span class="badge">${LABELS[r.status]||r.status}</span></div>
 <p>${r.description}</p><p class="mono">${r.hash}</p><p>Способности: ${(r.capabilities||[]).join(', ')||'нет'}</p>
 ${r.kind==='tool'?html`<p class="faint">exec — программы внутри изоляции; fs.read/fs.write — рабочая папка. Настоящие данные Бендера закрыты. Сеть — только через посредник на одобренные домены.</p>`:null}
 ${(r.network_hosts||[]).length?html`<p>Домены net.read: <strong>${r.network_hosts.join(', ')}</strong>. HTTPS CONNECT может передавать данные этим доменам.</p>`:null}
 <p>Самотесты: ${r.tests?(r.tests.ok?'пройдены':'провалены'):'—'} · Экзамены: ${r.exams?(r.exams.ok?'пройдены':'провалены'):'—'}</p>
 ${r.error?html`<p class="note amber">${r.error.title} ${r.error.hint||''}</p>`:null}
 ${r.rollback?html`<p class="note amber">Сработал откат: ${r.rollback.reason}</p>`:null}
 <${Button} small onClick=${preview}>Просмотреть код и проверки</${Button}>
 ${detail?html`<div class="proposal-preview">${Object.entries(detail.files||{}).map(([name,text])=>html`<details open><summary>${name}</summary><pre class="module-text mono">${text}</pre></details>`)}
 ${detail.diff?html`<${DiffView} text=${detail.diff}/>`:null}
 <details><summary>Диагностика самотестов</summary><pre class="module-text mono">${r.tests?.log||''}</pre></details><details><summary>Результаты экзаменов</summary><pre class="module-text mono">${JSON.stringify(r.exams,null,2)}</pre></details>
 ${r.definitions?html`<details open><summary>Определения MCP при проверке</summary><pre class="module-text mono">${JSON.stringify(r.definitions,null,2)}</pre></details>`:null}
 ${['waiting','archived'].includes(r.status)?html`<${Button} kind="primary" onClick=${()=>run(()=>post(`/api/workshop/${r.id}/approve`,{hash:r.hash}))}>${r.status==='archived'?'Вернуть эту версию тула':'Одобрить код и способности'}</${Button}>`:null}</div>`:null}
 ${['waiting','failed'].includes(r.status)?html`<${Button} small kind="danger" onClick=${()=>run(()=>post(`/api/workshop/${r.id}/reject`))}>Отклонить предложение</${Button}>`:null}
 ${r.kind==='tool'&&['approved','applied'].includes(r.status)?html`<${Button} small onClick=${()=>run(async()=>{const result=await post(`/api/workshop/${r.id}/definitions`);setMcp(result.servers.find(s=>s.id===r.server));})}>Проверить определения MCP</${Button}>
 <${Button} small onClick=${()=>run(()=>post(`/api/workshop/${r.id}/enabled`,{enabled:false}))}>Выключить тул</${Button}>`:null}
 ${r.status==='disabled'?html`<${Button} small onClick=${()=>run(()=>post(`/api/workshop/${r.id}/enabled`,{enabled:true}))}>Включить прежний тул</${Button}>`:null}
 ${mcp?html`<div><pre class="module-text mono">${JSON.stringify(mcp.tools,null,2)}</pre><${Button} small onClick=${()=>run(()=>post(`/api/workshop/${r.id}/definitions`,{review:mcp.review}))}>Одобрить просмотренные определения</${Button}><p class="faint">Имена тулов показаны также в разделе MCP; доступны после одобрения.</p></div>`:null}
 </div>`;
}
