// Общие компоненты пульта.
import { html, render, useEffect, useRef, useState, cloneElement } from "../vendor/ui.js";

const PATHS = {
  menu: "M4 6h16M4 12h16M4 18h16",
  back: "M15 18l-6-6 6-6",
  forward: "M9 18l6-6-6-6",
  send: "M5 12h13M13 6l6 6-6 6",
  stop: "M7 7h10v10H7z",
  plus: "M12 5v14M5 12h14",
  x: "M6 6l12 12M18 6L6 18",
  check: "M5 12.5l4.5 4.5L19 7.5",
  trash: "M4 7h16M9 7V4.5h6V7M6.5 7l1 13h9l1-13",
  "trash-one": "M4 7h16M9 7V4.5h6V7M6.5 7l1 13h9l1-13M11 12.5l1.5-1.5v6",
  edit: "M4 20h4L19 9l-4-4L4 16v4zM13.5 6.5l4 4",
  copy: "M9 9h11v11H9zM5 15H4V4h11v1",
  refresh: "M20 11a8 8 0 10-2.3 5.7M20 4v7h-7",
  branch: "M6 3v12M18 9a3 3 0 100-6 3 3 0 000 6zM6 21a3 3 0 100-6 3 3 0 000 6zM18 9a9 9 0 01-9 9",
  eye: "M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7S2 12 2 12zM12 15a3 3 0 100-6 3 3 0 000 6z",
  eyeOff: "M3 3l18 18M10.6 5.1A10 10 0 0112 5c6.4 0 10 7 10 7a17 17 0 01-3.2 4.1M6.6 6.6C3.8 8.4 2 12 2 12s3.6 7 10 7a9.7 9.7 0 005.4-1.6M9.9 9.9a3 3 0 004.2 4.2",
  lock: "M6 11h12v10H6zM8.5 11V7.5a3.5 3.5 0 017 0V11",
  unlock: "M6 11h12v10H6zM8.5 11V7.5a3.5 3.5 0 016.8-1.2",
  settings: "M12 15a3 3 0 100-6 3 3 0 000 6zM19.4 15a1.7 1.7 0 00.3 1.8l.1.1a2 2 0 11-2.8 2.8l-.1-.1a1.7 1.7 0 00-1.8-.3 1.7 1.7 0 00-1 1.5V21a2 2 0 11-4 0v-.1a1.7 1.7 0 00-1.1-1.5 1.7 1.7 0 00-1.8.3l-.1.1a2 2 0 11-2.8-2.8l.1-.1a1.7 1.7 0 00.3-1.8 1.7 1.7 0 00-1.5-1H3a2 2 0 110-4h.1a1.7 1.7 0 001.5-1.1 1.7 1.7 0 00-.3-1.8l-.1-.1a2 2 0 112.8-2.8l.1.1a1.7 1.7 0 001.8.3H9a1.7 1.7 0 001-1.5V3a2 2 0 114 0v.1a1.7 1.7 0 001 1.5 1.7 1.7 0 001.8-.3l.1-.1a2 2 0 112.8 2.8l-.1.1a1.7 1.7 0 00-.3 1.8V9a1.7 1.7 0 001.5 1H21a2 2 0 110 4h-.1a1.7 1.7 0 00-1.5 1z",
  users: "M16 21v-2a4 4 0 00-4-4H6a4 4 0 00-4 4v2M9 11a4 4 0 100-8 4 4 0 000 8zM22 21v-2a4 4 0 00-3-3.9M16 3.1a4 4 0 010 7.8",
  user: "M20 21v-2a4 4 0 00-4-4H8a4 4 0 00-4 4v2M12 11a4 4 0 100-8 4 4 0 000 8z",
  book: "M4 19.5A2.5 2.5 0 016.5 17H20V3H6.5A2.5 2.5 0 004 5.5v14zM4 19.5A2.5 2.5 0 006.5 22H20v-5",
  cpu: "M9 3v3M15 3v3M9 18v3M15 18v3M3 9h3M3 15h3M18 9h3M18 15h3M6 6h12v12H6zM10 10h4v4h-4z",
  upload: "M12 16V4M7 9l5-5 5 5M4 20h16",
  download: "M12 4v12M7 11l5 5 5-5M4 20h16",
  info: "M12 22a10 10 0 100-20 10 10 0 000 20zM12 16v-4M12 8h.01",
  alert: "M12 3l10 18H2L12 3zM12 10v4M12 17.5h.01",
  more: "M12 13a1 1 0 100-2 1 1 0 000 2zM19 13a1 1 0 100-2 1 1 0 000 2zM5 13a1 1 0 100-2 1 1 0 000 2z",
  chat: "M21 12a8 8 0 01-11.8 7L3 21l2-6.2A8 8 0 1121 12z",
  sparkles: "M12 3l1.8 4.7L18.5 9.5l-4.7 1.8L12 16l-1.8-4.7L5.5 9.5l4.7-1.8L12 3zM19 15l.9 2.1L22 18l-2.1.9L19 21l-.9-2.1L16 18l2.1-.9L19 15z",
  layers: "M12 2l10 5-10 5L2 7l10-5zM2 17l10 5 10-5M2 12l10 5 10-5",
  history: "M3 12a9 9 0 109-9 9.7 9.7 0 00-6.7 2.8L3 8M3 3v5h5M12 7v5l3 3",
  file: "M14 2H6a2 2 0 00-2 2v16a2 2 0 002 2h12a2 2 0 002-2V8l-6-6zM14 2v6h6",
  continue: "M4 12h10M10 6l6 6-6 6M20 5v14",
  question: "M12 22a10 10 0 100-20 10 10 0 000 20zM9.1 9a3 3 0 015.8 1c0 2-3 3-3 3M12 17h.01",
  home: "M3 10.5L12 3l9 7.5V21H3V10.5zM9 21v-6h6v6",
  drafts: "M12 20h9M16.5 3.5a2.1 2.1 0 013 3L7 19l-4 1 1-4L16.5 3.5z",
  sun: "M12 17a5 5 0 100-10 5 5 0 000 10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4",
  compare: "M3 4h7v16H3zM14 4h7v16h-7z",
  search: "M11 19a8 8 0 100-16 8 8 0 000 16zM21 21l-4.3-4.3",
  paperclip: "M21 10.5l-8.6 8.6a5 5 0 01-7-7l8.6-8.6a3.3 3.3 0 014.7 4.7l-8.6 8.6a1.7 1.7 0 01-2.4-2.4l7.9-7.9",
  shield: "M12 3l8 3v6c0 5-3.4 8.3-8 9-4.6-.7-8-4-8-9V6l8-3z",
  heart: "M20.8 4.6a5.5 5.5 0 00-7.8 0L12 5.7l-1-1.1a5.5 5.5 0 00-7.8 7.8L12 21l8.8-8.6a5.5 5.5 0 000-7.8z",
  flask: "M9 3h6M10 3v6L4.5 18.5A1.7 1.7 0 006 21h12a1.7 1.7 0 001.5-2.5L14 9V3M7 14h10",
  list: "M8 6h13M8 12h13M8 18h13M3 6h.01M3 12h.01M3 18h.01",
  power: "M12 2v10M18.4 6.6a9 9 0 11-12.8 0",
  key: "M15 7a4 4 0 11-3.5 6L5 19.5V22H2v-3h2v-2h2l3.5-3.5A4 4 0 0115 7zM16.5 8.5h.01",
  robot: "M12 2v3M5 8h14a2 2 0 012 2v8a2 2 0 01-2 2H5a2 2 0 01-2-2v-10a2 2 0 012-2zM9 13h.01M15 13h.01M9 17h6",
  play: "M6 4l14 8-14 8V4z",
  memory: "M4 6h16v12H4zM8 6V3M12 6V3M16 6V3M8 21v-3M12 21v-3M16 21v-3",
  move: "M5 12h14M13 6l6 6-6 6M5 6v12",
  moon: "M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z",
  activity: "M22 12h-4l-3 9L9 3l-3 9H2",
  mail: "M3 6h18v12H3zM3 7l9 6 9-6",
  link: "M10 14a4 4 0 0 0 5.7 0l3-3a4 4 0 0 0-5.7-5.7l-1 1M14 10a4 4 0 0 0-5.7 0l-3 3a4 4 0 0 0 5.7 5.7l1-1",
};

