// Подключение, карантин и просмотр изменений определений внешних тулов.
import { html, useEffect, useState } from "../lib/ui.js";
import { get, post, del } from "../lib/api.js";
import { Button, ErrorBox, Field } from "../lib/ui.js";
import {useEvents} from '../lib/store.js';
import {McpInputCard} from './mcp-input.js';
import {McpTasks,McpSubscription} from './mcp-background.js';
import {McpOAuthPanel} from './mcp-oauth.js';

// Кому из стаи виден сервер: без списка — всем; отмеченные агенты видят его тулы, остальные — нет.
function McpAgents({ server, agents, busy, onSave }) {
  const all = !Array.isArray(server.agents), chosen = all ? [] : server.agents;
  const live = agents.filter(a => !a.archived);
  const set = list => onSave({ id: server.id, agents: list });
  return html`<div class="row mcp-agents" style="flex-wrap:wrap;gap:8px"><span class="faint">Кому доступен:</span>
    <label><input type="checkbox" aria-label=${`Всем агентам ${server.id}`} checked=${all} disabled=${busy} onChange=${e => set(e.target.checked ? null : [])} /> всем</label>
    ${live.map(a => html`<label><input type="checkbox" aria-label=${`${server.id} для ${a.name}`} checked=${all || chosen.includes(a.id)} disabled=${busy || all} onChange=${e => set(e.target.checked ? [...chosen, a.id] : chosen.filter(x => x !== a.id))} /> ${a.name}</label>`)}
    ${!all && !chosen.length ? html`<span class="badge amber">никому — тулы скрыты</span>` : null}
    <span class="faint">Помощникам нужны ещё права на MCP: Стая → Права (способность mcp.call и группа mcp).</span></div>`;
}

const EMPTY = { id: "", name: "", transport: "stdio", program: "", args: "", url: "", cwd: "", env: "{}", headers: "{}", network: false,client_features:{},client_roots:'',tasks_enabled:false };

