// Состояние меняется только по кнопкам/событиям. Фоновых запросов к модели нет.
import {html,useState,Button,ErrorBox} from '../lib/ui.js';
import {post} from '../lib/api.js';

export function McpTasks({tasks,onChanged}){
  const [busy,setBusy]=useState(false),[error,setError]=useState(null);
  const run=async(id,action)=>{setBusy(true);setError(null);try{onChanged(await post(`/api/mcp/tasks/${id}/${action}`));}catch(e){setError(e);}finally{setBusy(false);}};
  return html`<section class="card mcp-tasks"><h3>Фоновые задачи MCP</h3><p class="faint">Статус и ввод по кнопке. Отмена — просьба серверу, она не откатывает сделанное. Результат остаётся недоверенными данными и сам не запускает модель.</p>
    <${ErrorBox} error=${error}/>
    ${!tasks.length?html`<p class="faint">Сохранённых задач пока нет. Включи приём задач в нужном подключении и заново просмотри определения.</p>`:null}
    ${tasks.map(task=>html`<details class="mcp-task" data-task=${task.id}><summary>${task.server}/${task.name} · ${task.state.status}${task.cancel_requested?' · отмена запрошена':''}</summary>
      <pre style="white-space:pre-wrap;overflow-wrap:anywhere">${JSON.stringify(task.state,null,2)}</pre>
      <div class="row"><${Button} small busy=${busy} onClick=${()=>run(task.id,'status')}>Проверить статус</${Button}>
        ${task.state.status==='input_required'?html`<${Button} small busy=${busy} onClick=${()=>run(task.id,'input')}>Ответить на запрос задачи</${Button}>`:null}
        ${['working','input_required'].includes(task.state.status)?html`<${Button} small busy=${busy} onClick=${()=>run(task.id,'cancel')}>Попросить отмену</${Button}>`:null}
        <${Button} small busy=${busy} onClick=${()=>run(task.id,'forget')}>Удалить локальную запись</${Button}></div></details>`)}
  </section>`;
}

export function McpSubscription({server,tasks,status,onChanged}){
  const [busy,setBusy]=useState(false),[error,setError]=useState(null),[resources,setResources]=useState(false),[taskEvents,setTaskEvents]=useState(false);
  const run=async stop=>{setBusy(true);setError(null);try{
    const notifications={toolsListChanged:true,promptsListChanged:true,resourcesListChanged:true};
    if(resources)notifications.resourceSubscriptions=(server.content||[]).filter(item=>item.kind==='resources'&&item.enabled).map(item=>item.key);
    if(taskEvents)notifications.taskIds=tasks.filter(task=>task.server===server.id).map(task=>task.id);
    onChanged(await post(`/api/mcp/${server.id}/subscriptions`,stop?{stop:true}:{notifications}));
  }catch(e){setError(e);}finally{setBusy(false);}};
  return html`<details class="mcp-subscriptions"><summary>Подписка на события · ${status.active?'открыта':'закрыта'}</summary>
    <p class="faint">Изменение каталога отправит определения на повторный просмотр. Данные ресурсов не читаются автоматически.</p>
    <label class="row"><input type="checkbox" checked=${resources} onChange=${event=>setResources(event.target.checked)}/> События одобренных ресурсов</label>
    <label class="row"><input type="checkbox" checked=${taskEvents} onChange=${event=>setTaskEvents(event.target.checked)}/> Состояния своих фоновых задач</label>
    <p>${status.message||''}</p>${status.acknowledged?html`<pre style="white-space:pre-wrap;overflow-wrap:anywhere">${JSON.stringify(status.notifications,null,2)}</pre>`:null}
    <${ErrorBox} error=${error}/><${Button} small busy=${busy} disabled=${!server.enabled} onClick=${()=>run(!!status.active)}>${status.active?'Закрыть подписку':'Открыть подписку'}</${Button}>
  </details>`;
}
