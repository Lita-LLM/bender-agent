// Тело: машина, на которой живёт агент, и он сам. Самочувствие меняет поведение (реже пульс, бесплатные модели).
import { html, useEffect, useState } from "../lib/ui.js";
import { get, post } from "../lib/api.js";
import { Button, ErrorBox, Icon, Modal, Toggle, Visor, money, timeOf } from "../lib/ui.js";
import { useApp, useEvents } from "../lib/store.js";
import {StartupStatus} from '../lib/lifecycle.js';

const MOOD_CLS = { ok: "green", tired: "amber", hungry: "amber", sick: "red", hot: "red", drained: "red" };
const SUBJECT = { bender: "Бендер", stalls: "зависания", node: "узел", memory: "память", heat: "перегрев" };
// «20261124-213000-node.txt» → «24.11 21:30 · узел»
const reportLabel = (name) => { const m = /^(\d{4})(\d\d)(\d\d)-(\d\d)(\d\d)\d\d-([a-z]+)/.exec(name); return m ? `${m[3]}.${m[2]} ${m[4]}:${m[5]} · ${SUBJECT[m[6]] || m[6]}` : name; };
const MANAGER_ON = [["all", "при любой необычной"], ["bender", "при нагрузке от Бендера"], ["node", "при нагрузке узла"], ["off", "никогда"]];

function Meter({ value, max, warn = 0.8 }) {
  const f = Math.min(1, value / Math.max(1e-9, max));
  return html`<div class="meter"><i class=${f >= 1 ? "red" : f >= warn ? "amber" : ""} style=${`width:${Math.round(f * 100)}%`}></i></div>`;
}

function dur(s) {
  if (s == null) return "—";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d} дн ${h} ч` : h ? `${h} ч ${m} мин` : `${m} мин`;
}

// ЦП за час: янтарная — сам Бендер (% одного ядра), синяя — весь узел (%). Цвета — переменные темы.
function Spark({ samples }) {
  const pts = (samples || []).filter((s) => s.own != null);
  if (pts.length < 2) return html`<div class="faint small">Замеров пока мало — график появится через минуту.</div>`;
  const W = 600, H = 80, t0 = pts[0].t, span = Math.max(1, pts[pts.length - 1].t - t0);
  const top = Math.max(100, ...pts.map((s) => Math.max(s.own || 0, s.sys || 0)));
  const x = (t) => (((t - t0) / span) * W).toFixed(1), y = (v) => (H - (Math.min(v, top) / top) * H).toFixed(1);
  const line = (key) => pts.filter((s) => s[key] != null).map((s) => `${x(s.t)},${y(s[key])}`).join(" ");
  return html`<svg viewBox=${`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img" aria-label="Процессор за последний час" style="width:100%;height:80px;display:block;margin:6px 0">
      <line x1="0" x2=${W} y1=${y(100)} y2=${y(100)} stroke="var(--line-2)" stroke-dasharray="4 4" vector-effect="non-scaling-stroke" />
      <polyline points=${line("sys")} fill="none" stroke="var(--blue)" stroke-width="1.5" vector-effect="non-scaling-stroke" />
      <polyline points=${line("own")} fill="none" stroke="var(--amber)" stroke-width="2" vector-effect="non-scaling-stroke" /></svg>
    <div class="faint small"><span style="color:var(--amber)">━</span> Бендер, % ядра · <span style="color:var(--blue)">━</span> узел, % · пунктир — 100%</div>`;
}

function numbers(n) {
  const out = [];
  if (n.own != null) out.push(`Бендер ${Math.round(n.own)}% ядра` + (n.own_5m != null ? ` (за 5 мин ${Math.round(n.own_5m)}%)` : ""));
  if (n.sys != null) out.push(`узел занят ${Math.round(n.sys)}%` + (n.iowait >= 1 ? `, ожидание диска ${Math.round(n.iowait)}%` : ""));
  if (n.load1 != null) out.push(`load ${n.load1} на ${n.cores} яд.`);
  if (n.mem != null) out.push(`память свободна ${Math.round(n.mem)}%`);
  if (n.swap) out.push(`подкачка ${n.swap} МБ`);
  if (n.temp != null) out.push(`${n.temp} °C`);
  return out.join(" · ");
}