export function Icon({ name, title }) {
  const d = PATHS[name] || PATHS.info;
  return html`<svg class="icon" viewBox="0 0 24 24" aria-hidden=${title ? "false" : "true"}>${title ? html`<title>${title}</title>` : null}<path d=${d} /></svg>`;
}

export function IconButton({ icon, title, onClick, small, danger, disabled }) {
  return html`<button type="button" class=${`icon-btn${small ? " small" : ""}${danger ? " danger" : ""}`} title=${title} aria-label=${title} onClick=${onClick} disabled=${disabled}><${Icon} name=${icon} /></button>`;
}

export function Visor({ mode = "idle", avatar = {}, role = "" }) {
  mode = ({"":"idle",busy:"thinking",alarm:"halted"})[mode] || mode;
  const words={idle:"Простой",listening:"Слушает",thinking:"Думает",tool:"Работает тулом",approval:"Ждёт одобрения",sleep:"Спит",error:"Ошибка",safe:"Безопасный режим",halted:"Стоп-кран",joy:"Радуется",doubt:"Сомневается",smirk:"Ехидничает",surprise:"Удивлён",sad:"Грустит"};
  const hash=[...role].reduce((n,c)=>n+c.charCodeAt(0),0);
  const color=role?['#ffb840','#7fb0f5','#63c991','#cd9bff'][hash%4]:(/^#[0-9a-f]{6}$/i.test(avatar.color||'')?avatar.color:'#ffb840');
  const shape=avatar.shape||['visor','round','square'][hash%3], eyes=avatar.eyes||'round';
  return html`<svg class="face-visor" data-mode=${mode} data-shape=${shape} data-eyes=${eyes} viewBox="0 0 64 40" role="img" aria-label=${(role?role+': ':'')+(words[mode]||words.idle)} style=${`--glow:${color}`}>
    <rect class="face-shell" x="1" y="1" width="62" height="38" rx=${shape==='round'?19:shape==='square'?3:12}/>
    <path class="face-grille" d="M19 32h26M24 35h16"/>
    <g class="face-eyes"><rect x="13" y="12" width="13" height="11" rx=${eyes==='square'?1:6}/><rect x="38" y="12" width="13" height="11" rx=${eyes==='square'?1:6}/></g>
  </svg>`;
}

export function Button({ kind = "", onClick, disabled, busy, children, title, icon, small, block }) {
  const cls = ["btn", kind, small ? "small" : "", block ? "block" : ""].filter(Boolean).join(" ");
  return html`<button type="button" class=${cls} onClick=${onClick} disabled=${disabled || busy} title=${title} aria-label=${title||(!children&&icon?({send:"Отправить",stop:"Остановить",plus:"Добавить",x:"Закрыть"}[icon]||"Действие"):undefined)}>
    ${busy ? html`<${Visor} mode="busy" />` : icon ? html`<${Icon} name=${icon} />` : null}${children}</button>`;
}

let fieldNumber=0;
export function Field({ label, hint, children }) {
  const ident=useRef(null);if(!ident.current)ident.current='field-'+(++fieldNumber);
  let number=0,firstId=null;
  const suffix={imap_host:'сервер IMAP',imap_port:'порт IMAP',imap_security:'защита IMAP',smtp_host:'сервер SMTP',smtp_port:'порт SMTP',smtp_security:'защита SMTP'};
  const decorate=node=>{
    if(Array.isArray(node))return node.map(decorate);
    if(!node||typeof node!=='object'||!node.props)return node;
    const props={};
    if(['input','textarea','select'].includes(node.type)){
      number++;props.id=node.props.id||ident.current+(number===1?'':'-'+number);if(!firstId)firstId=props.id;
      if(!node.props['aria-label']&&!node.props['aria-labelledby'])props['aria-label']=suffix[node.props.name]||label||node.props.placeholder;
    }
    if(node.props.children)props.children=decorate(node.props.children);
    return cloneElement(node,props);
  };
  const content=decorate(children);
  return html`<div class="field">${label?html`<label for=${firstId||ident.current}>${label}</label>`:null}${content}${hint?html`<div class="hint">${hint}</div>`:null}</div>`;
}

export function Toggle({ label, title, hint, checked, onChange, disabled }) {
  return html`<label class="toggle"><input type="checkbox" aria-label=${label||title} checked=${!!checked} disabled=${disabled} onChange=${(e) => onChange(e.target.checked)} />
    <span class="track"></span><span><span class="t-label">${label}</span>${hint ? html`<span class="hint">${hint}</span>` : null}</span></label>`;
}

export function Seg({ value, options, onChange }) {
  return html`<div class="seg" role="group">${options.map(([v, label]) => html`<button type="button" class=${value === v ? "on" : ""} aria-pressed=${value===v} onClick=${() => onChange(v)}>${label}</button>`)}</div>`;
}

export function AutoTextarea({ value, onInput, placeholder, minRows = 1, className = "textarea", onKeyDown, inputRef, autofocus, id, "aria-label":ariaLabel }) {
  const ref = useRef(null);
  const el = inputRef || ref;
  useEffect(() => { const t = el.current; if (!t) return; t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight + 2, window.innerHeight * 0.4) + "px"; }, [value]);
  useEffect(() => { if (autofocus && el.current) el.current.focus(); }, []);
  return html`<textarea ref=${el} id=${id} aria-label=${ariaLabel||placeholder||"Сообщение"} class=${className} rows=${minRows} value=${value ?? ""} placeholder=${placeholder} onInput=${(e) => onInput(e.target.value)} onKeyDown=${onKeyDown}></textarea>`;
}

