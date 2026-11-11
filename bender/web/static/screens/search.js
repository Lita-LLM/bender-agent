// Настройки рисуются из схемы; здесь — проверка каждого поставщика без модели.
import { html, useState } from "../lib/ui.js";
import { post } from "../lib/api.js";
import { Button, ErrorBox } from "../lib/ui.js";
import { PendingCards } from "./chat.js";

export function SearchTest() {
  const [provider, setProvider] = useState("duckduckgo");
  const [query, setQuery] = useState("Бендер");
  const [result, setResult] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    setBusy(true); setError(null); setResult(null);
    try { setResult(await post("/api/search/test", { provider, query })); } catch (e) { setError(e); }
    setBusy(false);
  };
  return html`<div class="search-test">
    <h4>Проверить поставщика</h4>
    <div class="row"><select class="select" aria-label="Поставщик поиска" value=${provider} onChange=${e => setProvider(e.target.value)}>${["duckduckgo", "searxng", "brave", "tavily"].map(p => html`<option value=${p}>${p}</option>`)}</select>
      <input class="input grow" aria-label="Поисковый запрос" value=${query} onInput=${e => setQuery(e.target.value)} />
      <${Button} busy=${busy} onClick=${run}>Проверить поиск</${Button}></div>
    <${PendingCards} /><${ErrorBox} error=${error} />
    ${result ? html`<div class="search-results"><p class="faint">${result.provider}${result.cached ? " · из памяти" : ""} · найдено: ${result.results.length}</p>${result.results.map(r => html`<p><a href=${r.url} target="_blank" rel="noopener noreferrer">${r.title}</a><br/><span class="faint">${r.snippet}</span></p>`)}</div>` : null}
    <p class="faint">Найденную страницу агент открывает через web_fetch. Результаты считаются недоверенными.</p>
  </div>`;
}