function LoadCard() {
  const s = useApp();
  const [d, setD] = useState(null), [err, setErr] = useState(null), [busy, setBusy] = useState(false), [report, setReport] = useState(null);
  const load = (top = false) => get("/api/load" + (top ? "?top=1" : "")).then(setD, setErr);
  useEffect(() => { load(); }, []);
  useEvents((ev) => { if ((ev.type === "settings" && ev.group === "load") || (ev.type === "notify" && ev.kind === "load")) load(); });
  const save = async (values) => { setBusy(true); setErr(null); try { await post("/api/settings/load", values); await load(); } catch (e) { setErr(e); } setBusy(false); };
  const fresh = async () => { setBusy(true); setErr(null); try { await load(true); } finally { setBusy(false); } };
  const open = async (name) => { setErr(null); try { setReport(await get("/api/load/report/" + encodeURIComponent(name))); } catch (e) { setErr(e); } };
  if (!d) return html`<div class="card"><h3>Нагрузка</h3><${ErrorBox} error=${err} /></div>`;
  const cfg = d.settings || {}, now = d.now || {};
  const agents = (s.st?.agents || []).filter((a) => !a.archived);
  const known = !cfg.manager || agents.some((a) => a.id === cfg.manager);
  return html`<div class="card" data-load-card><div class="card-title"><${Icon} name="activity" /><h3>Нагрузка</h3><${Button} small busy=${busy} icon="refresh" onClick=${fresh}>Кто грузит сейчас</${Button}></div>
    <${ErrorBox} error=${err} onClose=${() => setErr(null)} />
    <${Toggle} label="Анализ потребления Бендера" hint="Раз в ${cfg.sample_seconds || 10} с: процессор, память, задержка цикла — в памяти, без записи на диск. Зависание называет поток, который держал процессор." checked=${!!cfg.enabled} onChange=${(v) => save({ enabled: v })} disabled=${busy} />
    <${Toggle} label="Следить за всем узлом" hint="Весь процессор машины, память, подкачка и чужие процессы: кто грузит узел, даже если это не Бендер." checked=${!!cfg.node} onChange=${(v) => save({ node: v })} disabled=${busy || !cfg.enabled} />
    ${!cfg.enabled ? html`<p class="faint small">Выключено: ничего не читается.</p>` : html`
      <div style="margin-top:8px">${numbers(now) || html`<span class="faint">Замеров ещё нет: они идут, когда Бендер запущен через bender run.</span>`}</div>
      ${d.platform ? html`<div class="faint small">${d.platform}</div>` : null}
      <${Spark} samples=${d.samples} />
      ${d.top ? (d.top.length ? html`<table class="t small" data-load-top><tbody>${d.top.map((p) => html`<tr><td class="mono">${p.comm}</td><td class="faint">${p.pid}</td><td>${p.cpu_pct}%</td><td class="faint">${p.bender || ""}</td></tr>`)}</tbody></table>`
        : html`<div class="faint small" data-load-top>${cfg.node ? "За полсекунды процессор узла заметно никто не брал." : "Чужие процессы не читаются: «Следить за всем узлом» выключено."}</div>`) : null}`}
    <div style="margin-top:10px"><strong>Необычное</strong></div>
    ${d.anomalies?.length ? html`<ul class="small">${d.anomalies.slice(0, 8).map((a) => html`<li>${timeOf(a.started)} · ${a.title}: ${a.reason}${a.culprit ? html` · <span class="faint">виновник: ${a.culprit}</span>` : null}
        ${a.ended ? html` <span class="faint">(спала через ${Math.max(1, Math.round((a.ended - a.started) / 60))} мин)</span>` : html` <span class="badge amber">идёт</span>`}
        ${a.report ? html` <${Button} small onClick=${() => open(a.report.split("/").pop())}>Отчёт</${Button}>` : null}</li>`)}</ul>`
      : html`<div class="faint small">С запуска необычной нагрузки не было.</div>`}
    ${d.reports?.length ? html`<div class="faint small">Отчёты на диске (local/load): ${d.reports.slice(0, 5).map((name, i) => html`${i ? " · " : " "}<a href="#" onClick=${(e) => { e.preventDefault(); open(name); }}>${reportLabel(name)}</a>`)}</div>` : null}
    <div style="margin-top:10px"><strong>Заведующий узлом</strong></div>
    <p class="faint small">Агент стаи читает отчёт о необычной нагрузке, ищет причину и пишет тебе вывод. Сам ничего не завершает и не меняет; каждый разбор — ход модели (не больше ${cfg.manager_per_day} в сутки).${d.safe ? " В безопасном режиме не зовётся." : ""}</p>
    <div class="row wrap">
      <select class="input" style="width:auto" aria-label="Заведующий узлом" value=${cfg.manager || ""} disabled=${busy} onChange=${(e) => save({ manager: e.target.value })}>
        <option value="">никто</option>${agents.map((a) => html`<option value=${a.id}>${a.name}</option>`)}${known ? null : html`<option value=${cfg.manager}>${cfg.manager} (нет в стае)</option>`}</select>
      <select class="input" style="width:auto" aria-label="Когда звать заведующего" value=${cfg.manager_on || "all"} disabled=${busy || !cfg.manager} onChange=${(e) => save({ manager_on: e.target.value })}>
        ${MANAGER_ON.map(([v, label]) => html`<option value=${v}>${label}</option>`)}</select></div>
    <p class="faint small">Пороги, сообщения хозяину и сколько хранить отчётов — Настройки → Нагрузка. В Telegram: /load, /load report, /load manager.</p>
    ${report ? html`<${Modal} title=${"Отчёт о нагрузке · " + reportLabel(report.name)} wide onClose=${() => setReport(null)}><pre class="mono small" style="white-space:pre-wrap">${report.text}</pre></${Modal}>` : null}
  </div>`;
}

