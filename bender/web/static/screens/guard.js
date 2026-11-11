// Права: Предохранитель — профиль, жёсткий список, песочница, правила хозяина, проверка без выполнения, снимки.
import { html, useEffect, useState } from "../lib/ui.js";
import { del, get, post } from "../lib/api.js";
import { refresh } from "../lib/store.js";
import { Button, ErrorBox, Field, Icon, IconButton, Seg, Toggle, Visor, timeAgo, toast } from "../lib/ui.js";
import { PendingCards } from "./chat.js";

const PROFILES = [["careful", "Осторожный"], ["normal", "Обычный"], ["trust", "Доверие"], ["off", "Выключен"]];
const PROFILE_HINT = {
  careful: "Команды вне песочницы и правки вне рабочей папки — с вопросом. По умолчанию.",
  normal: "Команды в песочнице и правки в доме — без вопросов. Вне песочницы — с вопросом.",
  trust: "Почти всё без вопросов. Остаются: жёсткий список, защищённые места, необратимое и действия после чтения недоверенного.",
  off: "Предохранитель ничего не останавливает — только пишет бортжурнал. Как OpenClaw из коробки.",
};

export function GuardScreen() {
  const [g, setG] = useState(null);
  const [err, setErr] = useState(null);
  const [check, setCheck] = useState({ capability: "exec", target: "rm -rf build", tainted: false, unattended: false });
  const [verdict, setVerdict] = useState(null);
  const [rule, setRule] = useState({ capability: "exec", target: "", verdict: "allow", note: "" });
  const load = () => get("/api/guard").then(setG, setErr);
  useEffect(() => { load(); }, []);
  if (!g) return html`<div class="page"><div class="page-inner"><${ErrorBox} error=${err} /><${Visor} mode="busy" /></div></div>`;
  const s = g.settings;
  const setS = async (changes) => {
    try { await post("/api/settings/guard", changes); await load(); refresh(); } catch (e) { toast(e.message, "error"); }
  };
  const doCheck = async () => { try { setVerdict(await post("/api/guard/check", check)); } catch (e) { setVerdict({ error: e }); } };
  const addRule = async () => { try { await post("/api/guard/rules", rule); setRule({ ...rule, target: "", note: "" }); load(); } catch (e) { toast(e.message, "error"); } };
  return html`<div class="page"><div class="page-inner">
    <${PendingCards} />
    <div class="card"><div class="card-title"><${Icon} name="shield" /><h3>Предохранитель</h3>
      ${g.halted ? html`<${Button} kind="green" small onClick=${async () => { await post("/api/resume"); load(); refresh(); }}>Снять стоп</${Button}>` : html`<${Button} kind="danger" small icon="power" onClick=${async () => { await post("/api/halt"); load(); refresh(); }}>Стоп-кран</${Button}>`}</div>
      <div class="note">Одна граница для всех действий агента: тулы, команды, сеть, сообщения, память, настройки. Модель могут обмануть — вред ограничивает механика, а не просьбы.</div>
      <div class="note">Действующий профиль: <b>${PROFILES.find(([id])=>id===g.effective.profile)?.[1]||g.effective.profile}</b>, включён: ${g.effective.enabled?'да':'нет'}; ${g.profile_source}. <a href="#/rights">Стая → Права</a>.</div>
      <${Toggle} label="Предохранитель включён" checked=${s.enabled} onChange=${(v) => setS({ enabled: v })} hint="Выключенный только пишет бортжурнал." />
      <${Field} label="Профиль" hint=${PROFILE_HINT[s.profile]}><${Seg} value=${s.profile} options=${PROFILES} onChange=${(v) => setS({ profile: v })} /></${Field}>
      <${Toggle} label="Жёсткий список" checked=${s.hardline} onChange=${(v) => { if (v || confirm("Выключить жёсткий список? Тогда агент сможет выполнить даже rm -rf / или форматирование диска.")) setS({ hardline: v }); }}
        hint="Неотменяемые запреты: удаление корня и дома, форматирование и перезапись дисков, бомбы процессов." />
      <${Toggle} label="Снимки перед правками" checked=${s.checkpoints} onChange=${(v) => setS({ checkpoints: v })} hint="Перед перезаписью и правкой файла — копия для отката." />
      <${Field} label="Песочница команд" hint=${g.sandbox.title}><${Seg} value=${s.sandbox} options=${[["auto", "Авто"], ["off", "Без песочницы"]]} onChange=${(v) => setS({ sandbox: v })} /></${Field}>
      <${Toggle} label="Лимиты дочерних процессов" checked=${s.resource_limits} onChange=${v => setS({ resource_limits: v })} hint="Linux и macOS; действуют для новых команд, навыков и MCP, даже при выключенной песочнице." />
      ${[["cpu_seconds", "Процессор на процесс, секунд"], ["memory_mb", "Виртуальная память процесса, МБ"], ["process_count", "Процессов на команду"], ["file_mb", "Размер одного файла, МБ"]].map(([key, label]) => html`<${Field} label=${label}><input class="input" type="number" value=${s[key]} onChange=${e => setS({ [key]: e.target.value })} /></${Field}>`)}
      <${Field} label="Ждать ответа на карточку, секунд"><input class="input" type="number" value=${s.approval_timeout} onChange=${(e) => setS({ approval_timeout: e.target.value })} /></${Field}>
    </div>

    <div class="card"><div class="card-title"><${Icon} name="question" /><h3>Что решит Предохранитель?</h3></div>
      <div class="row"><select aria-label="Проверяемая способность" class="select" style="width:auto" value=${check.capability} onChange=${(e) => setCheck({ ...check, capability: e.target.value })}>${Object.entries(g.caps).map(([k, v]) => html`<option value=${k}>${v}</option>`)}</select>
        <input aria-label="команда, путь, адрес" class="input mono grow" value=${check.target} onInput=${(e) => setCheck({ ...check, target: e.target.value })} placeholder="команда, путь, адрес" /></div>
      <div class="row" style="margin-top:8px"><${Toggle} label="после недоверенного" checked=${check.tainted} onChange=${(v) => setCheck({ ...check, tainted: v })} />
        <${Toggle} label="хозяина рядом нет" checked=${check.unattended} onChange=${(v) => setCheck({ ...check, unattended: v })} />
        <${Button} small onClick=${doCheck}>Проверить</${Button}></div>
      ${verdict ? verdict.error ? html`<${ErrorBox} error=${verdict.error} />` : html`<div class=${`note ${verdict.verdict === "allow" ? "green" : verdict.verdict === "ask" ? "amber" : "red"}`}>
        <b>${{ allow: "Разрешить", ask: "Спросить хозяина", deny: "Запретить" }[verdict.verdict]}</b> — ${verdict.reason} <span class="faint">(${verdict.rule})</span></div>` : null}
    </div>

    <div class="card"><div class="card-title"><${Icon} name="list" /><h3>Правила хозяина</h3></div>
      ${!g.rules.length ? html`<div class="faint">Правил нет. Они появляются из карточек («всегда такое») или добавляются здесь.</div>` : null}
      <div class="list">${g.rules.map((r) => html`<div class="list-item"><span class=${`badge ${r.verdict === "allow" ? "green" : "red"}`}>${r.verdict === "allow" ? "можно" : "нельзя"}</span>
        <div class="li-main"><div class="li-title">${g.caps[r.capability] || r.capability}: <span class="mono">${r.target}</span></div><div class="li-sub">${r.note || ""} ${r.only_clean ? "· не действует после недоверенного" : ""}</div></div>
        <${IconButton} small icon="trash" danger title="Удалить" onClick=${async () => { await del(`/api/guard/rules/${r.id}`); load(); }} /></div>`)}</div>
      <div class="row" style="margin-top:10px"><select aria-label="Способность правила" class="select" style="width:auto" value=${rule.capability} onChange=${(e) => setRule({ ...rule, capability: e.target.value })}>${Object.entries(g.caps).map(([k, v]) => html`<option value=${k}>${v}</option>`)}</select>
        <input aria-label="шаблон: npm test* или /home/я/проекты/*" class="input mono grow" value=${rule.target} placeholder="шаблон: npm test* или /home/я/проекты/*" onInput=${(e) => setRule({ ...rule, target: e.target.value })} />
        <${Seg} value=${rule.verdict} options=${[["allow", "можно"], ["deny", "нельзя"]]} onChange=${(v) => setRule({ ...rule, verdict: v })} />
        <${Button} small kind="primary" disabled=${!rule.target} onClick=${addRule}>Добавить</${Button}></div>
    </div>

    <div class="card"><div class="card-title"><${Icon} name="history" /><h3>Снимки файлов</h3></div>
      ${!g.checkpoints.length ? html`<div class="faint">Снимков пока нет.</div>` : null}
      <div class="list">${g.checkpoints.map((c) => html`<div class="list-item"><div class="li-main"><div class="li-title mono">${c.path}</div><div class="li-sub">${c.reason} · ${timeAgo(c.ts)} · ${c.size} байт${c.skipped ? ` · ${c.skipped}` : ""}</div></div>
        ${!c.skipped ? html`<${Button} small onClick=${async () => { if (confirm(`Вернуть ${c.path} как было?`)) { try { await post(`/api/checkpoints/${c.id}/restore`); toast("Возвращено", "ok"); } catch (e) { toast(e.message, "error"); } } }}>Вернуть</${Button}>` : null}</div>`)}</div>
    </div>
  </div></div>`;
}