export function Modal({ title, onClose, children, wide }) {
  const ref=useRef(null);
  useEffect(()=>{const previous=document.activeElement;const first=ref.current?.querySelector('button,input,textarea,select,a[href]');(first||ref.current)?.focus();return()=>{if(previous?.isConnected)previous.focus();};},[]);
  const key=e=>{
    if(e.target.closest('[role="dialog"]')!==ref.current)return;
    if(e.key==='Escape'){e.stopPropagation();e.preventDefault();onClose?.();}
    if(e.key==='Tab'){
      const nodes=[...ref.current.querySelectorAll('button:not(:disabled),input:not(:disabled),textarea:not(:disabled),select:not(:disabled),a[href],[tabindex="0"]')].filter(n=>n.getClientRects().length);
      const first=nodes[0],last=nodes[nodes.length-1];
      if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus();}
      else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus();}
    }
  };
  return html`<div class="modal-wrap" onMouseDown=${e=>{if(e.target===e.currentTarget)onClose?.();}}>
    <div ref=${ref} tabindex="-1" onKeyDown=${key} class=${`modal${wide?' wide':''}`} role="dialog" aria-modal="true" aria-label=${title}>
      <div class="modal-head"><h2>${title}</h2>${onClose?html`<${IconButton} icon="x" title="Закрыть" onClick=${onClose}/>`:null}</div>
      <div class="modal-body">${children}</div></div></div>`;
}

