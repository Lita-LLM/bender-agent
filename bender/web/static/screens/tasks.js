// Задачи: субагенты — кто над чем работает, сколько стоит, что вернул. Дерево: задачи задач — вложенно.
import { html, useEffect, useState } from "../lib/ui.js";
import { del, get, post } from "../lib/api.js";
import { mdToHtml } from "../lib/md.js";
import { useEvents } from "../lib/store.js";
import { Button, ErrorBox, Field, Icon, Modal, Visor, money, timeAgo, toast } from "../lib/ui.js";

const ST = { queued: ["", "в очереди"], running: ["amber", "работает"], done: ["green", "готово"], error: ["red", "ошибка"], cancelled: ["", "отменено"] };

function dur(t) {
  if (!t.started) return "";
  const s = Math.round((t.finished || Date.now() / 1000) - t.started);
  return s < 60 ? `${s} с` : `${Math.floor(s / 60)} мин ${s % 60} с`;
}

function Detail({ id, onClose }) {
  const [d, setD] = useState(null);
  const load=()=>get(`/api/tasks/${id}`).then(setD,(e)=>toast(e.message,'error'));
  useEffect(() => { load(); }, [id]);
  useEvents(ev=>{if(ev.type==='task'&&ev.task?.id===id||ev.type==='task_step'&&ev.task_id===id)load();},[id]);
  if (!d) return html`<${Modal} title="Задача" onClose=${onClose}><${Visor} mode="busy" /></${Modal}>`;
  const t = d.task;
  return html`<${Modal} title=${`#${t.id.slice(-6)} · ${t.goal.slice(0, 60)}`} onClose=${onClose} wide>
    <div class="faint small">${t.role} · модель: ${t.model_role} · шагов ${t.steps} · ${money(t.cost)}${t.tainted ? " · начата заражённой" : ""}${t.result_tainted ? " · итог недоверенный" : ""}</div>
    ${t.context ? html`<details><summary>Контекст</summary><pre class="block">${t.context}</pre></details>` : null}
    ${t.schema ? html`<details><summary>Схема ответа ${t.schema_valid === 1 ? "✓" : t.schema_valid === 0 ? "✗ не совпал" : ""}</summary><pre class="block">${JSON.stringify(t.schema, null, 1)}</pre></details>` : null}
    ${t.error ? html`<div class="note red">${t.error}</div>` : null}
    ${t.worker_agent&&['done','error','cancelled'].includes(t.status)?html`<p>${t.kept_agent?html`<a href=${'#/rights?agent='+t.kept_agent}>Агент оставлен: ${t.kept_agent}</a>`:html`<${Button} onClick=${async()=>{try{await post('/api/tasks/'+t.id+'/keep',{});setD(await get('/api/tasks/'+t.id));toast('Агент оставлен со своими последними правами');}catch(e){toast(e.message,'error');}}}>Оставить агента</${Button}>`}</p>`:null}
    <details><summary>Подробности: шаги и ответы провайдеров</summary><pre class="block">${JSON.stringify(d.timeline || [], null, 1)}</pre></details>
    <h3 style="margin:12px 0 6px">Итог</h3>
    ${t.data ? html`<pre class="block">${JSON.stringify(t.data, null, 1)}</pre>` : html`<div class="m-text" dangerouslySetInnerHTML=${{ __html: mdToHtml(t.result || "—") }}></div>`}
    <details style="margin-top:10px"><summary>Ход субагента (${d.messages.length})</summary>
      ${d.messages.map((m) => html`<div class="small" style="margin:6px 0"><b>${{ user: "Задание", assistant: "Субагент", tool: `тул ${m.name || ""}`, event: "·" }[m.role] || m.role}:</b> ${(typeof m.content === "string" ? m.content : JSON.stringify(m.content)).slice(0, 600)}${m.tool_calls ? ` ⚙ ${m.tool_calls.map((c) => c.name).join(", ")}` : ""}</div>`)}</details>
  </${Modal}>`;
}

function Roles({data, reload}) {
  const [draft, setDraft] = useState(null);
  const [id, setId] = useState("");
  const [schema, setSchema] = useState("");
  const [existing, setExisting] = useState(false);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const edit = (name, role) => { setId(name); setDraft({...role}); setSchema(role.schema ? JSON.stringify(role.schema, null, 2) : ""); setExisting(!!name); setError(null); };
  const run = async fn => { setBusy(true); setError(null); try { await fn(); reload(); } catch(e) { setError(e); } setBusy(false); };
  return html`<details class="team-roles" style="margin-top:12px"><summary>Роли</summary>
    <p class="note">Хозяин задаёт промпт, способности, модель и схему ответа. Задача получает пересечение прав роли и родителя. Вложенное делегирование — team.spawn, не глубже 2; у «Помощника» оно уже есть. Предложение агента через settings требует карточки хозяина.</p>
    ${Object.entries(data.roles).map(([name,r]) => html`<div class="list-item role-row"><div class="li-main"><b>${r.title}</b> <code>${name}</code> ${r.builtin ? html`<span class="badge">встроенная</span>` : null} ${!r.enabled ? html`<span class="badge">выключена</span>` : null}<div class="li-sub">${r.model} · ${(r.capabilities || []).join(", ") || "без способностей"}</div></div>
      <${Button} small onClick=${() => edit(name,r)}>Изменить</${Button}>
      <${Button} small onClick=${() => run(() => post(`/api/team/roles/${name}`, {enabled:!r.enabled}))}>${r.enabled ? "Выключить" : "Включить"}</${Button}>
      ${!r.builtin ? html`<${Button} small kind="danger" onClick=${() => { if(confirm(`Удалить роль ${name}?`)) run(() => del(`/api/team/roles/${name}`)); }}>Удалить</${Button}>` : null}</div>`)}
    <${Button} small onClick=${() => edit("",{title:"",prompt:"",capabilities:[],model:"fast",enabled:true})}>Создать роль</${Button}>
    ${draft ? html`<div class="card role-editor">
      <${Field} label="Имя роли"><input class="input mono" aria-label="Имя роли" value=${id} disabled=${existing} onInput=${e => setId(e.target.value)} /></${Field}>
      <${Field} label="Название роли"><input class="input" aria-label="Название роли" value=${draft.title} onInput=${e => setDraft({...draft,title:e.target.value})} /></${Field}>
      <${Field} label="Промпт роли"><textarea class="textarea" aria-label="Промпт роли" value=${draft.prompt} onInput=${e => setDraft({...draft,prompt:e.target.value})} /></${Field}>
      <${Field} label="Роль модели"><select class="select" aria-label="Роль модели" value=${draft.model} onChange=${e=>setDraft({...draft,model:e.target.value})}>${data.model_roles.map(m=>html`<option value=${m}>${m} · ${data.model_labels?.[m] || "не выбрана"}</option>`)}</select></${Field}>
      <fieldset><legend>Способности роли</legend>${Object.entries(data.capabilities).map(([cap,title])=>html`<label style="display:block;margin:5px"><input type="checkbox" aria-label=${cap} checked=${draft.capabilities.includes(cap)} onChange=${e=>setDraft({...draft,capabilities:e.target.checked ? [...draft.capabilities,cap] : draft.capabilities.filter(c=>c!==cap)})} /> ${title} <code>${cap}</code></label>`)}</fieldset>
      <label><input type="checkbox" checked=${!!draft.read_parent_memory} onChange=${e=>setDraft({...draft,read_parent_memory:e.target.checked})}/> Читать память родителя (только чтение)</label>
      <p class="faint">Свои роли получают стандартные тулы по выбранным способностям; встроенные сохраняют набор шаблона. Команда exec даёт программе права процесса, ограниченные доступной песочницей ОС.</p>
      <${Field} label="Схема ответа по умолчанию (JSON)"><textarea class="textarea mono" aria-label="Схема ответа роли" value=${schema} onInput=${e=>setSchema(e.target.value)} /></${Field}>
      <div class="row"><${Button} kind="primary" busy=${busy} disabled=${!id} onClick=${() => run(async()=>{await post(`/api/team/roles/${encodeURIComponent(id)}`,{title:draft.title,prompt:draft.prompt,model:draft.model,capabilities:draft.capabilities,schema:schema.trim()?JSON.parse(schema):null,enabled:draft.enabled,read_parent_memory:!!draft.read_parent_memory});setDraft(null);})}>Сохранить роль</${Button}><${Button} onClick=${()=>setDraft(null)}>Отмена</${Button}></div>
    </div>` : null}
    <${ErrorBox} error=${error} />
    <p class="faint">Полигон → spawn_task/wait_tasks; терминал: <code>try roles</code>.</p>
  </details>`;
}

export function TasksScreen() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  const [open, setOpen] = useState(null);
  const load = () => get("/api/tasks").then(setD, setErr);
  useEffect(() => { load(); }, []);
  useEvents((ev) => { if (["task", "task_roles"].includes(ev.type)) load(); }, []);
  if (!d) return html`<div class="page"><div class="page-inner"><${ErrorBox} error=${err} /><${Visor} mode="busy" /></div></div>`;
  const kids = (pid) => d.tasks.filter((t) => t.parent_task === pid);
  const tops = d.tasks.filter((t) => !t.parent_task || !d.tasks.find((x) => x.id === t.parent_task));
  const running = d.tasks.filter((t) => t.status === "running").length;
  const Row = ({ t, depth }) => {
    const [cls, word] = ST[t.status] || ["", t.status];
    return html`<div style=${`margin-left:${depth * 18}px`}>
      <div class="list-item" role="button" tabindex="0" aria-label=${t.goal} onKeyDown=${e=>{if(e.target===e.currentTarget&&["Enter"," "].includes(e.key)){e.preventDefault();setOpen(t.id);}}} style="cursor:pointer" onClick=${() => setOpen(t.id)}>
        <${Visor} role=${t.role} mode=${t.status==="running"?"thinking":t.status==="error"?"error":"idle"}/><span class=${`badge ${cls}`}>${word}</span>
        <div class="li-main"><div class="li-title">${t.goal}</div>
          <div class="li-sub">${t.role_title} · ${t.model_role} · ${timeAgo(t.created)}${dur(t) ? " · " + dur(t) : ""}${t.cost ? " · " + money(t.cost) : ""}${t.result_tainted ? " · ⚠ недоверенное" : ""}${t.schema_valid === 0 ? " · схема ✗" : ""}</div></div>
        ${["queued", "running"].includes(t.status) ? html`<${Button} small kind="ghost" onClick=${async (e) => { e.stopPropagation(); await post(`/api/tasks/${t.id}/cancel`); load(); }}>Отменить</${Button}>` : null}
      </div>
      ${kids(t.id).map((c) => html`<${Row} t=${c} depth=${depth + 1} />`)}
    </div>`;
  };
  return html`<div class="page"><div class="page-inner">
    <div class="card"><div class="card-title"><${Icon} name="users" /><h3>Команда</h3></div>
      <div class="faint">Агент поручает работу субагентам: у каждого своя модель, урезанные руки и бюджет. Они знают только то, что им передали. Сразу работают до ${d.parallel}; ${running ? `сейчас — ${running}` : "сейчас никто"}.</div>
      <${Roles} data=${d} reload=${load} />
    </div>
    ${!d.tasks.length ? html`<div class="note">Задач ещё не было. Попроси Бендера сделать что-то большое — «найди и сравни три статьи про …» — он сам позовёт помощников.</div>` : null}
    <div class="list">${tops.map((t) => html`<${Row} t=${t} depth=${0} />`)}</div>
    ${open ? html`<${Detail} id=${open} onClose=${() => setOpen(null)} />` : null}
  </div></div>`;
}
