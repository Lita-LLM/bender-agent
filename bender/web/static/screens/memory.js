// Память: что агент помнит, откуда это и как он это находит. Обзор, поиск, дневник, факты.
import { html, useEffect, useState } from "../lib/ui.js";
import { get, post } from "../lib/api.js";
import {MemoryInspector} from "./inspector.js";
import {useApp} from "../lib/store.js";
import { mdToHtml } from "../lib/md.js";
import { useEvents } from "../lib/store.js";
import { Button, ErrorBox, Icon, Seg, Tabs, Visor, timeAgo, toast } from "../lib/ui.js";

const TABS = [["overview", "Обзор"], ["search", "Поиск"], ["diary", "Дневник"], ["facts", "Факты"]];
const KIND = { core: "ядро", diary: "дневник", session: "разговор", fact: "факт", note: "заметка" };
const PROV = { owner: ["от хозяина", "green"], bender: ["вывел сам", ""], untrusted: ["недоверенное", "red"], imported: ["перенесено", "blue"], system: ["служебное", ""] };

function Prov({ p }) {
  const [label, cls] = PROV[p] || [p, ""];
  return html`<span class=${`badge ${cls}`}>${label}</span>`;
}

function Bar({ value, max, warn = 0.8 }) {
  const f = Math.min(1, value / Math.max(1, max));
  return html`<div class="meter"><i class=${f >= 1 ? "red" : f >= warn ? "amber" : ""} style=${`width:${Math.round(f * 100)}%`}></i></div>`;
}

function Overview({ data, reload }) {
  const [busy, setBusy] = useState("");
  const act = (name, fn) => async () => { setBusy(name); try { await fn(); await reload(); } catch (e) { toast(e.message, "error"); } finally { setBusy(""); } };
  const c = data.compact, v = data.vectors, idx = data.index, r = data.review;
  return html`<div>
    <div class="grid2">
      ${["memory", "user"].map((k) => { const x = data.core[k]; return html`<div class="card">
        <h3><${Icon} name=${k === "memory" ? "memory" : "user"} /> ${x.file} <span class="faint">— ${k === "memory" ? "долговременная память" : "директивы о тебе"}</span></h3>
        <${Bar} value=${x.chars} max=${x.limit} />
        <div class="faint" style="margin-top:6px">${x.chars} из ${x.limit} символов · записей: ${x.entries}${x.template ? " · пока заготовка" : ""}</div>
        <div style="margin-top:8px"><a href="#/self">Открыть и править →</a></div></div>`; })}
    </div>

    <div class="card">
      <h3><${Icon} name="history" /> Разговор и его пересказ</h3>
      <${Bar} value=${c.tail_tokens} max=${c.budget} warn=${c.trigger / Math.max(1, c.budget)} />
      <div class="faint" style="margin-top:6px">История занимает ≈${c.tail_tokens} из ${c.budget} токенов бюджета${c.enabled ? ` · сожмётся после ${c.trigger}` : " · сжатие выключено"}${c.dropped ? ` · не влезло: ${c.dropped} сообщ.` : ""}</div>
      ${c.summary ? html`<details style="margin-top:10px"><summary>Пересказ начала: ${c.count} сообщ., частей ${c.parts}${c.ts ? `, ${timeAgo(c.ts)}` : ""}</summary>
        <div class="m-text" style="margin-top:8px" dangerouslySetInnerHTML=${{ __html: mdToHtml(c.summary) }}></div></details>` : html`<div class="faint" style="margin-top:6px">Пересказа пока нет — разговор помещается целиком.</div>`}
      ${c.error ? html`<div class="note amber" style="margin-top:8px">Последнее сжатие не удалось: ${c.error}</div>` : null}
      <div class="row" style="margin-top:10px">
        <${Button} small busy=${busy === "compact"} onClick=${act("compact", async () => { await post("/api/memory/compact", { action: "now" }); toast("Начало разговора пересказано", "ok"); })}>Сжать сейчас</${Button}>
        <${Button} small kind="ghost" busy=${busy === "page"} title="Модель начнёт разговор заново; память, дневник и поиск останутся"
          onClick=${act("page", async () => { await post("/api/memory/compact", { action: "page" }); toast("Новая страница", "ok"); })}>Новая страница</${Button}>
        ${c.summary ? html`<${Button} small kind="ghost" busy=${busy === "reset"} onClick=${act("reset", () => post("/api/memory/compact", { action: "reset" }))}>Забыть пересказ</${Button}>` : null}
      </div>
    </div>

    <div class="grid2">
      <div class="card">
        <h3><${Icon} name="search" /> Индекс</h3>
        <div>Кусков: <b>${idx.chunks}</b> ${Object.entries(idx.by_kind || {}).map(([k, n]) => html`<span class="badge">${KIND[k] || k}: ${n}</span> `)}</div>
        <div class="faint" style="margin-top:6px">Поиск словами: ${idx.fts ? "FTS5 (основы слов + триграммы)" : "запасной (FTS5 в этом SQLite нет)"}</div>
        <div style="margin-top:6px">По смыслу: ${v.available ? html`<span class="badge green">включён</span> <span class="faint">${v.model || ""} · с векторами ${idx.with_vectors}${v.pending ? ` · ждут: ${v.pending}` : ""}</span>`
          : html`<span class="badge">выключен</span> <span class="faint">нужна роль «Эмбеддинги» в <a href="#/models">Моделях</a></span>`}</div>
        ${v.error ? html`<div class="note amber" style="margin-top:8px">Векторы: ${v.error}</div>` : null}
        <div class="row" style="margin-top:10px">
          <${Button} small busy=${busy === "reindex"} onClick=${act("reindex", () => post("/api/memory/reindex"))}>Пересобрать индекс</${Button}>
          ${v.available ? html`<${Button} small busy=${busy === "vectors"} onClick=${act("vectors", () => post("/api/memory/vectors"))}>Досчитать векторы</${Button}>` : null}
        </div>
      </div>
      <div class="card">
        <h3><${Icon} name="sparkles" /> Само запоминание</h3>
        <div>Разбор после хода: ${r.enabled ? html`<span class="badge green">включён</span>` : html`<span class="badge">выключен</span>`}
          <span class="faint">${r.enabled ? (r.every === 1 ? "после каждого хода (роль «Быстрая»)" : `пачкой раз в ${r.every} ходов — роли «Быстрая» нет`) : ""}</span></div>
        ${r.pending ? html`<div class="faint" style="margin-top:6px">Ходов ждут разбора: ${r.pending}</div>` : null}
        <div style="margin-top:6px">Фактов: <b>${data.facts.active}</b> <span class="faint">(всего с прошлыми: ${data.facts.total})</span></div>
        <div style="margin-top:6px">Дней в дневнике: <b>${data.diary_total}</b></div>
        <div class="faint" style="margin-top:8px">Триггеры: ${data.settings.triggers ? "да" : "нет"} · «Вспомнилось»: ${data.settings.auto_recall ? "да" : "нет"} · <a href="#/settings">настройки памяти</a></div>
      </div>
    </div>
  </div>`;
}

