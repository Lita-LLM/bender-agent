// Происхождение → использования → ход → исходный запрос модели; правки с объяснением.
import {html,useEffect,useState,Button,Field,ErrorBox,Modal} from '../lib/ui.js';
import {get,post} from '../lib/api.js';
import {WhatWent} from './chat.js';
export function Trace({session,turn,onClose}){
 const [data,setData]=useState(null),[error,setError]=useState(null),[call,setCall]=useState(null);
 useEffect(()=>{Promise.all([get(`/api/session/${session}/messages?limit=300`),get(`/api/session/${session}/calls`)]).then(([messages,calls])=>setData({...messages,...calls}),setError);},[session]);
 return html`<${Modal} title="Ход и запросы модели" onClose=${onClose} wide><${ErrorBox} error=${error}/><p class="note">Последние 300 сообщений и 100 вызовов сессии. Исходное сообщение выделено; история не переписывается инспектором.</p>
 ${data?.messages.map(m=>html`<details class=${m.id===turn?'note amber':''} open=${m.id===turn}><summary>${m.role} · ${m.id}</summary><pre class="block">${typeof m.content==='string'?m.content:JSON.stringify(m)}</pre></details>`)}
 <h3>Что ушло в модель</h3>${data?.calls.map(c=>html`<div><${Button} small onClick=${()=>setCall(c.id)}>${c.role} · ${c.model} · ${new Date(c.created_at*1000).toLocaleString('ru')}</${Button}></div>`)}
 ${call?html`<${WhatWent} callId=${call} onClose=${()=>setCall(null)}/>`:null}</${Modal}>`;
}
export function MemoryInspector({ids,onClose,onChanged}){
 const [data,setData]=useState(null),[text,setText]=useState(''),[reason,setReason]=useState(''),[error,setError]=useState(null),[busy,setBusy]=useState(false),[trace,setTrace]=useState(null);
 useEffect(()=>{Promise.all(ids.map(id=>get('/api/inspectors/memory/'+id))).then(ds=>{const d=ds[0];setData({...d,hashes:Object.fromEntries(ds.map(r=>[r.id,r.source_hash]))});let text=ds.map(r=>r.editable_text).join('\n\n');if(d.kind==='fact'&&ds.length>1){const f=JSON.parse(d.editable_text);f.object+='; '+ds.slice(1).map(r=>r.text).join('; ');text=JSON.stringify(f,null,2);}setText(text);},setError);},[ids.join(',')]);
 const change=async action=>{setBusy(true);setError(null);try{await post('/api/inspectors/memory',{ids,action,text,reason,hash:data.hashes});onChanged();onClose();}catch(e){setError(e);}finally{setBusy(false);}};
 return html`<${Modal} title="Инспектор памяти" onClose=${onClose} wide><${ErrorBox} error=${error}/>${data?html`
 <p class="break-word">Источник: ${data.title||data.doc_key} · ${data.ref} · ${data.kind} · ${data.provenance==='untrusted'?'⚠ Недоверенное':data.provenance}</p>
 ${data.origin?html`<p>Канал: ${data.origin.meta.channel||'не записан'} · отправитель: ${data.origin.meta.actor||'не записан'} · ${data.origin.meta.untrusted?'пересланное/недоверенное':''}<${Button} small onClick=${()=>setTrace({session:data.origin.session_id,turn:data.origin.id})}>Открыть исходный ход</${Button}></p>`:null}
 <p>Recall: ${data.usage.hits} раз · последний: ${data.usage.last?new Date(data.usage.last*1000).toLocaleString('ru'):'не использовалась'}</p>
 <details><summary>Ходы, где использовалась запись (${data.uses.length})</summary>${data.uses.map(u=>html`<p><${Button} small onClick=${()=>setTrace({session:u.session,turn:u.turn})}>${new Date(u.at*1000).toLocaleString('ru')} · ${u.channel||'канал не записан'}${u.tainted?' · недоверенное':''}</${Button}></p>`)}</details>
 <p class="note">Файл ядра/дневника: редактор меняет весь исходный абзац, включая продолжения. Факт: JSON subject/relation/object. Разговор остаётся в истории; «забыть» убирает запись из recall и автоматического продвижения. Счётчик использования — по тексту; до появления инспектора связей с ходами не было.</p>
 <${Field} label="Новый текст записи"><textarea class="textarea mono" aria-label="Новый текст записи" value=${text} onInput=${e=>setText(e.target.value)}/></${Field}>
 <${Field} label="Причина изменения"><input class="input" aria-label="Причина изменения" value=${reason} onInput=${e=>setReason(e.target.value)}/></${Field}>
 <div class="row wrap"><${Button} busy=${busy} disabled=${reason.trim().length<3} onClick=${()=>change(ids.length>1?'merge':'edit')}>${ids.length>1?'Слить выбранные записи':'Сохранить правку'}</${Button}><${Button} kind="danger" busy=${busy} disabled=${reason.trim().length<3} onClick=${()=>change('forget')}>Забыть выбранное</${Button}></div>
 ${ids.length>1?html`<p class="note">Общий текст останется в первой записи; остальные будут убраны. При смешанном происхождении он останется недоверенным.</p>`:null}
 <details><summary>История правок с объяснениями</summary>${data.history.map(h=>html`<p>${h.action} · ${h.reason}</p>`)}</details>`:null}
 ${trace?html`<${Trace} ...${trace} onClose=${()=>setTrace(null)}/>`:null}</${Modal}>`;
}
