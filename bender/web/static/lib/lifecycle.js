// Явное подтверждение общего действия; ожидание и отмена ходов — выбор хозяина.
import {html,useState,useEffect,Button,Modal,ErrorBox} from './ui.js';
import {get,post} from './api.js';
import {refresh,useApp} from './store.js';

export function ControlButton({action='restart',small=false}){
 const {st}=useApp();const [boot,setBoot]=useState(null);
 const [open,setOpen]=useState(false),[state,setState]=useState(null),[policy,setPolicy]=useState('wait'),[error,setError]=useState(null),[result,setResult]=useState(null),[busy,setBusy]=useState(false);
 useEffect(()=>{if(boot!==null&&st?.boot?.starts>boot){setBoot(null);setOpen(false);location.reload();}},[boot,st?.boot?.starts]);
 const title=action==='restart'?'Перезапустить':'Выключить';
 const show=async()=>{setOpen(true);setResult(null);setError(null);try{setState(await get('/api/lifecycle'));}catch(e){setError(e);}};
 const apply=async()=>{setBusy(true);setError(null);try{const r=await post('/api/control/'+action,{policy,confirmed:true});setResult(r.message);if(action==='restart')setBoot(st?.boot?.starts??0);refresh().catch(()=>{});}catch(e){setError(e);}finally{setBusy(false);}};
 return html`<${Button} small=${small} kind=${action==='shutdown'?'danger':''} onClick=${show}>${title}</${Button}>
 ${open?html`<${Modal} title=${title+' Бендера?'} onClose=${()=>setOpen(false)}><${ErrorBox} error=${error}/>
 ${result?html`<p class="note">${result}</p>`:html`<p>Действие относится ко всей стае. Текущих ходов: ${state?.busy?.length??'…'}. При ожидании новые ходы не начинаются.</p>
 ${state?.busy?.length?html`<p class="faint">${state.busy.map(row=>row.agent+' · '+row.title).join('; ')}</p>`:null}
 <div style="display:grid;gap:10px"><label><input type="radio" name=${'control-'+action} checked=${policy==='wait'} onChange=${()=>setPolicy('wait')}/> Дождаться текущих ходов</label>
 <label><input type="radio" name=${'control-'+action} checked=${policy==='interrupt'} onChange=${()=>setPolicy('interrupt')}/> Прервать текущие ходы</label></div>
 ${action==='shutdown'?html`<p class="note">${state?.kind==='systemd'?'Запустить снова: systemctl --user start bender.':'Ручной запуск остановится вместе со Сторожем. Запустить снова: bender.'}</p>`:state&&!state.watched?html`<p class="note amber">Запущен без Сторожа: перезапуск недоступен. Останови Ctrl+C и запусти bender снова.</p>`:null}
 <div class="row"><${Button} kind=${action==='shutdown'?'danger':'primary'} busy=${busy} disabled=${!state||action==='restart'&&!state.watched} onClick=${apply}>${title+' сейчас'}</${Button}><${Button} onClick=${()=>setOpen(false)}>Отмена</${Button}></div>`}
 </${Modal}>`:null}`;
}

export function StartupStatus(){
 const [data,setData]=useState(null),[error,setError]=useState(null),[note,setNote]=useState(''),[busy,setBusy]=useState(false);
 const load=()=>get('/api/service').then(setData,setError);useEffect(()=>{load();},[]);
 const install=async()=>{if(!confirm('Включить автозапуск для этой папки данных? Ручная копия останется работать, вторая не запускается.'))return;setBusy(true);try{const r=await post('/api/service/install',{confirmed:true});setNote(r.message);await load();}catch(e){setError(e);}finally{setBusy(false);}};
 return html`<div class="card"><h3>Состояние запуска</h3><${ErrorBox} error=${error}/><p>${data?.message||'Проверяю запуск…'}</p>${note?html`<p class="note">${note}</p>`:null}
 ${data?html`<p class="faint">Автозапуск ${data.installed?'установлен':'не установлен'}. ${!data.available?'Здесь нет доступного systemd пользователя или Termux:Boot.':''}</p>`:null}
 <div class="row"><${Button} disabled=${!data?.available} busy=${busy} onClick=${install}>Включить автозапуск</${Button}><${ControlButton}/><${ControlButton} action="shutdown"/></div>
 <p class="faint">Терминал: bender service install|remove|status. Отдельный установленный голос: bender service install --voice.</p></div>`;
}
