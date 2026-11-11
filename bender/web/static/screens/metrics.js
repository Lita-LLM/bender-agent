// История событий, без таймеров и модельной самооценки.
import {html,useEffect,useState,ErrorBox} from '../lib/ui.js';
import {get} from '../lib/api.js';
import {useEvents} from '../lib/store.js';
function Plot({values,title,labels=[],scale=1}){
 const top=Math.max(scale,0.000001,...values),points=values.map((v,i)=>`${20+i*560/Math.max(1,values.length-1)},${110-v*90/top}`).join(' ');
 return html`<figure style="margin:12px 0"><figcaption>${title}</figcaption>${values.length?html`<svg viewBox="0 0 600 140" role="img" aria-label=${title} style="width:100%;max-height:190px"><title>${title}</title><line x1="20" y1="110" x2="580" y2="110" stroke="currentColor" opacity="0.3"/><polyline points=${points} fill="none" stroke="currentColor" stroke-width="2"/>${values.map((v,i)=>html`<circle cx=${20+i*560/Math.max(1,values.length-1)} cy=${110-v*90/top} r="4" fill="currentColor"><title>${labels[i]||''}: ${v}</title></circle>`)}<text x="20" y="133" font-size="11" fill="currentColor">${labels[0]||''}</text><text x="580" y="133" text-anchor="end" font-size="11" fill="currentColor">${labels[labels.length-1]||''}</text></svg>`:html`<p class="faint">Пока нет данных.</p>`}</figure>`;
}
export function MetricsPanel({revision}){
 const [data,setData]=useState(null),[error,setError]=useState(null);
 const load=()=>get('/api/workshop/metrics').then(setData).catch(setError);
 useEffect(()=>{load();},[revision]);
 useEvents(ev=>{if(['turn_end','reaction'].includes(ev.type))load();},[]);
 return html`<div class="card metrics-panel"><h3>Метрики</h3><${ErrorBox} error=${error}/>${data?html`
 ${!data.enabled?html`<p class="note">Учёт новых исходов выключен. Сохранённая история доступна.</p>`:null}
 <p>Ходы без ошибки: ${data.turns.successful} / ${data.turns.total} · ${data.turns.rate===null?'—':(data.turns.rate*100).toFixed(1)+'%'}<br/>Субагенты: ${data.tasks.successful} / ${data.tasks.total} · провал схемы: ${data.tasks.invalid_schema}<br/>Реакции хозяина: 👍 ${data.owner_reactions?.["👍"]||0} · 👎 ${data.owner_reactions?.["👎"]||0}<br/>Поправки по фразам: ${data.corrections} · автооткаты: ${data.rollbacks} · использований навыков: ${data.skill_uses}</p>
 <p>Средняя цена хода: ${data.turns.mean_cost===null?'—':'$'+data.turns.mean_cost.toFixed(4)} · субагента: $${data.tasks.mean_cost.toFixed(4)}<br/>Все вызовы: ${data.billing.calls} · расходы всего: $${data.billing.cost.toFixed(4)}</p>
 <${Plot} title="Доля ходов без технической ошибки по дням" values=${data.series.map(r=>r.successful/r.total)} labels=${data.series.map(r=>r.day)}/>
 <${Plot} title="Цена хода по дням, $" scale=${0} values=${data.series.map(r=>r.cost/r.total)} labels=${data.series.map(r=>r.day)}/>
 <${Plot} title="Экзамены во времени: 1 — пройдены, 0 — провалены" values=${data.exams.map(r=>r.ok?1:0)} labels=${data.exams.map(r=>new Date(r.at*1000).toLocaleDateString('ru'))}/>
 <details><summary>Использование навыков</summary>${data.skills.map(r=>html`<p>${r.name}: ${r.uses||0}, без ошибки ${r.helped||0}, с ошибкой ${r.failed||0}</p>`)}</details><p class="faint">${data.limits}</p>`:null}</div>`;
}
