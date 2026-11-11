// Сердце: задачи по времени, пульс, намерения. Всё выключается; задачи из OpenClaw ждут здесь включения.
import { html, useEffect, useState } from "../lib/ui.js";
import { del, get, patch, post } from "../lib/api.js";
import { useEvents } from "../lib/store.js";
import { Button, ErrorBox, Field, Icon, IconButton, Seg, Toggle, Visor, timeAgo, toast } from "../lib/ui.js";

const KIND = { remind: ["⏰", "напоминание"], agent: ["🤖", "ход агента"], reflex: ["🔔", "рефлекс"] };
const MODES = [["two_stage", "Двухступенчатый"], ["always_model", "Всегда модель"], ["reflexes", "Только рефлексы"], ["off", "Выключен"]];
const MODE_HINT = {
  two_stage: "Сначала проверки без модели; модель — только если есть что обсудить. Ровный пульс ничего не стоит.",
  always_model: "Модель думает на каждом пульсе, как в OpenClaw. Дорого.",
  reflexes: "Только проверки без модели; найденное — строкой в разговор.",
  off: "Пульса нет. Задачи и напоминания работают.",
};
const REFLEX = [["disk", "Мало места"], ["file", "Файл изменился"], ["url", "Страница изменилась"], ["battery", "Батарея"], ["temp", "Перегрев"], ["command", "Команда"]];

