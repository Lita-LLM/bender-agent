// Явный ответ хозяина внешнему серверу: данные формы не становятся поручением агенту.
import {html,useState} from '../lib/ui.js';
import {post} from '../lib/api.js';
import {Button,ErrorBox,Field} from '../lib/ui.js';

function options(field){
  const node=field.type==='array'?(field.items||{}):field;
  return node.enum?.map((value,index)=>[value,node.enumNames?.[index]||value])||(node.oneOf||node.anyOf)?.map(item=>[item.const,item.title||item.const]);
}

export function McpInputCard({item,onAnswer}){
  const fields=item.requestedSchema?.properties||{};
  const [values,setValues]=useState(()=>Object.fromEntries(Object.entries(fields).filter(([,field])=>field.type==='boolean').map(([name])=>[name,false])));
  const [error,setError]=useState(null),[busy,setBusy]=useState(false);
  const change=(name,value)=>setValues(previous=>({...previous,[name]:value}));
  const answer=async(action)=>{
    setBusy(true);setError(null);
    try{await post(`/api/mcp/inputs/${item.id}`,{action,content:values});onAnswer();}catch(error){setError(error);}finally{setBusy(false);}
  };
  return html`<div class="card mcp-input" data-input=${item.id}>
    <h3>Запрос MCP ${item.server} · агент ${item.agent}</h3>
    <p class="note">${item.message}</p>
    <p class="faint">Чужой запрос. Ответ получит сервер ${item.server}. Пароли, ключи и токены в форму не вводи.</p>
    ${item.mode==='url'?html`<p><a href=${item.url} target="_blank" rel="noopener noreferrer">Открыть сайт сервиса</a></p><p class="mono" style="overflow-wrap:anywhere">${item.url}</p>`:
      Object.entries(fields).map(([name,field])=>{const variants=options(field);return html`<${Field} label=${String(field.title||name)+(item.requestedSchema.required?.includes(name)?' *':'')} hint=${String(field.description||'')}>
        ${field.type==='boolean'?html`<label><input type="checkbox" aria-label=${name} checked=${!!values[name]} onChange=${event=>change(name,event.target.checked)}/> Да</label>`:
          variants?html`<select class="select" aria-label=${name} multiple=${field.type==='array'} value=${values[name]??(field.type==='array'?[]:'')} onChange=${event=>change(name,field.type==='array'?Array.from(event.target.selectedOptions,option=>option.value):event.target.value)}>
            ${field.type!=='array'?html`<option value="">Выбери</option>`:null}${variants.map(([value,title])=>html`<option value=${value}>${String(title)}</option>`)}</select>`:
          html`<input class="input" aria-label=${name} type=${['integer','number'].includes(field.type)?'number':'text'} step=${field.type==='integer'?'1':'any'} value=${values[name]??''} onInput=${event=>change(name,field.type==='string'?event.target.value:(event.target.value===''?undefined:Number(event.target.value)))}/>`}
      </${Field}>`;})}
    <${ErrorBox} error=${error}/><div class="row"><${Button} kind="primary" busy=${busy} onClick=${()=>answer('accept')}>${item.mode==='url'?'Я завершил на сайте':'Отправить серверу'}</${Button}>
      <${Button} disabled=${busy} onClick=${()=>answer('decline')}>Отказать</${Button}><${Button} disabled=${busy} onClick=${()=>answer('cancel')}>Закрыть</${Button}></div>
  </div>`;
}