export function McpScreen() {
  const [state, setState] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [form, setForm] = useState(null);
  const [importSecrets, setImportSecrets] = useState(false);
  const [importNote, setImportNote] = useState("");
  const [agents, setAgents] = useState([]);
  const run = async (fn) => {
    setBusy(true); setError(null);
    try { setState(await fn()); } catch (e) { setError(e); }
    setBusy(false);
  };
  useEffect(() => { run(() => get("/api/mcp")); get("/api/agents").then(value => setAgents(value.agents || []), () => {}); }, []);
  useEvents(event=>{if(['mcp_input','mcp_oauth','mcp_tasks','mcp_subscriptions','mcp_changed'].includes(event.type))get('/api/mcp').then(setState,setError);},[]);
  const change = (key) => (e) => setForm({ ...form, [key]: e.target.type === "checkbox" ? e.target.checked : e.target.value });
  const edit = (s) => setForm({ ...EMPTY, ...s, program: s.command?.[0] || "", args: (s.command || []).slice(1).join("\n"), env: JSON.stringify(s.env || {}, null, 2), headers: JSON.stringify(s.headers || {}, null, 2),client_roots:(s.client_roots||[]).join('\n'), editing: true });
  const save = () => run(async () => {
    const data = { id: form.id, name: form.name || form.id, transport: form.transport,
      env: JSON.parse(form.env), headers: JSON.parse(form.headers), network: form.network,tasks_enabled:form.tasks_enabled,client_features:form.client_features,client_roots:form.client_roots.split('\n').map(value=>value.trim()).filter(Boolean) };
    if (form.transport !== "stdio") data.url = form.url;
    else { data.command = [form.program, ...(form.args ? form.args.split("\n") : [])]; if (form.cwd) data.cwd = form.cwd; }
    const result = await post("/api/mcp", data); setForm(null); return result;
  });
  return html`<div class="page mcp-page"><div class="page-inner">
    <div class="note">Внешний сервер получает только те вызовы, которые пропускает Предохранитель. Новые и изменённые определения тулов требуют твоего просмотра. Ответы и описания считаются недоверенными.</div>
    <div class="faint">Локальные серверы запускаются в доступной песочнице; без неё — с правами пользователя. Предохранитель проверяет подключение и вызовы, но не действия внутри чужой программы.</div>
    ${state?.safe ? html`<div class="note amber">Безопасный режим: MCP-подключения выключены.</div>` : null}
    <div class="row"><h2 class="grow">Внешние MCP-серверы</h2><${Button} onClick=${() => setForm({ ...EMPTY })}>Добавить сервер</${Button}></div>
    <label class="row"><input type="checkbox" checked=${!!state?.enabled} onChange=${(e) => run(async () => { await post("/api/settings/mcp", { enabled: e.target.checked }); return get("/api/mcp"); })} /> MCP-клиент включён</label>
    <label class="row"><input type="checkbox" aria-label="Ресурсы и шаблоны MCP" checked=${!!state?.content_enabled} onChange=${(e)=>run(async()=>{await post('/api/settings/mcp',{content_enabled:e.target.checked});return get('/api/mcp');})} /> Ресурсы и шаблоны MCP</label>
    <p class="faint">Каталог ресурсов и шаблонов одобряется отдельно от тулов. Чтение — тулом mcp_content в Полигоне; шаблоны остаются недоверенными данными.</p>
    <label class="row"><input type="checkbox" aria-label="Встречные запросы MCP" checked=${!!state?.client_enabled} onChange=${event=>run(async()=>{await post('/api/settings/mcp',{client_enabled:event.target.checked});return get('/api/mcp');})}/> Встречные запросы MCP</label>
    <p class="faint">Дополнительно разреши нужное в каждом подключении. Сервер не получает твои тулы/историю; sampling использует только настроенную Быструю модель с потолком ответа. Пределы — Настройки → Внешние MCP-серверы.</p>
    <label class="row"><input type="checkbox" aria-label="Вход OAuth в MCP" checked=${!!state?.oauth_enabled} onChange=${event=>run(async()=>{await post('/api/settings/mcp',{oauth_enabled:event.target.checked});return get('/api/mcp');})}/> Вход OAuth в MCP-сервисы</label>
    ${(state?.pending_inputs||[]).map(item=>html`<${McpInputCard} key=${item.id} item=${item} onAnswer=${()=>get('/api/mcp').then(setState,setError)}/>`)}
    ${[['tasks_enabled','Фоновые задачи MCP 2026'],['subscriptions_enabled','Подписки MCP 2026']].map(([key,title])=>html`<label class="row"><input type="checkbox" aria-label=${title} checked=${!!state?.[key]} onChange=${event=>run(async()=>{await post('/api/settings/mcp',{[key]:event.target.checked});return get('/api/mcp');})}/> ${title}</label>`)}
    ${state?.tasks_enabled?html`<${McpTasks} tasks=${state.tasks||[]} onChanged=${setState}/>`:null}
    <div class="card"><h3>Импорт из Claude Code</h3><div class="faint">Выбери .mcp.json или .claude.json. Серверы добавятся выключенными, существующие не заменяются.</div>
      <label><input type="checkbox" checked=${importSecrets} onChange=${(e) => setImportSecrets(e.target.checked)} /> Перенести значения переменных и заголовков в сейф</label>
      <input type="file" accept=".json,application/json" style="max-width:100%" aria-label="Конфигурация MCP" onChange=${(e) => { const file = e.target.files[0]; if (file) run(async () => { const r = await post("/api/mcp/import", { config: JSON.parse(await file.text()), secrets: importSecrets }); setImportNote([`Добавлено: ${r.imported.length}`, ...(r.errors || []), ...(r.warnings || [])].join(" · ")); return r; }); }} />
      ${importNote ? html`<p class="note">${importNote}</p>` : null}
    </div>
    ${form ? html`<div class="card mcp-form"><h3>${form.editing ? "Изменить подключение" : "Новый сервер"}</h3>
      <${Field} label="Короткое имя"><input aria-label="files" class="input" name="mcp-id" disabled=${form.editing} value=${form.id} placeholder="files" onInput=${change("id")} /></${Field}>
      <${Field} label="Название"><input class="input" name="mcp-name" value=${form.name} onInput=${change("name")} /></${Field}>
      <${Field} label="Подключение"><select class="select" name="mcp-transport" value=${form.transport} onChange=${change("transport")}><option value="stdio">Локальная программа (stdio)</option><option value="http">Streamable HTTP</option><option value="sse">Старый HTTP+SSE</option></select></${Field}>
      ${form.transport !== "stdio" ? html`<${Field} label="Адрес MCP"><input aria-label="https://example.org/mcp" class="input mono" name="mcp-url" value=${form.url} placeholder="https://example.org/mcp" onInput=${change("url")} /></${Field}>`
        : html`<${Field} label="Программа"><input aria-label="python3" class="input mono" name="mcp-program" value=${form.program} placeholder="python3" onInput=${change("program")} /></${Field}>
          <${Field} label="Аргументы — по одному на строке"><textarea class="textarea mono" name="mcp-args" value=${form.args} onInput=${change("args")}></textarea></${Field}>
          <${Field} label="Рабочая папка" hint="Пусто — рабочая папка Бендера. В песочнице запись разрешена в этой папке."><input class="input mono" name="mcp-cwd" value=${form.cwd} onInput=${change("cwd")} /></${Field}>
          <label><input type="checkbox" checked=${form.network} onChange=${change("network")} /> Разрешить сеть локальному серверу в песочнице</label>`}
      <details><summary>Переменные и заголовки</summary><div class="faint">Словари JSON. Значения уйдут в сейф с правами 0600; можно указывать ссылки {{secret:имя}}.</div>
        <${Field} label="Переменные окружения"><textarea class="textarea mono" name="mcp-env" value=${form.env} onInput=${change("env")}></textarea></${Field}>
        <${Field} label="HTTP-заголовки"><textarea class="textarea mono" name="mcp-headers" value=${form.headers} onInput=${change("headers")}></textarea></${Field}></details>
      <label class="row"><input type="checkbox" name="mcp-task-support" checked=${!!form.tasks_enabled} onChange=${change('tasks_enabled')}/> Принимать фоновые задачи MCP 2026 от этого сервера</label>
      <details class="mcp-client-options"><summary>Что сервер может попросить у Бендера</summary>
        <p class="faint">Каждый запрос проверяется Предохранителем как недоверенный. Изменение этих прав требует заново просмотреть определения сервера.</p>
        ${[['roots','Показать разрешённые папки'],['sampling','Попросить текст у Быстрой модели'],['elicitation','Показать хозяину форму или ссылку']].map(([key,title])=>html`<label class="row"><input type="checkbox" aria-label=${`MCP ${key}`} checked=${!!form.client_features?.[key]} onChange=${event=>setForm({...form,client_features:{...form.client_features,[key]:event.target.checked}})}/> ${title}</label>`)}
        <${Field} label="Разрешённые папки — абсолютные пути по одному на строке" hint="Только имена и URI, без чтения содержимого. Доступ проверяется по правам вызывающего."><textarea class="textarea mono" name="mcp-roots" value=${form.client_roots} onInput=${change('client_roots')}></textarea></${Field}>
      </details>
      <div class="row"><${Button} kind="primary" busy=${busy} onClick=${save}>Сохранить</${Button}><${Button} onClick=${() => setForm(null)}>Отмена</${Button}></div>
    </div>` : null}
    <${ErrorBox} error=${error} onClose=${() => setError(null)} />
    ${state?.servers?.length === 0 ? html`<p class="faint">Серверов пока нет.</p>` : null}
    ${state?.servers?.map((s) => html`<div class="card mcp-server" data-server=${s.id}>
      <div class="row"><h3 class="grow">${s.name || s.id}</h3><span class="badge">${s.transport}</span><label><input type="checkbox" aria-label=${`Включить ${s.id}`} checked=${s.enabled} disabled=${s.managed_by_workshop} onChange=${(e) => run(() => post("/api/mcp", { id: s.id, enabled: e.target.checked }))} /> Включён</label>${s.managed_by_workshop?html`<a href="#/workshop">Управление в Мастерской</a>`:null}</div>
      <div class="mono faint" style="overflow-wrap:anywhere">${s.transport !== "stdio" ? s.url : (s.command || []).join(" ")}</div>
      <${McpAgents} server=${s} agents=${agents} busy=${busy} onSave=${data => run(() => post("/api/mcp", data))} />
      ${(s.rejected_tools||[]).map(item=>html`<p class="note amber">Тул ${item.name} исключён: ${item.reason}</p>`)}
      ${state.oauth_enabled&&s.transport!=='stdio'?html`<${McpOAuthPanel} server=${s} enabled=${s.enabled&&state.enabled&&!state.safe} onChanged=${setState}/>`:null}
      ${state.subscriptions_enabled&&s.transport==='http'?html`<${McpSubscription} server=${s} tasks=${state.tasks||[]} status=${state.subscriptions?.[s.id]||{}} onChanged=${setState}/>`:null}
      <div class="row"><${Button} small busy=${busy} disabled=${!s.enabled || !state.enabled || state.safe} onClick=${() => run(() => post(`/api/mcp/${s.id}/discover`))}>Обнаружить тулы</${Button}>
        <${Button} small onClick=${() => edit(s)}>Изменить</${Button}><${Button} small onClick=${() => run(() => del(`/api/mcp/${s.id}`))}>Удалить</${Button}></div>
      ${s.tools.map((t) => html`<details class="mcp-tool"><summary>${t.name} · <span class=${`badge ${t.approved ? "green" : "amber"}`}>${t.approved ? "одобрено" : "нужно одобрение"}</span></summary>
        <div class="faint">${t.alias}</div><pre class="mono" style="white-space:pre-wrap;overflow-wrap:anywhere">${JSON.stringify(t.definition, null, 2)}</pre>
        ${t.previous && !t.approved ? html`<h4>Ранее одобрено</h4><pre class="mono" style="white-space:pre-wrap;overflow-wrap:anywhere">${JSON.stringify(t.previous, null, 2)}</pre>` : null}</details>`)}
      ${!s.review && s.tools.some((t) => !t.approved) ? html`<p class="note amber">Подключение или определения изменились после одобрения. Нажми «Обнаружить тулы» — покажу разницу и кнопку «Одобрить».${!s.enabled ? " Сначала включи сервер." : ""}</p>` : null}
      ${s.diagnostics && (s.diagnostics.stderr?.length || s.diagnostics.error) ? html`<details class="mcp-log"><summary>Журнал сервера${s.diagnostics.exit != null ? ` · код выхода ${s.diagnostics.exit}` : ""}${s.diagnostics.error ? " · была ошибка" : ""}</summary>
        ${s.diagnostics.error ? html`<p class="note amber" style="overflow-wrap:anywhere">${s.diagnostics.error}</p>` : null}
        <pre class="mono" style="white-space:pre-wrap;overflow-wrap:anywhere;max-height:320px;overflow:auto">${(s.diagnostics.stderr || []).join("\n") || "(пусто)"}</pre></details>` : null}
      ${s.review && s.tools.some((t) => !t.approved) ? html`<p class="note amber">Просмотри определения: описание тула может содержать чужие инструкции. После одобрения каждый вызов всё равно проверит Предохранитель.</p><${Button} kind="primary" busy=${busy} onClick=${() => run(() => post(`/api/mcp/${s.id}/approve`, { review: s.review }))}>Одобрить просмотренные определения</${Button}>` : null}
      ${state.content_enabled?html`<h4>Ресурсы и шаблоны</h4><${Button} small busy=${busy} disabled=${!s.enabled||!state.enabled||state.safe} onClick=${()=>run(()=>post(`/api/mcp/${s.id}/content/discover`))}>Обнаружить ресурсы и шаблоны</${Button}>
        ${(s.content||[]).map(item=>html`<details class="mcp-content"><summary>${({resources:'Ресурс',templates:'Шаблон URI',prompts:'Шаблон текста'})[item.kind]}: ${item.key} · <span class=${`badge ${item.approved?'green':'amber'}`}>${item.approved?'одобрено':'нужно одобрение'}</span></summary>
          <pre class="mono" style="white-space:pre-wrap;overflow-wrap:anywhere">${JSON.stringify(item.definition,null,2)}</pre>
          ${item.previous&&!item.approved?html`<h4>Ранее одобрено</h4><pre class="mono" style="white-space:pre-wrap;overflow-wrap:anywhere">${JSON.stringify(item.previous,null,2)}</pre>`:null}</details>`)}
        ${s.content_review&&(s.content||[]).some(item=>!item.approved)?html`<${Button} busy=${busy} onClick=${()=>run(()=>post(`/api/mcp/${s.id}/content/approve`,{review:s.content_review}))}>Одобрить каталог ресурсов и шаблонов</${Button}>`:null}`:null}
    </div>`)}
    <div class="faint">Проверка в терминале: <code>python -m bender try mcp</code>. Вызовы одобренных тулов — на экране «Полигон».</div>
  </div></div>`;
}