function Search() {
  const {st,route}=useApp();
  const asked = route.query.get("q") || "";
  const [selected,setSelected]=useState([]),[inspect,setInspect]=useState(null);
  const [q, setQ] = useState(asked);
  const [kind, setKind] = useState("");
  const [hits, setHits] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const run = async () => {
    if (!q.trim()) return;
    setBusy(true); setErr(null);
    try { const t0 = performance.now(); const r = await get(`/api/memory/search?q=${encodeURIComponent(q)}&kinds=${kind || "core,diary,session,fact,note"}&limit=30`); setHits({ list: r.hits, ms: Math.round(performance.now() - t0) }); }
    catch (e) { setErr(e); } finally { setBusy(false); }
  };
  useEffect(() => { if (asked) setQ(asked); }, [asked]);
  useEffect(() => { if (asked && q === asked) run(); }, [asked, q]);
  return html`<div>
    <div class="row" style="margin-bottom:10px">
      <input aria-label="Что вспомнить? Так же ищет агент тулом recall" class="input grow" placeholder="Что вспомнить? Так же ищет агент тулом recall" value=${q} onInput=${(e) => setQ(e.target.value)} onKeyDown=${(e) => e.key === "Enter" && run()} />
      <${Button} kind="primary" busy=${busy} onClick=${run} icon="search">Найти</${Button}>
    </div>
    <${Seg} value=${kind} options=${[["", "всё"], ["core", "ядро"], ["diary", "дневник"], ["session", "разговоры"], ["fact", "факты"]]} onChange=${setKind} />
    <${ErrorBox} error=${err} />
    ${selected.length>1?html`<${Button} onClick=${()=>setInspect(selected)}>Слить выбранные (${selected.length})</${Button}>`:null}
    ${hits ? html`<div class="faint" style="margin:10px 0">Найдено: ${hits.list.length} · ${hits.ms} мс</div>
      ${hits.list.map((h) => html`<div class="card hit">
        <div class="row small"><span class="badge">${KIND[h.kind] || h.kind}</span><span class="faint">${h.source}</span><${Prov} p=${h.provenance} />
          <span class="faint">${timeAgo(h.ts)} · важность ${h.importance}</span><span class="grow"></span><span class="faint mono">${h.score.toFixed(4)}</span></div>
        <div class="hit-text">${h.text}</div>
        ${st?.inspectors?.enabled!==false?html`<div class="row"><label><input type="checkbox" aria-label=${`Выбрать запись ${h.id}`} checked=${selected.includes(h.id)} onChange=${e=>setSelected(e.target.checked?[...selected,h.id]:selected.filter(i=>i!==h.id))}/> №${h.id}</label><${Button} small onClick=${()=>setInspect([h.id])}>Инспектор</${Button}></div>`:null}
        ${h.why?.length ? html`<div class="faint small">почему: ${h.why.join(", ")}</div>` : null}</div>`)}` : null}
    ${inspect?html`<${MemoryInspector} ids=${inspect} onClose=${()=>setInspect(null)} onChanged=${()=>{setSelected([]);run();}}/>`:null}
  </div>`;
}

