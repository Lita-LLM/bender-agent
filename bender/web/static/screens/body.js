// Тело: машина, на которой живёт агент, и он сам. Самочувствие меняет поведение (реже пульс, бесплатные модели).
import { html, useEffect, useState } from "../lib/ui.js";
import { get } from "../lib/api.js";
import { Button, ErrorBox, Icon, Visor, money } from "../lib/ui.js";
import {StartupStatus} from '../lib/lifecycle.js';

const MOOD_CLS = { ok: "green", tired: "amber", hungry: "amber", sick: "red", hot: "red", drained: "red" };

function Meter({ value, max, warn = 0.8 }) {
  const f = Math.min(1, value / Math.max(1e-9, max));
  return html`<div class="meter"><i class=${f >= 1 ? "red" : f >= warn ? "amber" : ""} style=${`width:${Math.round(f * 100)}%`}></i></div>`;
}

function dur(s) {
  if (s == null) return "—";
  const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60);
  return d ? `${d} дн ${h} ч` : h ? `${h} ч ${m} мин` : `${m} мин`;
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
      <div class="faint small">Датчики читаются только по запросу — тело ничего не опрашивает в фоне.</div>
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
    ${b.top?.length ? html`<div class="card"><h3>Больше всех памяти едят</h3>
      <table class="t"><tbody>${b.top.map((p) => html`<tr><td class="mono">${p.name}</td><td class="faint">${p.pid}</td><td>${p.rss_mb} МБ</td></tr>`)}</tbody></table></div>` : null}
  </div></div>`;
}