export function BodyScreen() {
  const [b, setB] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState(false);
  const load = () => { setBusy(true); return get("/api/body").then(setB, setErr).finally(() => setBusy(false)); };
  useEffect(() => { load(); }, []);
  if (!b) return html`<div class="page"><div class="page-inner"><${ErrorBox} error=${err} /><${Visor} mode="busy" /></div></div>`;
  const m = b.mood, mem = b.memory || {}, disk = b.disk, bud = b.budget;
  return html`<div class="page"><div class="page-inner">
    <${StartupStatus}/>
    <div class="card"><div class="card-title"><${Icon} name="activity" /><h3>Самочувствие</h3><${Button} small busy=${busy} icon="refresh" onClick=${load}>Обновить</${Button}></div>
      <div class="mood"><span class=${`badge ${MOOD_CLS[m.code]}`}>${m.word}</span> <span class="faint">режим: ${m.mode_word}</span></div>
      ${m.why.length ? html`<ul class="small">${m.why.map((w) => html`<li>${w}</li>`)}</ul>` : html`<div class="faint small">Всё в порядке.</div>`}
      <div class="faint small">Датчики тела читаются по запросу. Нагрузку считает сторож цикла раз в несколько секунд — в памяти, на диск только отчёт о необычном.</div>
    </div>
    <div class="grid2">
      <div class="card"><h3><${Icon} name="cpu" /> Машина</h3>
        <div>${b.os} · ${b.machine} · ${b.cpus} яд. · Python ${b.python}${b.termux ? " · Termux" : ""}</div>
        ${b.load ? html`<div class="faint small">Нагрузка: ${b.load.join(" / ")}</div>` : null}
        ${mem.total_mb ? html`<div style="margin-top:8px">Память: свободно ${mem.free_mb} из ${mem.total_mb} МБ<${Meter} value=${mem.total_mb - mem.free_mb} max=${mem.total_mb} warn=${0.9} /></div>` : null}
        ${disk ? html`<div style="margin-top:8px">Диск: свободно ${disk.free_gb} из ${disk.total_gb} ГБ<${Meter} value=${disk.total_gb - disk.free_gb} max=${disk.total_gb} warn=${0.9} /></div>` : null}
        ${b.battery ? html`<div style="margin-top:8px">Батарея: ${b.battery.percent}%${b.battery.charging ? " (заряжается)" : ""}</div>` : null}
        ${b.temp_c != null ? html`<div>Температура: ${b.temp_c} °C</div>` : null}
        <div class="faint small" style="margin-top:6px">Машина работает ${dur(b.uptime_machine_s)} · сеть: ${b.network?.default_route ? `есть (${b.network.interface})` : b.network && Object.keys(b.network).length ? "нет маршрута" : "—"}</div>
      </div>
      <div class="card"><h3><${Icon} name="robot" /> Я сам</h3>
        <div>Память процесса: ${b.self?.rss_mb} МБ · база: ${b.self?.db_mb ?? "—"} МБ</div>
        <div class="faint small">Работаю ${dur(b.uptime_bender_s)}</div>
        <div style="margin-top:8px">Деньги сегодня: ${money(bud.spent)}${bud.daily ? ` из ${money(bud.daily)}` : " (без лимита)"}</div>
        ${bud.daily ? html`<${Meter} value=${bud.spent} max=${bud.daily} />` : null}
        <div style="margin-top:8px">Модели:</div>
        ${b.models.length ? html`<ul class="small">${b.models.map((x) => html`<li>${x.ok ? "🟢" : "🟠"} ${x.label} <span class="faint">(${x.role === "main" ? "Главная" : "Быстрая"})${x.ok ? "" : ` — ${x.why}, ещё ${x.cooling_s} с`}</span></li>`)}</ul>` : html`<div class="faint small">не настроены</div>`}
      </div>
    </div>
    <${LoadCard} />
    ${b.top?.length ? html`<div class="card"><h3>Больше всех памяти едят</h3>
      <table class="t"><tbody>${b.top.map((p) => html`<tr><td class="mono">${p.name}</td><td class="faint">${p.pid}</td><td>${p.rss_mb} МБ</td></tr>`)}</tbody></table></div>` : null}
  </div></div>`;
}
