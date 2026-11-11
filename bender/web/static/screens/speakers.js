// Колонки (превью): список, состояние, поиск по mDNS и добавление без правки JSON.
import { html, useEffect, useState, Button, Toggle, ErrorBox, Icon } from "../lib/ui.js";
import { get, post } from "../lib/api.js";
import { useEvents, useApp } from "../lib/store.js";

const STATE = { ok: "на связи", off: "не подключена", connecting: "подключаюсь", error: "ошибка" };

export function SpeakersPanel() {
  const s = useApp();
  const [d, setD] = useState(null), [err, setErr] = useState(null), [busy, setBusy] = useState(false);
  const [found, setFound] = useState(null), [manual, setManual] = useState({ name: "", host: "", port: 6053 });
  const load = () => get("/api/speakers").then(setD, setErr);
  useEffect(() => { load(); }, []);
  useEvents((ev) => { if ((ev.type === "channel" && ev.channel === "speaker") || (ev.type === "settings" && ev.group === "speakers")) load(); });
  const save = async (values) => { setBusy(true); setErr(null); try { await post("/api/settings/speakers", values); await load(); } catch (e) { setErr(e); } setBusy(false); };
  const stations = d?.settings?.stations || [];
  const write = (list) => save({ stations: list });
  const add = (row) => write([...stations, { name: row.name || row.host, host: row.host, port: +row.port || 6053 }]);
  const discover = async () => { setBusy(true); setErr(null); try { setFound((await post("/api/speakers/discover", {})).found); } catch (e) { setErr(e); } setBusy(false); };
  const agents = (s.st?.agents || []).filter((a) => !a.archived);
  if (!d) return html`<div class="card"><h3>Колонки</h3><${ErrorBox} error=${err} /></div>`;
  return html`<div class="card"><div class="card-title"><${Icon} name="send" /><h3>Колонки <span class="badge amber">превью</span></h3></div>
    <p class="note">Колонка с Pulsar слушает и говорит через Бендера. Работает в домашней сети; опыт проверен на Станции Лайт 2.</p>
    <${ErrorBox} error=${err} />
    <${Toggle} label="Колонки включены" checked=${!!d.settings.enabled} onChange=${(v) => save({ enabled: v })} />
    ${stations.length ? stations.map((st, i) => {
      const live = d.stations.find((x) => x.name === st.name || x.host === st.host) || {};
      return html`<div class="list-item"><div class="grow"><strong>${st.name || st.host}</strong> <span class="faint">${st.host}:${st.port || 6053}</span>
        <div class="faint small">${STATE[live.state] || live.state || "—"}${live.notice ? " · " + live.notice : ""}${live.answering ? " · отвечает " + live.answering : ""}</div></div>
        <select class="input" style="width:auto" aria-label="Агент колонки" value=${st.agent_id || ""} onChange=${(e) => write(stations.map((x, j) => (j === i ? { ...x, agent_id: e.target.value || undefined } : x)))}>
          <option value="">агент по умолчанию</option>${agents.map((a) => html`<option value=${a.id}>${a.name}</option>`)}</select>
        <${Button} small onClick=${() => write(stations.filter((_, j) => j !== i))}>Убрать</${Button}></div>`;
    }) : html`<p class="faint">Колонок пока нет. Найди их в сети или впиши адрес.</p>`}
    <div class="row wrap"><${Button} small busy=${busy} onClick=${discover}>Найти в сети</${Button}></div>
    ${found ? (found.length ? found.map((row) => html`<div class="list-item"><div class="grow">${row.name} <span class="faint">${row.host}:${row.port}</span></div>
        ${row.known ? html`<span class="faint">уже в списке</span>` : html`<${Button} small onClick=${() => add(row)}>Добавить</${Button}>`}</div>`)
      : html`<p class="faint">Ничего не нашлось. Сеть может не пропускать mDNS: впиши адрес вручную.</p>`) : null}
    <div class="row wrap"><input class="input" placeholder="Имя" aria-label="Имя колонки" value=${manual.name} onInput=${(e) => setManual({ ...manual, name: e.target.value })} />
      <input class="input" placeholder="Адрес" aria-label="Адрес колонки" value=${manual.host} onInput=${(e) => setManual({ ...manual, host: e.target.value })} />
      <${Button} small disabled=${!manual.host.trim()} onClick=${() => { add({ ...manual, host: manual.host.trim() }); setManual({ name: "", host: "", port: 6053 }); }}>Добавить вручную</${Button}></div>
    <p class="faint small">Остальное — Настройки → Колонки: голос, пауза, продолжение разговора, подтверждения голосом.</p></div>`;
}
