// Просмотр пакета, его файлов и карантина; сторонний код не запускается при импорте.
import { html, useEffect, useState } from "../lib/ui.js";
import { get, post } from "../lib/api.js";
import { Button, ErrorBox, Field } from "../lib/ui.js";
import { PendingCards } from "./chat.js";

const ROOTS = { local: "", claude: "~/.claude/skills", hermes: "~/.hermes/skills", openclaw: "~/.openclaw/workspace/skills" };

export function SkillsScreen() {
  const [state, setState] = useState(null);
  const [source, setSource] = useState("local");
  const [folder, setFolder] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [note, setNote] = useState("");
  const [file, setFile] = useState(null);
  const [editText,setEditText]=useState(''),[versions,setVersions]=useState([]);
  const run = async (fn) => { setBusy(true); setError(null); try { const r = await fn(); if (r) setState(r); } catch (e) { setError(e); } setBusy(false); };
  const reload = () => run(() => get("/api/skills"));
  useEffect(() => { reload(); }, []);
  const open = (name, path = "SKILL.md", offset = 0) => run(async () => { const f=await get(`/api/skills/${name}/file?path=${encodeURIComponent(path)}&offset=${offset}`);setFile(f);setEditText(f.text);setVersions((await get(`/api/skills/${name}/history`)).versions); });
  return html`<div class="page skills-page"><div class="page-inner">
    <div class="row"><h2 class="grow">Навыки SKILL.md</h2><${Button} small busy=${busy} onClick=${reload}>Обновить</${Button}></div>
    <p class="note">В контекст идут только названия и описания одобренных навыков. Полный текст и отдельные файлы открываются по необходимости. Чужой пакет сначала просматриваешь ты; изменение файлов вернёт его в карантин.</p>
    <div class="row"><label><input type="checkbox" checked=${!!state?.enabled} onChange=${(e) => { const enabled = e.target.checked; run(async () => { await post("/api/settings/skills", { enabled }); return get("/api/skills"); }); }} /> Навыки включены</label></div>
    ${state?.safe ? html`<p class="note amber">Безопасный режим: навыки и их скрипты выключены.</p>` : null}
    <div class="card"><h3>Импорт папки навыков</h3>
      <${Field} label="Откуда"><select class="select" value=${source} onChange=${(e) => { setSource(e.target.value); setFolder(ROOTS[e.target.value]); }}>${Object.keys(ROOTS).map(s => html`<option value=${s}>${({ local: "Своя папка", claude: "Claude Code", hermes: "Hermes", openclaw: "OpenClaw" })[s]}</option>`)}</select></${Field}>
      <${Field} label="Папка на этой машине" hint="Один навык с SKILL.md или коллекция папок. Источник не меняется, существующие навыки не заменяются."><input class="input mono" name="skills-folder" value=${folder} onInput=${e => setFolder(e.target.value)} /></${Field}>
      <${Button} busy=${busy} disabled=${!folder} onClick=${() => run(async () => { const r = await post("/api/skills/import", { path: folder, source }); setNote([`Импортировано: ${r.imported.length}`, `Уже есть: ${r.skipped.length}`, ...r.errors].join(" · ")); return r; })}>Импортировать в карантин</${Button}>
      ${note ? html`<p class="note">${note}</p>` : null}
    </div>
    <${PendingCards} />
    <${ErrorBox} error=${error} onClose=${() => setError(null)} />
    ${state?.skills?.length === 0 ? html`<p class="faint">Навыков пока нет.</p>` : null}
    ${state?.skills?.map(s => html`<div class="card skill-card" data-skill=${s.id}>
      <div class="row"><h3 class="grow">/${s.id}</h3><span class=${`badge ${s.enabled ? "green" : "amber"}`}>${s.error ? "Ошибка формата" : s.enabled ? "Включён" : s.approved ? "Выключен" : s.previous ? "Изменён — карантин" : "Карантин"}</span></div>
      <p>${s.metadata.description || ""}</p>
      <p class="faint">${s.stats?.pinned?'📌 Закреплён · ':''}${s.stats?.archived?'В архиве · ':s.stats?.stale?'Устарел · ':''}Использован: ${s.stats?.uses||0} · помог: ${s.stats?.helped||0} · с ошибкой: ${s.stats?.failed||0}${s.stats?.last_used?' · '+new Date(s.stats.last_used*1000).toLocaleString('ru'):''}</p>
      ${s.error ? html`<${ErrorBox} error=${s.error} />` : html`<div class="faint">Файлов: ${s.files.length} · скриптов: ${s.scripts.length} · <code>${s.hash.slice(0, 12)}</code></div>`}
      ${s.missing?.length ? html`<p class="note amber">Для скриптов: ${s.missing.join("; ")}</p>` : null}
      <div class="row"><${Button} small disabled=${!!s.error} busy=${busy} onClick=${() => open(s.id)}>Просмотреть</${Button}>
        <${Button} small busy=${busy} onClick=${()=>run(()=>post(`/api/skills/${s.id}/state`,{pinned:!s.stats?.pinned}))}>${s.stats?.pinned?'Открепить':'Закрепить'}</${Button}>
        <${Button} small busy=${busy} onClick=${()=>run(()=>post(`/api/skills/${s.id}/state`,{archived:!s.stats?.archived}))}>${s.stats?.archived?'Вернуть из архива':'В архив'}</${Button}>
        ${s.approved ? html`<${Button} small busy=${busy} onClick=${() => run(() => post(`/api/skills/${s.id}/enabled`, { enabled: !s.enabled }))}>${s.enabled ? "Выключить" : "Включить"}</${Button}>` : null}</div>
      ${file?.name === s.id ? html`<div class="skill-preview">
        <${Field} label="Файл"><select class="select mono" value=${file.path} onChange=${e => open(s.id, e.target.value)}>${s.files.map(f => html`<option value=${f.path}>${f.path} (${f.bytes} байт)</option>`)}</select></${Field}>
        <pre class="mono skill-text">${file.text}</pre>
        ${file.path==='SKILL.md'&&file.next_offset===null?html`<${Field} label="Редактор навыка"><textarea class="input mono" aria-label="Редактор навыка" rows="12" value=${editText} onInput=${e=>setEditText(e.target.value)} /></${Field}>
        <${Button} small disabled=${busy||editText===file.text} onClick=${()=>run(async()=>{const r=await post(`/api/skills/${s.id}/edit`,{text:editText,hash:file.hash});await open(s.id);return r;})}>Сохранить новую версию</${Button}>
        <details><summary>Версии и откат (${versions.length})</summary>${versions.map(v=>html`<div><p>${new Date(v.at*1000).toLocaleString('ru')} · ${v.approved?'была одобрена':'карантин'}</p><pre class="mono skill-text">${v.diff}</pre><${Button} small onClick=${()=>run(async()=>{const r=await post(`/api/skills/${s.id}/rollback`,{version:v.id,hash:file.hash});await open(s.id);return r;})}>Вернуть эту версию</${Button}></div>`)}</details>`:null}
        ${file.next_offset !== null ? html`<${Button} small onClick=${() => open(s.id, file.path, file.next_offset)}>Следующий фрагмент</${Button}>` : null}
        <details><summary>Метаданные и запрошенные условия</summary><pre class="mono skill-text">${JSON.stringify(s.metadata, null, 2)}</pre><p class="faint">allowed-tools и другие поля не дают дополнительных прав.</p></details>
        ${s.warnings.length ? html`<div class="note amber">Сканер заметил: ${s.warnings.join(" · ")}. Это подсказки для просмотра, не гарантия безопасности.</div>` : null}
        ${s.scripts.length ? html`<p class="faint">Скрипты: ${s.scripts.join(", ")}. Запуск — отдельно через Предохранитель; без доступной песочницы программа имеет права пользователя.</p>` : null}
        ${s.previous && !s.approved ? html`<details><summary>Ранее одобренный SKILL.md</summary><pre class="mono skill-text">${s.previous}</pre><div class="faint">Прежние хэши файлов</div><pre class="mono skill-text">${JSON.stringify(s.previous_manifest, null, 2)}</pre></details>` : null}
        ${file.path === "SKILL.md" && !s.error ? html`<${Button} kind="primary" busy=${busy} onClick=${() => run(() => post(`/api/skills/${s.id}/approve`, { hash: file.hash }))}>Одобрить и включить просмотренный пакет</${Button}>` : null}
      </div>` : null}
    </div>`)}
    <p class="faint">Вызов в разговоре: /имя задача или /skill имя задача. Проверки в терминале: <code>python -m bender try skills</code>; чтение и скрипты — также на экране «Полигон».</p>
  </div></div>`;
}
