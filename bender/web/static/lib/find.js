// Поиск по всему пульту: экраны, настройки, подписи и кнопки экранов. Индекс строит сервер из кода экранов.
import { html, useEffect, useRef, useState } from "./ui.js";
import { get } from "./api.js";
import { go } from "./store.js";
import { Icon, Modal } from "./ui.js";

const KIND = { screen: "экран", setting: "настройка", heading: "раздел", field: "поле", action: "кнопка" };
const WEIGHT = { screen: 4, setting: 3, heading: 2, field: 1, action: 1 };
const norm = (text) => String(text || "").toLowerCase().replace(/ё/g, "е");
let cached = null;

export function match(items, query) {
  const words = norm(query).split(/\s+/).filter(Boolean);
  if (!words.length) return [];
  const out = [];
  for (const item of items) {
    const title = norm(item.title);
    const hay = [title, norm(item.screen), norm(item.group), norm(item.hint), norm(item.key), norm(item.words)].join(" ");
    if (!words.every((w) => hay.includes(w))) continue;
    let score = WEIGHT[item.kind] || 0;
    if (words.every((w) => title.includes(w))) score += 10;
    if (title.startsWith(words[0])) score += 5;
    if (title.split(/[\s,.(«]+/).some((part) => part.startsWith(words[0]))) score += 2;
    out.push([score, item]);
  }
  return out.sort((a, b) => b[0] - a[0]).slice(0, 40).map(([, item]) => item);
}

// После перехода найти подпись на экране, раскрыть, прокрутить и подсветить.
export function reveal(texts, tries = 30) {
  const wanted = (texts || []).map(norm).filter(Boolean);
  if (!wanted.length) return;
  const nodes = [...document.querySelectorAll("main .page h3, main .page h4, main .page summary, main .page label, main .page .t-label, main .page button, main .page .li-title")];
  for (const target of wanted) {
    const node = nodes.find((n) => norm(n.textContent).trim() === target) || nodes.find((n) => norm(n.textContent).includes(target));
    if (!node) continue;
    for (let d = node.closest("details"); d; d = d.parentElement?.closest("details")) d.open = true;
    const box = node.closest(".field, .toggle, .list-item") || (node.matches("h3, h4") ? node.closest(".card") : null) || node;
    box.scrollIntoView({ block: "center" });
    box.classList.remove("found-flash"); void box.offsetWidth; box.classList.add("found-flash");
    setTimeout(() => box.classList.remove("found-flash"), 2600);
    return;
  }
  if (tries > 0) setTimeout(() => reveal(texts, tries - 1), 100);  // экран ещё грузит данные
}

export function Finder({ onClose }) {
  const [items, setItems] = useState(cached);
  const [query, setQuery] = useState("");
  const [at, setAt] = useState(0);
  const [error, setError] = useState(null);
  const list = useRef(null);
  useEffect(() => { get("/api/find").then((r) => { cached = r.items; setItems(r.items); }, setError); }, []);
  const found = items ? match(items, query) : [];
  const extra = query.trim() ? [{ kind: "memory", title: `Искать «${query.trim()}» в памяти агента`, route: "#/memory?q=" + encodeURIComponent(query.trim()) }] : [];
  const rows = [...found, ...extra];
  useEffect(() => setAt(0), [query]);
  useEffect(() => { list.current?.querySelector(".on")?.scrollIntoView({ block: "nearest" }); }, [at]);
  const open = (item) => {
    onClose();
    go(item.route);
    if (item.kind !== "memory") setTimeout(() => reveal(item.find), 60);
  };
  const key = (e) => {
    if (e.key === "ArrowDown") { e.preventDefault(); setAt((n) => Math.min(n + 1, rows.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setAt((n) => Math.max(n - 1, 0)); }
    else if (e.key === "Enter" && rows[at]) { e.preventDefault(); open(rows[at]); }
  };
  return html`<${Modal} title="Найти в пульте" onClose=${onClose}>
    <input class="input" type="search" aria-label="Что найти" placeholder="песочница, бекап, telegram, бюджет…" value=${query} onInput=${(e) => setQuery(e.target.value)} onKeyDown=${key} />
    ${error ? html`<p class="faint">Индекс не загрузился: ${error.message}</p>` : !items ? html`<p class="faint">Загружаю…</p>` : null}
    <div class="find-list" ref=${list} role="listbox" aria-label="Найдено">
      ${rows.map((item, i) => html`<button type="button" role="option" aria-selected=${i === at} class=${"find-item" + (i === at ? " on" : "")} onMouseEnter=${() => setAt(i)} onClick=${() => open(item)}>
        <span class="find-title">${item.kind === "memory" ? html`<${Icon} name="search" /> ` : null}${item.title}</span>
        <span class="find-where">${item.kind === "memory" ? "Память → Поиск" : [KIND[item.kind], item.kind === "screen" ? "" : item.screen, item.group && item.group !== item.screen ? item.group : ""].filter(Boolean).join(" · ")}</span>
        ${item.hint ? html`<span class="find-hint">${item.hint}</span>` : null}
      </button>`)}
      ${items && query.trim() && !found.length ? html`<p class="faint">В пульте не нашлось. Попробуй другое слово или поищи в памяти.</p>` : null}
    </div>
    <p class="faint">Ctrl+K или / — открыть поиск. Стрелки и Enter — перейти.</p>
  </${Modal}>`;
}
