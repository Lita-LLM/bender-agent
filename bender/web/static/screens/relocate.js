// Переезд из OpenClaw: найти установку → план с галочками → перенос → отчёт.
import { html, useEffect, useState } from "../lib/ui.js";
import { PendingCards } from "./chat.js";
import { get, post } from "../lib/api.js";
import { refresh, setState, useApp } from "../lib/store.js";
import { Button, ErrorBox, Field, Icon, Modal, Toggle, Visor, toast } from "../lib/ui.js";
import {switchAgent} from './agents.js';

const GROUPS = [
  ["self", "Личность и память"], ["diary", "Дневник"], ["skill", "Навыки"], ["avatar", "Аватары"], ["work", "Файлы из рабочей папки"],
  ["provider", "Модели: провайдеры"], ["role", "Модели: роли"], ["setting", "Настройки"], ["session", "История разговоров"],
  ["secret", "Секреты"], ["archive", "В архив — заберут будущие вехи"], ["ritual", "Переселение"],
];
const WORDS = { replace: "заменить", append: "дописать", set: "записать", copy: "скопировать", create: "создать", merge: "добавить к своему", archive: "в архив", skip: "пропустить" };

function PlanItem({ it, on, secrets, onToggle }) {
  const skip = it.action === "skip";
  const off = skip || !on || (it.kind === "secret" && !secrets);
  return html`<label class=${`pi${off ? " off" : ""}`}>
    <input type="checkbox" checked=${!skip && on} disabled=${skip || (it.kind === "secret" && !secrets)} onChange=${(e) => onToggle(it.id, e.target.checked)} />
    <div class="pi-main">
      <div class="pi-title">${it.title}${it.secret && secrets ? html` <span class="badge amber" title="Содержит ключ или токен"><${Icon} name="key" /> ключ</span>` : null} <span class="faint">— ${WORDS[it.action] || it.action}</span></div>
      ${it.detail ? html`<pre class="pi-sub mono module-text">${it.detail}</pre>` : null}
      ${it.note ? html`<div class="pi-sub">↳ ${it.note}</div>` : null}
    </div></label>`;
}

