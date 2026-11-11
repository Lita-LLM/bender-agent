// Настройки: все группы рисуются из схем ядра — новая настройка появляется здесь сама. Плюс тулы и сейф.
import { html, useEffect, useState } from "../lib/ui.js";
import { del, get, post } from "../lib/api.js";
import { Button, ErrorBox, Field, IconButton, Seg, Toggle, Visor, toast } from "../lib/ui.js";
import { SearchTest } from "./search.js";
import {StartupStatus} from '../lib/lifecycle.js';

function FieldEditor({ group, f, onSaved }) {
  const format=value=>f.type==="json"?JSON.stringify(value,null,2):value;
  const [v, setV] = useState(format(f.value));
  useEffect(() => setV(format(f.value)), [f.value]);
  const save = async (value) => {
    try { await post(`/api/settings/${group}`, { [f.key]: value }); onSaved(); } catch (e) { toast(e.message, "error"); setV(format(f.value)); }
  };
  const permissions = {free:'✓ агент меняет сам', ask:'? агент спрашивает при расширении прав или лимитов', owner:'🔒 только хозяин'};
  const title = f.title;
  const hint = [permissions[f.permission || 'free'], f.hint, f.restart ? "Нужен перезапуск." : ""].filter(Boolean).join(" ");
  if (f.type === "bool") return html`<${Toggle} label=${title} hint=${hint} checked=${v} onChange=${(x) => { setV(x); save(x); }} />`;
  if (f.type === "choice") {
    const opts = Object.entries(f.choices || {});
    return html`<${Field} label=${title} hint=${hint}>${opts.length <= 4 && opts.every(([, l]) => l.length < 16)
      ? html`<${Seg} value=${v} options=${opts} onChange=${(x) => { setV(x); save(x); }} />`
      : html`<select class="select" value=${v} onChange=${(e) => { setV(e.target.value); save(e.target.value); }}>${opts.map(([k, l]) => html`<option value=${k}>${l}</option>`)}</select>`}</${Field}>`;
  }
  if(f.type==="text")return html`<${Field} label=${title} hint=${hint}><textarea class="textarea" rows="6" value=${v} onInput=${e=>setV(e.target.value)} onBlur=${()=>{if(v!==format(f.value))save(v);}}/></${Field}>`;
  if(f.type==="json"){
    // Ядро ждёт значение JSON, а не текст: иначе при каждом сохранении текст оборачивался в кавычки заново.
    const saveJson=()=>{if(v===format(f.value))return;let parsed;try{parsed=JSON.parse(v);}catch(e){toast(`«${title}»: это не JSON — ${e.message}`,"error");return;}save(parsed);};
    return html`<${Field} label=${title} hint=${hint}><textarea class="textarea mono" value=${v} onInput=${e=>setV(e.target.value)} onBlur=${saveJson}/></${Field}>`;
  }
  return html`<${Field} label=${title} hint=${hint}><input class="input" type=${f.type === "int" || f.type === "float" ? "number" : "text"} step=${f.type === "float" ? "any" : "1"}
    value=${v} onInput=${(e) => setV(e.target.value)} onBlur=${() => { if (String(v) !== String(f.value)) save(v); }} /></${Field}>`;
}

function Secrets() {
  const [list, setList] = useState([]);
  const [form, setForm] = useState({ name: "", value: "", hosts: "", tools: "", note: "" });
  const load = () => get("/api/secrets").then((r) => setList(r.secrets));
  useEffect(() => { load(); }, []);
  const add = async () => { try { await post("/api/secrets", form); setForm({ name: "", value: "", hosts: "", tools: "", note: "" }); load(); } catch (e) { toast(e.message, "error"); } };
  return html`<div class="card"><h3>Сейф</h3>
    <div class="note">Ключи и токены. Агент их не видит: пишет <code>{{secret:имя}}</code>, значение подставляет Предохранитель — и только туда, где этому секрету разрешено. Хранится файлом с правами 600, без шифрования.</div>
    <div class="list">${list.map((s) => html`<div class="list-item"><div class="li-main"><div class="li-title mono">${s.name}</div>
      <div class="li-sub">${s.masked}${s.note ? ` · ${s.note}` : ""}${s.allow?.hosts?.length ? ` · хосты: ${s.allow.hosts.join(", ")}` : ""}${s.allow?.tools?.length ? ` · тулы: ${s.allow.tools.join(", ")}` : ""}</div></div>
      ${s.name.startsWith("provider:") ? null : html`<${IconButton} small icon="trash" danger title="Удалить" onClick=${async () => { await del(`/api/secrets/${encodeURIComponent(s.name)}`); load(); }} />`}</div>`)}</div>
    <div class="row" style="margin-top:10px"><input aria-label="имя" class="input mono" style="width:160px" placeholder="имя" value=${form.name} onInput=${(e) => setForm({ ...form, name: e.target.value })} />
      <input aria-label="значение" class="input mono grow" type="password" placeholder="значение" value=${form.value} onInput=${(e) => setForm({ ...form, value: e.target.value })} /></div>
    <div class="row" style="margin-top:6px"><input aria-label="хосты через запятую (api.github.com)" class="input grow" placeholder="хосты через запятую (api.github.com)" value=${form.hosts} onInput=${(e) => setForm({ ...form, hosts: e.target.value })} />
      <input aria-label="тулы (run)" class="input" style="width:200px" placeholder="тулы (run)" value=${form.tools} onInput=${(e) => setForm({ ...form, tools: e.target.value })} />
      <${Button} kind="primary" small disabled=${!form.name || !form.value} onClick=${add}>Сохранить</${Button}></div>
  </div>`;
}

function Tools() {
  const [tools, setTools] = useState([]);
  useEffect(() => { get("/api/tools").then((r) => setTools(r.tools)); }, []);
  const toggle = async (name, enabled) => { const r = await post(`/api/tools/${name}`, { enabled }); setTools(r.tools); };
  return html`<div class="card"><h3>Тулы</h3><div class="faint" style="margin-bottom:8px">Выключенный тул агент не видит вовсе.</div>
    ${tools.map((t) => html`<${Toggle} label=${`${t.title} — ${t.name}`} hint=${t.description} checked=${t.enabled} onChange=${(v) => toggle(t.name, v)} />`)}</div>`;
}

export function SettingsScreen() {
  const [groups, setGroups] = useState(null);
  const [err, setErr] = useState(null);
  const load = () => get("/api/settings").then((r) => setGroups(r.groups), setErr);
  useEffect(() => { load(); }, []);
  if (!groups) return html`<div class="page"><div class="page-inner"><${ErrorBox} error=${err} /><${Visor} mode="busy" /></div></div>`;
  return html`<div class="page"><div class="page-inner">
    <${StartupStatus}/>
    <div class="note">Всё настраивается и всё отключается. То же самое можно попросить у агента словами — изменения, ослабляющие защиту, он отправит тебе на подтверждение.</div>
    ${groups.filter((g) => g.name !== "guard").map((g) => html`<div class="card"><h3>${g.title}</h3>${g.hint ? html`<div class="faint" style="margin:-6px 0 10px">${g.hint}</div>` : null}
      ${g.fields.map((f) => html`<${FieldEditor} key=${f.key} group=${g.name} f=${f} onSaved=${load} />`)}
      ${g.name === "search" ? html`<${SearchTest} />` : null}</div>`)}
    <p class="note">Роли моделей: ✓ перестановка подключённых моделей с причиной; ? платный ключ вместо подписки/локальной модели и удаление запасных. <a href="#/models">Открыть модели</a>.</p>
    <${Tools} />
    <${Secrets} />
  </div></div>`;
}
