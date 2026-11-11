// Результаты приходят от проверяющего; обновление только по событию хозяина.
import {html,useEffect,useState,Button,ErrorBox} from '../lib/ui.js';
import {get,post} from '../lib/api.js';
import {PendingCards} from './chat.js';
import {ProposalCard} from './proposal.js';
import {useEvents} from '../lib/store.js';
import {MetricsPanel} from './metrics.js';
export function WorkshopScreen(){
 const [state,setState]=useState(null),[busy,setBusy]=useState(false),[error,setError]=useState(null),[note,setNote]=useState('');
 const load=()=>get('/api/workshop').then(setState);
 const run=async fn=>{setBusy(true);setError(null);try{await fn();await load();}catch(e){setError(e);}setBusy(false);};
 useEffect(()=>{run(async()=>{});},[]);
 useEvents(ev=>{if(ev.type==='workshop')load().catch(setError);},[]);
 return html`<div class="page"><div class="page-inner"><div class="row"><h2 class="grow">Мастерская</h2><${Button} small onClick=${()=>run(async()=>{})}>Обновить</${Button}></div>
 <p class="note">Агент предлагает, проверяющий испытывает копию, хозяин применяет. Экзамены и права меняются руками в исходниках.</p><${ErrorBox} error=${error}/><${PendingCards}/>
 ${note?html`<p class="note">${note}</p>`:null}
 <div class="card"><h3>Сетевой посредник</h3><p>${state?.network_proxy?.title}</p><p class="note">${state?.network_proxy?.hint}</p><p>Домены входят в хеш пакета и просмотр перед одобрением. Самотесты и обнаружение предложения идут без сети. Выключатель — Настройки → Мастерская; проверка — <code>try proxy</code>.</p></div>
 <${MetricsPanel} revision=${state}/>
 ${(state?.proposals||[]).map(r=>html`<${ProposalCard} key=${r.id} record=${r} run=${run}/>`)}
 <div class="card"><h3>Экзамены</h3>${!state?.enabled?html`<p>Мастерская выключена в настройках.</p>`:null}
 <ul>${state?.catalog.map(c=>html`<li>${c.title} <span class="dim">${c.kind==='agent'?'фальшивая модель':'unittest'}</span></li>`)}</ul>
 <${Button} kind="primary" busy=${busy} disabled=${!state?.enabled} onClick=${()=>run(()=>post('/api/workshop/exams'))}>Запустить экзамены</${Button}>
 ${state?.last?html`<p class=${state.last.ok?'note green':'note amber'}>Последний прогон: ${state.last.ok?'пройден':'провален'} · ${state.last.seconds} с</p>`:null}</div>
 ${[...(state?.history||[])].reverse().map(r=>html`<details class="card exam-report"><summary>${new Date(r.at*1000).toLocaleString('ru')} · ${r.ok?'Пройден':'Провален'} · ${r.id}</summary><p class="mono">Копия: ${r.candidate_hash}</p>${r.error?html`<p>${r.error}</p>`:null}${r.results.map(c=>html`<details><summary>${c.ok?'✓':'×'} ${c.title}</summary><pre class="module-text mono">${c.log}</pre></details>`)}</details>`)}
 <div class="card"><h3>Живая оценка</h3><p>Главная модель отвечает, другая модель из роли «Умная» оценивает по рубрике. Это отдельная оценка качества; для допуска модуля нужны офлайн-экзамены.</p><${Button} disabled=${!state?.enabled||!state?.live_enabled} busy=${busy} onClick=${()=>run(()=>post('/api/workshop/live'))}>Запустить живую оценку</${Button}>${state?.live_history.map(r=>html`<details><summary>${r.score}/3 · $${r.cost.toFixed(4)} · ${r.model} → ${r.judge}</summary><p>${r.answer}</p><p>${r.reason}</p></details>`)}</div>
 <p class="faint">Полигон: try exams; try exams run. Живые модели выключены по умолчанию; офлайн-прогон не расходует ключи.</p></div></div>`;
}