export function copyText(text) {
  if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text).then(() => true, () => fallbackCopy(text));
  return Promise.resolve(fallbackCopy(text));
}
function fallbackCopy(text) {
  const t = document.createElement("textarea"); t.value = text; t.style.position = "fixed"; t.style.opacity = "0";
  document.body.appendChild(t); t.select(); let ok = false; try { ok = document.execCommand("copy"); } catch { ok = false; } t.remove(); return ok;
}

export function ErrorBox({ error, onClose }) {
  if (!error) return null;
  const raw=error.error||error;
  const e=raw.title?raw:{title:"Действие не выполнено",hint:"Повтори попытку; если не помогло, открой подробности.",detail:raw.message||String(raw)};
  return html`<div class="error-box" role="alert">
    <div class="e-title"><${Icon} name="alert" /><span class="grow">${e.title}</span>${onClose ? html`<${IconButton} small icon="x" title="Скрыть" onClick=${onClose} />` : null}</div>
    ${e.hint ? html`<div class="e-hint">${e.hint}</div>` : null}
    ${e.detail ? html`<details><summary>Подробности</summary><pre>${e.detail}</pre><${Button} small icon="copy" onClick=${() => copyText(`${e.title}\n${e.hint || ""}\n\n${e.detail}`).then(() => toast("Скопировано", "ok"))}>Скопировать</${Button}></details>` : null}
  </div>`;
}

// тосты
let toastHost = null;
export function toast(text, kind = "") {
  if (!toastHost) { toastHost = document.createElement("div"); toastHost.className = "toasts"; toastHost.setAttribute("role","status");toastHost.setAttribute("aria-live","polite"); document.body.appendChild(toastHost); }
  const el = document.createElement("div"); el.className = `toast ${kind}`; el.textContent = text; toastHost.appendChild(el);
  setTimeout(() => el.remove(), kind === "error" ? 6000 : 3000);
}

export function Tabs({ value, tabs, onChange }) {
  return html`<div class="seg" role="tablist">${tabs.map(([v, label]) => html`<button type="button" class=${value === v ? "on" : ""} aria-pressed=${value===v} onClick=${() => onChange(v)}>${label}</button>`)}</div>`;
}

export function timeAgo(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "только что";
  if (s < 3600) return `${Math.floor(s / 60)} мин назад`;
  if (s < 86400) return `${Math.floor(s / 3600)} ч назад`;
  return new Date(ts * 1000).toLocaleDateString("ru-RU");
}
export function timeOf(ts) { return new Date(ts * 1000).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }); }
export function money(x) { return x ? `$${x < 0.01 ? x.toFixed(5) : x.toFixed(3)}` : "$0"; }
export { html, render, useEffect, useRef, useState };