function Diary({ data }) {
  const [cur, setCur] = useState(data.diary[0]?.path || "");
  const [text, setText] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => { if (cur) get(`/api/memory/diary?path=${encodeURIComponent(cur)}`).then((r) => setText(r.text), setErr); }, [cur]);
  if (!data.diary.length) return html`<div class="note">Дневник пуст. Агент пишет в него тулом note, разбор после хода — заметные события, а сжатие — что было в пересказанной части разговора.</div>`;
  return html`<div class="split">
    <div class="split-list">${data.diary.map((d) => html`<button type="button" class=${`day${d.path === cur ? " on" : ""}`} onClick=${() => setCur(d.path)}>
      ${d.path.replace(/\.md$/, "")}${d.path.startsWith("openclaw/") ? html` <span class="badge blue">OpenClaw</span>` : null}<span class="faint small"> ${Math.max(1, Math.round(d.size / 1024))} КБ</span></button>`)}</div>
    <div class="split-body"><${ErrorBox} error=${err} />${text === null ? html`<${Visor} mode="busy" />` : html`<div class="m-text" dangerouslySetInnerHTML=${{ __html: mdToHtml(text) }}></div>`}</div>
  </div>`;
}

function Facts() {
  const [all, setAll] = useState(false);
  const [rows, setRows] = useState(null);
  const [err, setErr] = useState(null);
  const load = () => get(`/api/memory/facts${all ? "?all=1" : ""}`).then((r) => setRows(r.facts), setErr);
  useEffect(() => { load(); }, [all]);
  const day = (ts) => (ts ? new Date(ts * 1000).toLocaleDateString("ru-RU") : "");
  const close = async (f) => { if (!confirm(`Это больше не верно?\n${f.subject} — ${f.relation} — ${f.object}`)) return; try { await post(`/api/memory/facts/${f.id}/close`); load(); } catch (e) { toast(e.message, "error"); } };
  return html`<div>
    <div class="row" style="margin-bottom:10px"><${Seg} value=${all ? "all" : "now"} options=${[["now", "верные сейчас"], ["all", "с прошлыми"]]} onChange=${(v) => setAll(v === "all")} />
      <span class="faint grow">Агент записывает факты тулом fact; изменившийся — не стирает, а закрывает датой.</span></div>
    <${ErrorBox} error=${err} />
    ${!rows ? html`<${Visor} mode="busy" />` : !rows.length ? html`<div class="note">Фактов пока нет.</div>` : html`<table class="t"><thead><tr><th>Кто/что</th><th>Связь</th><th>Значение</th><th>Когда</th><th>Откуда</th><th></th></tr></thead><tbody>
      ${rows.map((f) => html`<tr class=${f.valid_to ? "faint" : ""}><td>${f.subject}</td><td>${f.relation}</td><td>${f.valid_to ? html`<s>${f.object}</s>` : f.object}</td>
        <td class="nowrap faint">с ${day(f.valid_from)}${f.valid_to ? ` по ${day(f.valid_to)}` : ""}</td><td><${Prov} p=${f.provenance} /></td>
        <td>${f.valid_to ? null : html`<button type="button" class="link" title="Больше не верно" onClick=${() => close(f)}>закрыть</button>`}</td></tr>`)}
    </tbody></table>`}
  </div>`;
}

export function MemoryScreen() {
  const { route } = useApp();
  const [tab, setTab] = useState(route.query.get("q") ? "search" : "overview");
  useEffect(() => { if (route.query.get("q")) setTab("search"); }, [route.query.get("q")]);
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const load = () => get("/api/memory/overview").then(setData, setErr);
  useEffect(() => { load(); }, []);
  useEvents((ev) => { if (["memory", "compacted", "imported"].includes(ev.type)) load(); }, []);
  return html`<div class="page"><div class="page-inner">
    <${Tabs} value=${tab} tabs=${TABS} onChange=${setTab} />
    <div style="margin-top:14px">
      <${ErrorBox} error=${err} />
      ${!data ? html`<${Visor} mode="busy" />` : tab === "overview" ? html`<${Overview} data=${data} reload=${load} />` : tab === "search" ? html`<${Search} />`
        : tab === "diary" ? html`<${Diary} data=${data} />` : html`<${Facts} />`}
    </div>
  </div></div>`;
}