export function RelocateFlow({ onDone, kind = "openclaw" }) {
  const name = ({openclaw: "OpenClaw", hermes: "Hermes", claude: "Claude Code"})[kind];
  const endpoint = `/api/import/${kind}`;
  const [stage, setStage] = useState("find");
  const [installs, setInstalls] = useState(null);
  const [path, setPath] = useState("");
  const [agent, setAgent] = useState("");
  const [mode,setMode]=useState('new');
  const [profiles,setProfiles]=useState({});
  const [plan, setPlan] = useState(null);
  const [on, setOn] = useState({});
  const [secrets, setSecrets] = useState(true);
  const [rep, setRep] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);

  const find = async (p) => {
    setBusy(true); setErr(null);
    try {
      const r = await get(endpoint + (p ? `?path=${encodeURIComponent(p)}` : ""));
      setInstalls(r.installs);
      if (r.installs.length) { setPath(r.installs[0].path); setAgent(r.installs[0].agents.length>1?'*':(r.installs[0].agents[0]?.id||'')); }
    } catch (e) { setErr(e); setInstalls([]); }
    setBusy(false);
  };
  useEffect(() => { find(""); }, []);

  const makePlan = async (chosen=profiles) => {
    setBusy(true); setErr(null);
    try {
      const p = await post(endpoint + "/plan", { path, agent, mode, profiles:chosen });
      setProfiles(chosen);
      setPlan(p);
      setOn(Object.fromEntries(p.items.map((i) => [i.id, i.selected && i.action !== "skip"])));
      setStage("plan");
    } catch (e) { setErr(e); }
    setBusy(false);
  };
  const run = async () => {
    const confirmed=mode!=='current'||confirm('Заменить данные выбранного текущего агента? Перед переносом будет сохранён снимок.');
    if(!confirmed)return;
    setStage("running"); setErr(null);
    try {
      const items = plan.items.filter((i) => on[i.id] && i.action !== "skip" && (i.kind !== "secret" || secrets)).map((i) => i.id);
      const r = await post(endpoint + "/apply", { path, agent, items, secrets, hash: plan.hash, mode, profiles, confirmed:mode==='current'&&confirmed });
      setRep(r); setStage("done");
      refresh().catch(() => {});
    } catch (e) { setErr(e); setStage("plan"); }
  };

  if (stage === "find") {
    const inst = (installs || []).find((i) => i.path === path);
    return html`<div>
      <p class="muted" style="margin-top:0">Бендер прочитает ${name} и покажет, что и куда перенесёт. По умолчанию создаётся новый агент; действующая память сохраняется. Источник только читается.</p>
      <${Field} label="Куда переносить"><select aria-label="Назначение переезда" class="select" value=${mode} onChange=${e=>setMode(e.target.value)}><option value="new">В новых агентов</option><option value="current">Заменить текущего агента</option></select></${Field}>
      ${installs === null ? html`<${Visor} mode="busy" />` : installs.length ? html`<div>
        ${installs.length > 1 ? html`<${Field} label="Установка"><select class="select" value=${path} onChange=${(e) => { setPath(e.target.value); const i = installs.find((x) => x.path === e.target.value); setAgent((i?.agents.find((a) => a.default) || i?.agents[0] || {}).id || ""); }}>
          ${installs.map((i) => html`<option value=${i.path}>${i.path} ${i.version}</option>`)}</select></${Field}>`
          : html`<div class="note green">Нашёл ${name} ${inst?.version || ""}: <span class="mono">${path}</span></div>`}
        ${inst?.config_error ? html`<div class="note amber">Конфиг не разобрался: ${inst.config_error}. Личность и история перенесутся, модели — нет.</div>` : null}
        ${inst && inst.agents.length > 1 ? html`<${Field} label="Кого переселяем" hint="Каждый станет отдельным агентом со своей памятью.">
          <select class="select" value=${agent} onChange=${(e) => setAgent(e.target.value)}><option value="*">Всех агентов</option>${inst.agents.map((a) => html`<option value=${a.id}>${a.name} (${a.id})${a.default ? " — по умолчанию" : ""}</option>`)}</select></${Field}>` : null}
      </div>` : html`<div class="note amber">${name} не нашёлся в обычных местах. Укажи папку его состояния.</div>`}
      <${Field} label=${installs && installs.length ? "Или другая папка" : `Папка ${name}`} hint=${kind === "openclaw" ? "Там, где лежат openclaw.json и state/" : kind === "hermes" ? "~/.hermes или папка отдельного профиля" : "~/.claude или папка проекта с CLAUDE.md"}>
        <div class="row"><input aria-label="Папка источника переезда" class="input mono grow" placeholder=${`~/.${kind === "claude" ? "claude" : kind}`} onKeyDown=${(e) => { if (e.key === "Enter") find(e.target.value.trim()); }} id="oc-path" />
          <${Button} onClick=${() => find(document.getElementById("oc-path").value.trim())} busy=${busy && installs !== null}>Искать</${Button}></div></${Field}>
      <${ErrorBox} error=${err} onClose=${() => setErr(null)} />
      <${Button} kind="primary" icon="eye" disabled=${!path} busy=${busy} onClick=${()=>makePlan()}>Посмотреть план</${Button}>
    </div>`;
  }

  if (stage === "plan" && plan) {
    const toggle = (id, v) => setOn({ ...on, [id]: v });
    const count = plan.items.filter((i) => on[i.id] && i.action !== "skip" && (i.kind !== "secret" || secrets)).length;
    const hasSecrets = plan.items.some((i) => i.secret && i.action !== "skip");
    const active = plan.items.filter((i) => i.action !== "skip");
    const skipped = plan.items.filter((i) => i.action === "skip");
    return html`<div>
      <div class="note">Откуда: <span class="mono">${plan.source.path}</span>${plan.source.version ? ` · OpenClaw ${plan.source.version}` : ""} · агент <b>${plan.agent.name}</b></div>
      <div class="note green">${plan.mode==='new'?'Новые агенты. Текущий остаётся на месте.':'Явная замена выбранного агента.'}${(plan.destinations||[]).map(d=>html`<div>${d.name} → ${d.destination==='new'?'новый агент':'текущий агент'}</div>`)}</div>
      ${kind==='openclaw'&&plan.mode==='new'?(plan.destinations||[]).map(d=>html`<${Field} label=${'Шаблон прав: '+d.name}><select class="select" disabled=${busy} value=${d.profile} onChange=${e=>makePlan({...profiles,[d.source_agent]:e.target.value})}><option value="full">Полный</option><option value="helper">Помощник</option><option value="observer">Наблюдатель</option><option value="reader">Чтец</option></select><p class="faint">Полный: команды, сеть и SSH. Дополнительным агентам по умолчанию выдаётся Помощник.</p></${Field}>`):null}
      ${hasSecrets ? html`<${Toggle} label="Переносить ключи и токены" hint="Ключи моделей и токены лягут в сейф Бендера (файл с правами 600). В модель и в пульт они не попадают." checked=${secrets} onChange=${setSecrets} />` : null}
      ${skipped.length ? html`<div class="note">${active.length ? `Новое: ${active.length}` : "Всё уже на месте"} · уже перенесено или пропускается: ${skipped.length}</div>` : null}
      ${GROUPS.map(([kind, title]) => {
        const items = active.filter((i) => i.kind === kind);
        if (!items.length) return null;
        return html`<div class="section-title">${title}</div><div class="pi-list">${items.map((it) => html`<${PlanItem} key=${it.id} it=${it} on=${!!on[it.id]} secrets=${secrets} onToggle=${toggle} />`)}</div>`;
      })}
      ${skipped.length ? html`<details class="note"><summary>Уже перенесено или пропускается (${skipped.length})</summary>
        <div class="pi-list" style="margin-top:8px">${skipped.map((it) => html`<${PlanItem} key=${it.id} it=${it} on=${false} secrets=${secrets} onToggle=${toggle} />`)}</div></details>` : null}
      ${plan.warnings.length ? html`<div class="note amber"><b>Предупреждения</b>${plan.warnings.map((w) => html`<div>• ${w}</div>`)}</div>` : null}
      <${ErrorBox} error=${err} onClose=${() => setErr(null)} />
      <div class="row" style="margin-top:12px">
        <${Button} kind="primary" icon="move" disabled=${!count} onClick=${run}>${count ? `Переселить (${count})` : "Переносить нечего"}</${Button}>
        <${Button} kind="ghost" onClick=${() => setStage("find")}>Назад</${Button}></div>
    </div>`;
  }

  if (stage === "running") {
    return html`<div class="empty"><div style="font-size:2.4rem"><${Visor} mode="busy" /></div><h2>Переселяю…</h2><p class="muted">Снимок, личность, дневник, модели, история. Обычно это секунды.</p></div>`;
  }

  if (stage === "done" && rep) {
    return html`<div>
      <div class=${`note ${rep.errors.length ? "amber" : "green"}`}><b>Перенесено: ${rep.done.length}</b>${rep.counts?.messages ? ` · сообщений ${rep.counts.messages}` : ""}${rep.errors.length ? ` · ошибок ${rep.errors.length}` : ""}</div>
      ${rep.errors.map((e) => html`<div class="note red">${e}</div>`)}
      ${rep.body?.length ? html`<div class="note"><b>Старое и новое тело</b>${rep.body.map((b) => html`<div>${b}</div>`)}</div>` : null}
      ${rep.warnings.length ? html`<details class="note"><summary>Предупреждения (${rep.warnings.length})</summary>${rep.warnings.map((w) => html`<div>• ${w}</div>`)}</details>` : null}
      <details class="note"><summary>Что перенесено</summary>${rep.done.map((d) => html`<div>✓ ${d}</div>`)}</details>
      <div class="faint" style="margin:8px 0">Снимок Бендера до переезда и отчёт: <span class="mono">${rep.dir}</span></div>
      ${(rep.agents||[]).map(a=>html`<div class="row"><span>${a.name}</span><${Button} onClick=${()=>{location.hash='#/';switchAgent(a.id);}}>К агенту</${Button}></div>`)}
      <${Button} kind="primary" icon="chat" onClick=${() => { onDone && onDone(); location.hash = "#/"; if(plan?.mode==='new'&&rep.agents?.length===1)switchAgent(rep.agents[0].id); }}>К разговору</${Button}>
    </div>`;
  }
  return null;
}

