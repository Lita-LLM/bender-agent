// Одноразовая проверка и просмотр состава технического отчёта.
import {html,useEffect,useState,Button,ErrorBox} from '../lib/ui.js';
import {get,post} from '../lib/api.js';
const LABELS={python:'Python',os:'ОС',path:'Папка',bytes:'Байт',wal_bytes:'WAL, байт',kind:'Изоляция',limits:'Лимиты',port:'Порт',in_use:'Занят',candidates:'Кандидатов',enabled:'Включён',state:'Состояние',configured:'Настроено',last:'Последний снимок',verified:'Проверен',last_verify:'Последняя проверка',free_bytes:'Свободно, байт',total_bytes:'Всего, байт',starts:'Запусков',safe:'Безопасный режим',watched:'Под Сторожем',pending:'Ждёт проверки',has_previous:'Есть прежний слот',available:'Замер доступен',running:'Работает',pid:'PID',rss_mb:'Память, МБ',threads:'Потоков',fds:'Дескрипторов',wchar:'Записано процессом, байт',write_bytes:'Записано на диск, байт'};
export function DoctorScreen(){
 const [state,setState]=useState(null),[models,setModels]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(null),[plan,setPlan]=useState(null);
 const run=async fn=>{setBusy(true);setError(null);try{await fn();}catch(e){setError(e);}finally{setBusy(false);}};
 useEffect(()=>{run(async()=>setState(await get('/api/doctor')));},[]);
 const download=()=>run(async()=>{const response=await post('/api/report',{reviewed:true}),url=URL.createObjectURL(await response.blob());const link=document.createElement('a');link.href=url;link.download='bender-report.zip';link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);});
 return html`<div class="page"><div class="page-inner"><h2>Самопроверка</h2>
 <p class="note">Проверка выполняется по кнопке. Ключи и содержимое разговоров здесь не показываются. В терминале: <code>bender doctor</code>.</p>
 <label><input type="checkbox" checked=${models} onChange=${e=>setModels(e.target.checked)}/> Проверить настроенные роли маленьким запросом (может стоить денег)</label>
 <p class="faint">Без галочки запросов к моделям нет. Уши получают секунду тишины, Голос — слово «Готов»; выключенный голос не включается.</p>
 <${Button} busy=${busy} onClick=${()=>run(async()=>setState(await post('/api/doctor',{models})))}>Проверить сейчас</${Button}>
 <${ErrorBox} error=${error}/>
 ${state?.rows?.some(row=>row.id==='role_links'&&row.details.broken)?html`<p><${Button} busy=${busy} onClick=${()=>run(async()=>setState(await post('/api/doctor',{repair_roles:true})))}>Починить ссылки в ролях</${Button}></p>`:null}
 ${state?.repair?html`<p class="note">${state.repair.message}</p>`:null}
 <div aria-live="polite">${state?.rows?.map(row=>html`<div class="card"><h3>${{ok:'✅',warn:'⚠️',error:'❌'}[row.level]} ${row.title}</h3>${row.action?html`<p>${row.action}</p>`:null}
 ${Object.entries(row.details||{}).map(([key,value])=>html`<div class="break-word">${LABELS[key]||key}: ${value===null?'—':typeof value==='boolean'?(value?'да':'нет'):typeof value==='object'?JSON.stringify(value):String(value)}</div>`)}</div>`)}</div>
 <div class="card"><h3>Отчёт для разработчика</h3><p>Архив не шифруется. Сначала просмотри состав; исходные сообщения логов исключены, поэтому причина сбоя может потребовать отдельного объяснения.</p>
 <${Button} disabled=${busy} onClick=${()=>run(async()=>setPlan(await get('/api/report/preview')))}>Подготовить отчёт</${Button}>
 ${plan?html`<ul>${plan.files.map(item=>html`<li><code>${item.name}</code> — ${item.description}</li>`)}</ul><p>Исключено: ${plan.excluded}</p><${Button} busy=${busy} onClick=${download}>Скачать просмотренный отчёт</${Button}>`:null}</div></div></div>`;
}