function when(t) {
  if (!t) return "";
  const d = new Date(t * 1000);
  return d.toLocaleString("ru-RU", { weekday: "short", day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function JobForm({ onDone }) {
  const [f, setF] = useState({ kind: "remind", name: "", text: "", prompt: "", how: "at", at: "завтра в 9:00", every: "1d", cron: "0 9 * * 1-5", session: "isolated",
    window: "", idle_only: false, check: { type: "disk", path: "/", min_free_gb: 5 } });
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState(false);
  const set = (p) => setF((x) => ({ ...x, ...p }));
  const sched = () => ({ at: f.how === "at" ? f.at : "", every: f.how === "every" ? f.every : "", cron: f.how === "cron" ? f.cron : "", window: f.window });
  useEffect(() => {
    const t = setTimeout(() => post("/api/when", sched()).then(setPreview, (e) => setPreview({ error: e.message })), 300);
    return () => clearTimeout(t);
  }, [f.how, f.at, f.every, f.cron, f.window]);
  const save = async () => {
    setBusy(true);
    try {
      await post("/api/jobs", { kind: f.kind, name: f.name, text: f.text, prompt: f.prompt, session: f.session, idle_only: f.idle_only, check: f.kind === "reflex" ? f.check : null, ...sched() });
      toast("Задача добавлена", "ok"); onDone();
    } catch (e) { toast(e.message, "error"); } finally { setBusy(false); }
  };
  const c = f.check;
  return html`<div class="card"><div class="card-title"><${Icon} name="plus" /><h3>Новая задача</h3></div>
    <${Field} label="Что"><${Seg} value=${f.kind} options=${[["remind", "⏰ Напомнить"], ["agent", "🤖 Сделать агентом"], ["reflex", "🔔 Следить"]]} onChange=${(v) => set({ kind: v })} /></${Field}>
    ${f.kind === "remind" ? html`<${Field} label="О чём напомнить"><input aria-label="позвонить маме" class="input" value=${f.text} placeholder="позвонить маме" onInput=${(e) => set({ text: e.target.value })} /></${Field}>` : null}
    ${f.kind === "agent" ? html`<${Field} label="Поручение агенту" hint="Агент выполнит его, когда придёт время. Хозяина рядом не будет: опасное без полномочий будет отклонено.">
        <textarea aria-label="Пришли сводку: почта, календарь, погода" class="textarea" rows="3" value=${f.prompt} placeholder="Пришли сводку: почта, календарь, погода" onInput=${(e) => set({ prompt: e.target.value })}></textarea></${Field}>
      <${Field} label="Где"><${Seg} value=${f.session} options=${[["isolated", "Отдельно, итог — строкой"], ["main", "В разговоре со мной"]]} onChange=${(v) => set({ session: v })} /></${Field}>` : null}
    ${f.kind === "reflex" ? html`<${Field} label="За чем следить" hint="Без модели. Пишет только когда сработало.">
        <select class="select" value=${c.type} onChange=${(e) => set({ check: { type: e.target.value } })}>${REFLEX.map(([v, t]) => html`<option value=${v}>${t}</option>`)}</select></${Field}>
      ${c.type === "disk" ? html`<div class="row"><input aria-label="папка" class="input grow" value=${c.path || "/"} onInput=${(e) => set({ check: { ...c, path: e.target.value } })} placeholder="папка" />
        <input class="input" style="width:120px" type="number" value=${c.min_free_gb ?? 5} onInput=${(e) => set({ check: { ...c, min_free_gb: +e.target.value } })} /><span class="faint">ГБ минимум</span></div>` : null}
      ${c.type === "file" ? html`<input aria-label="~/важное.txt" class="input mono" value=${c.path || ""} placeholder="~/важное.txt" onInput=${(e) => set({ check: { ...c, path: e.target.value } })} />` : null}
      ${c.type === "url" ? html`<input aria-label="https://…" class="input mono" value=${c.url || ""} placeholder="https://…" onInput=${(e) => set({ check: { ...c, url: e.target.value } })} />
        <input aria-label="ждать появления текста (пусто — любое изменение)" class="input" style="margin-top:6px" value=${c.contains || ""} placeholder="ждать появления текста (пусто — любое изменение)" onInput=${(e) => set({ check: { ...c, contains: e.target.value } })} />` : null}
      ${c.type === "battery" ? html`<input class="input" type="number" value=${c.below ?? 20} onInput=${(e) => set({ check: { ...c, below: +e.target.value } })} />` : null}
      ${c.type === "temp" ? html`<input class="input" type="number" value=${c.above ?? 80} onInput=${(e) => set({ check: { ...c, above: +e.target.value } })} />` : null}
      ${c.type === "command" ? html`<input aria-label="systemctl is-active nginx" class="input mono" value=${c.cmd || ""} placeholder="systemctl is-active nginx" onInput=${(e) => set({ check: { ...c, cmd: e.target.value } })} />` : null}` : null}
    ${f.kind !== "remind" ? html`<${Field} label="Имя"><input aria-label="Утренняя сводка" class="input" value=${f.name} placeholder="Утренняя сводка" onInput=${(e) => set({ name: e.target.value })} /></${Field}>` : null}
    <${Field} label="Когда"><${Seg} value=${f.how} options=${[["at", "Разово"], ["every", "Каждые…"], ["cron", "Cron"]]} onChange=${(v) => set({ how: v })} />
      <input class=${`input${f.how === "cron" ? " mono" : ""}`} style="margin-top:6px" value=${f[f.how]} onInput=${(e) => set({ [f.how]: e.target.value })}
        placeholder=${{ at: "завтра в 9:00, через 20 минут, 23.10 18:30", every: "30m, 2h, 1d, каждые 15 минут", cron: "0 9 * * 1-5" }[f.how]} />
      <div class=${`faint small${preview?.error ? " red" : ""}`} style="margin-top:4px">${preview ? preview.error ? preview.error : `${preview.describe}${preview.next?.length ? " · ближайшие: " + preview.next.map((n) => n.human).join(", ") : ""}` : "…"}</div></${Field}>
    ${f.kind !== "remind" ? html`<div class="row"><input aria-label="окно: 08:00-23:00" class="input" style="max-width:200px" value=${f.window} placeholder="окно: 08:00-23:00" onInput=${(e) => set({ window: e.target.value })} />
      <${Toggle} label="только когда я не пишу" checked=${f.idle_only} onChange=${(v) => set({ idle_only: v })} /></div>` : null}
    <div class="row" style="margin-top:10px"><${Button} kind="primary" busy=${busy} disabled=${preview?.error || (f.kind === "remind" && !f.text) || (f.kind === "agent" && !f.prompt)} onClick=${save}>Добавить</${Button}>
      <${Button} kind="ghost" onClick=${onDone}>Отмена</${Button}></div>
  </div>`;
}

function Jobs({ data, reload }) {
  const [adding, setAdding] = useState(false);
  const [open, setOpen] = useState(null);
  const [runs, setRuns] = useState([]);
  const show = async (j) => { if (open === j.id) { setOpen(null); return; } setOpen(j.id); setRuns((await get(`/api/jobs/${j.id}/runs`)).runs); };
  const act = (fn) => async () => { try { await fn(); reload(); } catch (e) { toast(e.message, "error"); } };
  const fromOC = data.jobs.filter((j) => j.source === "openclaw" && !j.enabled && !j.runs);
  return html`<div class="card"><div class="card-title"><${Icon} name="history" /><h3>Задачи по времени</h3>
      ${!adding ? html`<${Button} small icon="plus" onClick=${() => setAdding(true)}>Добавить</${Button}>` : null}</div>
    ${!data.enabled ? html`<div class="note amber">Сердце выключено — задачи не запускаются. Включить — ниже, «Пульс».</div>` : null}
    ${fromOC.length ? html`<div class="note">Из OpenClaw пришло задач: ${fromOC.length}. Они выключены — посмотри, что они делают, и включи нужные.</div>` : null}
    ${adding ? html`<${JobForm} onDone=${() => { setAdding(false); reload(); }} />` : null}
    ${!data.jobs.length && !adding ? html`<div class="faint">Задач нет. Попроси Бендера: «напомни завтра в 9 позвонить маме» или «каждое утро присылай сводку».</div>` : null}
    <div class="list">${data.jobs.map((j) => html`<div class="job">
      <div class="list-item">
        <span class="job-kind" title=${KIND[j.kind]?.[1]}>${KIND[j.kind]?.[0] || "?"}</span>
        <div class="li-main" onClick=${() => show(j)} style="cursor:pointer">
          <div class="li-title">${j.name} ${j.source === "openclaw" ? html`<span class="badge blue">OpenClaw</span>` : null} ${!j.enabled ? html`<span class="badge">${j.runs && !j.next_run ? "завершена" : "выключена"}</span>` : null}
            ${j.last_status === "error" ? html`<span class="badge red">ошибка</span>` : null}</div>
          <div class="li-sub">${j.when}${j.enabled && j.next_run && j.schedule.kind !== "at" ? ` · следующий: ${j.next_human}` : ""}${j.window ? ` · окно ${j.window}` : ""}${j.idle_only ? " · в простое" : ""}${j.last_run ? ` · был ${timeAgo(j.last_run)}` : ""}</div>
        </div>
        <${Toggle} checked=${j.enabled} onChange=${(v) => act(() => patch(`/api/jobs/${j.id}`, { enabled: v }))()} />
        <${IconButton} small icon="play" title="Запустить сейчас" onClick=${act(async () => { const r = await post(`/api/jobs/${j.id}/run`); toast(`${r.status}${r.summary ? ": " + r.summary.slice(0, 80) : ""}`, r.status === "ok" ? "ok" : "error"); })} />
        <${IconButton} small icon="trash" danger title="Удалить" onClick=${act(async () => { if (confirm(`Удалить «${j.name}»?`)) await del(`/api/jobs/${j.id}`); })} />
      </div>
      ${open === j.id ? html`<div class="job-more">
        ${j.action.prompt ? html`<div><b>Поручение:</b> ${j.action.prompt} <span class="faint">(${j.action.session === "main" ? "в разговоре" : "отдельно"})</span></div>` : null}
        ${j.action.text ? html`<div><b>Текст:</b> ${j.action.text}</div>` : null}
        ${j.action.check ? html`<div><b>Проверка:</b> <span class="mono">${JSON.stringify(j.action.check)}</span></div>` : null}
        ${j.authority?.length ? html`<div><b>Полномочия без вопроса:</b> ${j.authority.map((a) => `${a.capability} «${a.target || "*"}»`).join(", ")}</div>` : null}
        ${j.last_error ? html`<div class="red">Последняя ошибка: ${j.last_error}</div>` : null}
        <div class="faint small" style="margin-top:6px">Запуски: ${runs.length ? "" : "ещё не было"}</div>
        ${runs.map((r) => html`<div class="small"><span class=${`badge ${r.status === "ok" ? "green" : r.status === "error" ? "red" : ""}`}>${r.status}</span> ${when(r.started)} ${r.summary ? "— " + r.summary.slice(0, 200) : ""}</div>`)}
      </div>` : null}
    </div>`)}</div>
  </div>`;
}

function Pulse({ heart, reload }) {
  const [p, setP] = useState(null);
  const [busy, setBusy] = useState(false);
  const load = () => get("/api/pulse").then(setP, () => {});
  useEffect(() => { load(); }, []);
  const setS = async (changes) => { try { await post("/api/settings/heart", changes); reload(); load(); } catch (e) { toast(e.message, "error"); } };
  if (!p) return null;
  return html`<div class="card"><div class="card-title"><${Icon} name="heart" /><h3>Пульс</h3>
      <${Button} small busy=${busy} onClick=${async () => { setBusy(true); try { const r = await post("/api/pulse"); toast(`Пульс: ${{ silent: "ровный", tell: "сказал", do: "сделал сам", alarm: "тревога" }[r.action] || r.action}${r.model ? " (думала модель)" : ""}`, "ok"); load(); } catch (e) { toast(e.message, "error"); } finally { setBusy(false); } }}>Проверить сейчас</${Button}></div>
    <${Toggle} label="Сердце бьётся" checked=${heart.enabled} onChange=${(v) => setS({ enabled: v })} hint="Выключенное сердце — ни задач, ни пульса, ни сна: агент отвечает только на сообщения." />
    <${Field} label="Режим" hint=${MODE_HINT[heart.mode]}><${Seg} value=${heart.mode} options=${MODES} onChange=${(v) => setS({ mode: v })} /></${Field}>
    <div class="row">
      <${Field} label="Пульс, минут"><input class="input" type="number" value=${heart.interval_minutes} onChange=${(e) => setS({ interval_minutes: +e.target.value })} /></${Field}>
      <${Field} label="Ночью, минут"><input class="input" type="number" value=${heart.night_interval_minutes} onChange=${(e) => setS({ night_interval_minutes: +e.target.value })} /></${Field}>
      <${Field} label="Тихие часы"><input class="input" value=${heart.quiet_hours} onChange=${(e) => setS({ quiet_hours: e.target.value })} /></${Field}>
    </div>
    <div class="faint small">${p.last_at ? `Последний удар ${timeAgo(p.last_at)}` : "Ударов ещё не было"}${p.next_at ? ` · следующий ${when(p.next_at)}` : ""} · модель думала ${p.calls} раз из ${p.beats}${p.quiet ? " · сейчас тихие часы" : ""}${p.last_result ? ` · в прошлый раз: ${p.last_result}` : ""}</div>
    <div style="margin-top:8px">Чек-лист HEARTBEAT.md: ${p.checklist.length ? html`<ul class="small">${p.checklist.map((x) => html`<li>${x}</li>`)}</ul>` : html`<span class="faint">пуст — пульс смотрит только сигналы. Пункты добавляются в <a href="#/self">«Личность» → «Пульс»</a>.</span>`}</div>
  </div>`;
}

function Intents() {
  const [rows, setRows] = useState(null);
  const [f, setF] = useState({ text: "", keywords: "" });
  const load = () => get("/api/intents").then((r) => setRows(r.intents), () => setRows([]));
  useEffect(() => { load(); }, []);
  const add = async () => { try { await post("/api/intents", f); setF({ text: "", keywords: "" }); load(); } catch (e) { toast(e.message, "error"); } };
  if (!rows) return null;
  const live = rows.filter((r) => r.status === "armed");
  return html`<div class="card"><div class="card-title"><${Icon} name="sparkles" /><h3>Намерения</h3></div>
    <div class="faint small">«Когда зайдёт речь о … — напомни себе …». Срабатывают на твои слова без модели и только подсказывают агенту.</div>
    <div class="list" style="margin-top:8px">${live.map((r) => html`<div class="list-item"><div class="li-main"><div class="li-title">${r.text} ${r.source === "openclaw" ? html`<span class="badge blue">OpenClaw</span>` : null}</div>
      <div class="li-sub">на слова: ${r.keywords.join(", ")} · сработало ${r.fired} из ${r.budget}${r.expires ? ` · до ${new Date(r.expires * 1000).toLocaleDateString("ru-RU")}` : ""}</div></div>
      <${IconButton} small icon="trash" danger title="Отменить" onClick=${async () => { await del(`/api/intents/${r.id}`); load(); }} /></div>`)}</div>
    ${!live.length ? html`<div class="faint">Намерений нет.</div>` : null}
    <div class="row" style="margin-top:10px"><input aria-label="на слова: релиз, выпуск версии" class="input grow" value=${f.keywords} placeholder="на слова: релиз, выпуск версии" onInput=${(e) => setF({ ...f, keywords: e.target.value })} />
      <input aria-label="напомнить себе: спроси про changelog" class="input grow" value=${f.text} placeholder="напомнить себе: спроси про changelog" onInput=${(e) => setF({ ...f, text: e.target.value })} />
      <${Button} small kind="primary" disabled=${!f.text || !f.keywords} onClick=${add}>Добавить</${Button}></div>
  </div>`;
}

export function HeartScreen() {
  const [data, setData] = useState(null);
  const [heart, setHeart] = useState(null);
  const [err, setErr] = useState(null);
  const load = () => get("/api/jobs").then((j) => { setData(j); setHeart(j.settings); }, setErr);
  useEffect(() => { load(); }, []);
  useEvents((ev) => { if (ev.type === "job" || (ev.type === "settings" && ev.group === "heart")) load(); }, []);
  if (!data || !heart) return html`<div class="page"><div class="page-inner"><${ErrorBox} error=${err} /><${Visor} mode="busy" /></div></div>`;
  return html`<div class="page"><div class="page-inner">
    <${Jobs} data=${data} reload=${load} />
    <${Pulse} heart=${heart} reload=${load} />
    <${Intents} />
  </div></div>`;
}
