// Пакет → просмотр → одобрение → отдельный MCP. Никаких import() стороннего кода в пульт.
import { html, useEffect, useState } from "../lib/ui.js";
import { del, get, post } from "../lib/api.js";
import { Button, ErrorBox, Field } from "../lib/ui.js";
import { PendingCards } from "./chat.js";

export function ModulesScreen() {
  const [state, setState] = useState(null);
  const [servers, setServers] = useState([]);
  const [source, setSource] = useState("");
  const [git, setGit] = useState(false);
  const [ref, setRef] = useState("");
  const [subdir, setSubdir] = useState("");
  const [file, setFile] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [note, setNote] = useState("");
  const reload = async () => { const [s, m] = await Promise.all([get("/api/modules"), get("/api/mcp")]); setState(s); setServers(m.servers); };
  const run = async fn => { setBusy(true); setError(null); try { await fn(); await reload(); } catch (e) { setError(e); } setBusy(false); };
  useEffect(() => { run(async () => {}); }, []);
  const open = (name, candidate = false, path = "bender-module.json", offset = 0) => run(async () => {
    setFile({ ...await get(`/api/modules/${name}/file?candidate=${candidate ? 1 : 0}&path=${encodeURIComponent(path)}&offset=${offset}`), candidate });
  });
  return html`<div class="page modules-page"><div class="page-inner">
    <div class="row"><h2 class="grow">Модули</h2><${Button} small busy=${busy} onClick=${() => run(async () => {})}>Обновить</${Button}></div>
    <p class="note">Код модуля работает отдельным MCP-процессом через Предохранитель. Пакет и определения тулов одобряются отдельно. Страницы изолированы: скриптов и доступа к пульту у них нет. Без доступной защиты ОС программа имеет права пользователя — это видно при запуске.</p>
    <label><input type="checkbox" checked=${!!state?.enabled} onChange=${e => run(() => post("/api/settings/modules", { enabled: e.target.checked }))} /> Модули включены</label>
    <div class="card"><h3>Установить или обновить пакет</h3>
      <label><input type="checkbox" checked=${git} onChange=${e => setGit(e.target.checked)} /> Из git по HTTPS</label>
      <${Field} label=${git ? "Адрес репозитория" : "Папка с bender-module.json"}><input class="input mono" aria-label="Источник модуля" value=${source} onInput=${e => setSource(e.target.value)} /></${Field}>
      ${git ? html`<details><summary>Ветка и подпапка</summary><${Field} label="Ветка (необязательно)"><input class="input mono" value=${ref} onInput=${e => setRef(e.target.value)} /></${Field}><${Field} label="Подпапка (необязательно)"><input class="input mono" value=${subdir} onInput=${e => setSubdir(e.target.value)} /></${Field}></details>` : null}
      <${Button} busy=${busy} disabled=${!source} onClick=${() => run(async () => { const r = await post("/api/modules/import", git ? { git: source, ref, subdir } : { path: source }); setNote(r.skipped ? "Такой пакет уже есть" : r.candidate ? "Обновление ожидает просмотра, текущий пакет сохранён" : "Пакет в карантине"); })}>Установить в карантин</${Button}>
      <p class="faint">Исходник только читается. Обновление сначала хранится кандидатом; прежняя версия остаётся до одобрения.</p>
    </div>
    <${PendingCards} /><${ErrorBox} error=${error} />${note ? html`<p class="note">${note}</p>` : null}
    ${state?.modules?.length === 0 ? html`<p class="faint">Модулей пока нет.</p>` : null}
    ${state?.modules?.map(m => {
      const server = servers.find(s => s.id === m.server);
      const current = file?.name === m.id;
      const reviewed = current && file.candidate ? m.candidate : m;
      return html`<div class="card module-card">
        <div class="row"><h3 class="grow">${m.id} ${m.metadata?.version || ""}</h3><span class=${`badge ${m.enabled ? "green" : "amber"}`}>${m.enabled ? "Включён" : m.approved ? "Выключен" : "Карантин"}</span></div>
        ${m.error ? html`<p class="note red">${m.error}</p>` : html`<p>${m.metadata.description || ""}</p><p class="faint">Файлов: ${m.files.length} · Способности: ${(m.metadata.capabilities || []).join(", ") || "не заявлены"} · ${m.hash.slice(0,12)}</p>`}
        <div class="row"><${Button} small busy=${busy} disabled=${!!m.error} onClick=${() => open(m.id)}>Просмотреть пакет</${Button}>
          ${m.candidate ? html`<${Button} small busy=${busy} onClick=${() => open(m.id, true)}>Просмотреть обновление ${m.candidate.metadata.version}</${Button}>` : null}
          ${m.approved ? html`<${Button} small onClick=${() => run(() => post(`/api/modules/${m.id}/enabled`, { enabled: !m.enabled }))}>${m.enabled ? "Выключить" : "Включить"}</${Button}>` : null}
          <${Button} small kind="danger" onClick=${() => { if (confirm(`Убрать модуль ${m.id}? Пакет сохранится в архиве.`)) run(() => del(`/api/modules/${m.id}`)); }}>Удалить</${Button}></div>
        ${current && reviewed ? html`<div class="module-preview">
          <${Field} label=${file.candidate ? "Файл обновления" : "Файл пакета"}><select class="select mono" value=${file.path} onChange=${e => open(m.id, file.candidate, e.target.value)}>${reviewed.files.map(f => html`<option value=${f.path}>${f.path} (${f.bytes} байт)</option>`)}</select></${Field}>
          <pre class="module-text mono">${file.text}</pre>
          ${file.next_offset !== null ? html`<${Button} small onClick=${() => open(m.id, file.candidate, file.path, file.next_offset)}>Следующий фрагмент</${Button}>` : null}
          <details><summary>Ранее одобренные хэши</summary><pre class="module-text mono">${JSON.stringify(m.previous || {}, null, 2)}</pre></details>
          ${file.path === "bender-module.json" ? html`<${Button} kind="primary" busy=${busy} onClick=${() => run(async () => { await post(`/api/modules/${m.id}/approve`, { hash: file.hash, candidate: file.candidate }); setFile(null); })}>Одобрить просмотренный пакет</${Button}>` : null}
        </div>` : null}
        ${m.enabled && m.server ? html`<div style="margin-top:12px"><${Button} small busy=${busy} onClick=${() => run(() => post(`/api/modules/${m.id}/discover`))}>Обнаружить инструменты</${Button}>
          ${server?.tools?.length ? html`<details><summary>Определения инструментов</summary><pre class="module-text mono">${JSON.stringify(server.tools.map(t => t.definition), null, 2)}</pre></details>` : null}
          ${server?.review ? html`<${Button} small kind="primary" busy=${busy} onClick=${() => run(() => post(`/api/mcp/${m.server}/approve`, { review: server.review }))}>Одобрить определения инструментов</${Button}>` : null}</div>` : null}
        ${m.enabled ? html`${(m.metadata.pages || []).map(p => html`<details class="module-page"><summary>${p.title || p.path}</summary><iframe title=${p.title || p.path} class="module-frame" sandbox="" referrerpolicy="no-referrer" src=${`/api/modules/${m.id}/page/${m.hash}/${p.path.split("/").map(encodeURIComponent).join("/")}`}></iframe></details>`)}
          ${(m.metadata.channels || []).map(c => html`<p><${Button} small onClick=${() => run(async () => { const hook = await post(`/api/modules/${m.id}/channel`, { channel: c.name, hash: m.hash }); setNote(`Вход ${c.name}: ${hook.path}. Ключ и настройка — в «Связь → Вебхуки».`); })}>Настроить вход ${c.title || c.name}</${Button}>${c.send_tool ? html`<span class="faint"> Выход — module_send через ${c.send_tool}</span>` : null}</p>`)}
          ${(m.metadata.providers || []).map((p,i) => html`<p>${p.name || p.kind} · <code>${p.base_url}</code> <${Button} small onClick=${() => run(async () => { await post(`/api/modules/${m.id}/provider`, { index: i, hash: m.hash }); setNote("Провайдер добавлен в «Модели»; ключ и роль выбери там."); })}>Подключить описанный API</${Button}></p>`)}` : null}
      </div>`;
    })}
    <p class="faint">Проверка в терминале: <code>python -m bender try modules</code>. Одобренные MCP-тулы доступны в Полигоне. Пример — tests/fixtures/module_demo.</p>
  </div></div>`;
}