export function RelocateCard({ compact, kind = "openclaw" }) {
  const name = ({openclaw: "OpenClaw", hermes: "Hermes", claude: "Claude Code"})[kind];
  return html`<div class="card">
    <div class="card-title"><${Icon} name="move" /><h3>Перенести из ${name}</h3></div>
    <div class="muted">${kind !== "openclaw" ? "Новый агент с выбранными заметками и историей. Источник остаётся на месте, задания не запускаются автоматически." : "Личность, память, дневник, навыки, модели с ключами и история — в новых агентов. Текущий сохраняется; замена доступна отдельным явным выбором."}</div>
    <div style="margin-top:10px"><${Button} icon="move" onClick=${() => setState({ relocate: kind })}>Переселить</${Button}></div>
  </div>`;
}

// Окно переезда живёт на уровне всего пульта: после переноса экран приветствия сменяется
// разговором, а отчёт должен остаться на месте.
export function RelocateModal() {
  const { relocate } = useApp();
  if (!relocate) return null;
  const close = () => { setState({ relocate: false }); refresh().catch(() => {}); };
  const kind = typeof relocate === "string" ? relocate : "openclaw";
  return html`<${Modal} wide title=${"Переезд из " + ({openclaw: "OpenClaw", hermes: "Hermes", claude: "Claude Code"})[kind]} onClose=${close}><${PendingCards} /><${RelocateFlow} kind=${kind} onDone=${() => { close(); toast("Переселение готово", "ok"); }} /></${Modal}>`;
}
