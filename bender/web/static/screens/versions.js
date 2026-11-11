// Версия готовится отдельно, точный пакет просматривается до переключения слота.
import {html,useEffect,useState,Button,Field,ErrorBox} from '../lib/ui.js';
import {get,post} from '../lib/api.js';
import {PendingCards} from './chat.js';
const ago=s=>s<60?s+' с':Math.floor(s/60)+' мин '+(s%60)+' с';
const size=b=>b>=1<<30?(b/(1<<30)).toFixed(1)+' ГБ':Math.round(b/(1<<20))+' МБ';
function UpdateStatus({progress,busy,note}){
 const step=progress?.step,last=progress?.last,b=step?.backup;
 if(step)return html`<p class="note amber" role="status">Сейчас: ${step.title} · ${ago(step.seconds)}${b?` · файлов ${b.files} из ${b.total}, прочитано ${size(b.read)}${b.folder?`, сейчас ${b.folder}`:''}${b.first?' (первый такой бекап читает всё; следующие — только изменённое)':''}`:''}. Всего ${ago(step.total)}. Можно уйти с экрана — обновление идёт в Бендере.</p>`;
 if(note)return html`<p class="note">${note}</p>`;
 if(busy)return html`<p class="note faint" role="status">Жду ответа Бендера…</p>`;
 return last?html`<p class=${'note '+(last.ok?'green':'red')}>Прошлое обновление, ${new Date(last.at*1000).toLocaleString('ru-RU')}: ${last.message}</p>`:null;
}
export function VersionsScreen(){
 const [state,setState]=useState(null),[source,setSource]=useState(''),[git,setGit]=useState(false),[ref,setRef]=useState(''),[development,setDevelopment]=useState(false);
 const [error,setError]=useState(null),[busy,setBusy]=useState(false),[selected,setSelected]=useState(null),[file,setFile]=useState('bender/__init__.py'),[text,setText]=useState(null),[note,setNote]=useState('');
 const [plan,setPlan]=useState(null),[noBackup,setNoBackup]=useState(false);
 // Обновление идёт на сервере долго (бекап, самопроверка): шаг и итог видны прямо под кнопкой.
 const [updating,setUpdating]=useState(false),[progress,setProgress]=useState(null),[updError,setUpdError]=useState(null),[updNote,setUpdNote]=useState('');
 const poll=()=>get('/api/update/progress').then(setProgress,()=>{});
 useEffect(()=>{poll();},[]);
 useEffect(()=>{if(!updating&&!progress?.step)return;const t=setInterval(poll,1000);return()=>clearInterval(t);},[updating,!!progress?.step]);
 const update=async fn=>{setUpdating(true);setUpdError(null);setUpdNote('');try{await fn();}catch(e){setUpdError(e);}setUpdating(false);poll();};
 const load=()=>get('/api/versions').then(setState);
 const run=async fn=>{setBusy(true);setError(null);try{const result=await fn();if(result!=="restart")await load();}catch(e){setError(e);}setBusy(false);};
 useEffect(()=>{run(async()=>{});},[]);
 const preview=(id,path='bender/__init__.py',offset=0)=>run(async()=>{setSelected(id);setFile(path);setText(await get(`/api/versions/${id}/file?path=${encodeURIComponent(path)}&offset=${offset}`));});
 return html`<div class="page"><div class="page-inner"><div class="row"><h2 class="grow">Версии и откат</h2><${Button} small onClick=${()=>run(async()=>{})}>Обновить состояние</${Button}></div>
 <div class="card"><h3>Обычное обновление</h3><p>Своя ветка git, бекап и самопроверка без запросов к моделям. Для приватного GitHub используется вход хозяина через gh.</p>
 <${Button} busy=${updating} disabled=${!!progress?.step} onClick=${()=>update(async()=>{const p=await post('/api/update/check');setPlan(p);setUpdNote(p.available?'Просмотри коммиты перед установкой.':'Установлена последняя версия.');})}>Проверить обновления</${Button}>
 ${plan?.available ? html`<pre class="module-text">${plan.commits.join('\n')}</pre><label class="row"><input type="checkbox" checked=${noBackup} onChange=${e=>setNoBackup(e.target.checked)}/> Без бекапа, один раз (если бекап не получается; самопроверка и откат остаются)</label><${Button} kind="primary" busy=${updating} disabled=${!!progress?.step} onClick=${()=>{if(confirm(noBackup?'Обновить БЕЗ бекапа и перезапустить?':'Обновить программу после бекапа и перезапустить?'))update(async()=>{const r=await post('/api/update/install',{...plan,skip_backup:noBackup});setUpdNote(r.message+' '+(r.backup?.path?'Бекап: '+r.backup.path+'. ':r.backup?.skipped?'Без бекапа. ':'')+(r.restart||''));setPlan(null);setNoBackup(false);});}}>Обновить программу</${Button}>`:null}
 <details><summary class="faint">Ветка разошлась с сервером?</summary><p class="faint">Бывает, когда на сервере пересобрали историю. Если своих коммитов у тебя нет, кнопка приведёт клон к серверной ветке; прежний коммит сохранится в отдельной ветке, данные не тронутся.</p><${Button} small busy=${updating} onClick=${()=>{if(confirm('Выровнять клон с сервером? Прежний коммит сохранится в отдельной ветке; потребуется перезапуск.'))update(async()=>{const r=await post('/api/update/realign',{skip_backup:noBackup});setUpdNote(r.message+' '+(r.restart||''));});}}>Выровнять с сервером</${Button}>
 <p class="faint">«В папке программы есть локальные правки», а правок ты не делал (часто на Windows из-за перевода строк)? Чистая переустановка вернёт программу точно к серверной, данные, ключи и агенты не тронет.</p><${Button} small kind="danger" busy=${updating} onClick=${()=>{if(confirm('Стереть все правки и лишние файлы в папке программы и поставить серверную версию? Данные, ключи и агенты останутся; перед этим делается бекап; потребуется перезапуск.'))update(async()=>{const r=await post('/api/update/realign',{skip_backup:noBackup,clean:true});setUpdNote(r.message+' '+(r.restart||''));});}}>Чистая переустановка</${Button}></details>
 <${UpdateStatus} progress=${progress} busy=${updating} note=${updNote}/><${PendingCards}/><${ErrorBox} error=${updError}/>
 <p class="faint">Режим: Настройки → Версии → обычное обновление git. ZIP: распакуй свежую программу отдельно и запусти с прежней папкой данных.</p></div>
 <p class="note">Сначала просмотр пакета, затем одобрение хозяина. Новая версия сообщает о здоровье базы, роутера, пульта и планировщика. Если старт не удался — Сторож вернёт прежний слот. Данные остаются на месте.</p>
 ${state?html`<div class="card"><p>Текущий слот: <code>${state.current}</code></p><p>Прежний: <code>${state.previous||'пока нет'}</code></p>${state.pending?html`<p class="note amber">Самопроверка: ${state.pending}. Обнови состояние после её срока.</p>`:null}
 ${state.health?html`<p class="note green">Самопроверка пройдена: ${state.health.slot}</p>`:null}
 ${state.last_rollback?html`<p class="note amber">Сработал откат: ${state.last_rollback.reason}</p>`:null}
 ${!state.watched?html`<p class="note amber">Для применения и отката нужен обычный запуск со Сторожем (bender run).</p>`:null}
 <${Button} kind="danger" disabled=${!state.previous||!state.watched} onClick=${()=>run(async()=>{setState(await post('/api/versions/rollback'));setNote('Откат выбран, Бендер перезапускается. Обнови состояние через несколько секунд.');return 'restart';})}>Откатить на прежнюю</${Button}></div>`:null}
 <div class="card"><h3>Подготовить версию</h3><label><input type="checkbox" checked=${git} onChange=${e=>setGit(e.target.checked)}/> Из git по HTTPS</label>
 <${Field} label="Источник версии"><input class="input mono" aria-label="Источник версии" value=${source} onInput=${e=>setSource(e.target.value)} placeholder=${git?'https://github.com/хозяин/проект.git':'Папка или подписанный ZIP'}/></${Field}>
 ${git?html`<${Field} label="Ветка"><input class="input" value=${ref} onInput=${e=>setRef(e.target.value)}/></${Field}>`:null}
 <label><input type="checkbox" aria-label="Собственная разработка" checked=${development} onChange=${e=>setDevelopment(e.target.checked)}/> Неподписанная собственная разработка (папка/git)</label><p class="faint">Этот режим отдельно разрешается хозяином в «Настройках → Версии». ZIP требует подписи Ed25519 доверенным ключом; открытые ключи добавляются там же после независимой сверки.</p>
 <${Button} busy=${busy} disabled=${!source} onClick=${()=>run(async()=>{const r=await post('/api/versions/stage',git?{git:source,ref}:{path:source,development});await preview(r.id);})}>Подготовить и просмотреть</${Button}></div>
 <${ErrorBox} error=${error}/>${note?html`<p class="note">${note}</p>`:null}
 ${state?.candidates?.map(c=>html`<div class="card version-card"><h3>${c.version}</h3><p class=${`badge ${c.signed?'green':'amber'}`}>${c.signed?'Доверенная подпись Ed25519':'БЕЗ ПОДПИСИ · разработка хозяина'}</p><p class="mono">${c.hash}</p><p>Файлов: ${c.files} · ${(c.bytes/1024/1024).toFixed(2)} МБ</p><${Button} small onClick=${()=>preview(c.id)}>Просмотреть файлы</${Button}>
 ${selected===c.id?html`<select class="select mono" value=${file} onChange=${e=>preview(c.id,e.target.value)}>${Object.keys(c.manifest).map(p=>html`<option value=${p}>${p}</option>`)}</select><pre class="module-text mono">${text?.text||''}</pre>${text?.next!==null?html`<${Button} small onClick=${()=>preview(c.id,file,text.next)}>Следующий фрагмент</${Button}>`:null}<${Button} kind="primary" disabled=${!state.watched} onClick=${()=>run(async()=>{setState(await post(`/api/versions/${c.id}/install`,{hash:c.hash}));setNote('Слот выбран, Бендер перезапускается. Обнови состояние через несколько секунд.');return 'restart';})}>Применить просмотренную версию</${Button}>`:null}</div>`)}
 <details><summary>История обновлений</summary><pre class="module-text mono">${JSON.stringify(state?.history||[],null,2)}</pre></details>
 <p class="faint">Полигон и терминал: try update / try rollback. Защитные настройки агент через settings не меняет.</p></div></div>`;
}
