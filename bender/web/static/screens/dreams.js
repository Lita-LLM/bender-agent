// Сны: что агент делал с памятью ночью. DREAMS.md пишется сам, для хозяина.
import { html, useEffect, useState } from "../lib/ui.js";
import { get, post } from "../lib/api.js";
import { mdToHtml } from "../lib/md.js";
import { useEvents } from "../lib/store.js";
import { Button, ErrorBox, Icon, Visor, timeAgo, toast } from "../lib/ui.js";

// Уборка ядра: сон или агент предлагает, хозяин отмечает и применяет. Без «Применить» ядро не меняется.
function TidyCard({ s, reload }) {
  const p = s.tidy;
  const [off, setOff] = useState({});
  const [busy, setBusy] = useState("");
  const act = (what, payload, done) => async () => {
    setBusy(what);
    try { const r = await post("/api/sleep/tidy", { action: what, ...payload }); if (done) toast(done(r), "ok"); setOff({}); reload(); }
    catch (e) { toast(e.message, "error"); } finally { setBusy(""); }
  };
  const keys = p ? p.items.filter((it) => !off[it.k]).map((it) => it.k) : [];
  return html`<div class="card"><div class="card-title"><${Icon} name="memory" /><h3>Уборка памяти</h3>
      <${Button} small busy=${busy === "propose"} onClick=${act("propose", {}, (r) => r.count ? `Предложено: ${r.count}` : "Убирать нечего")}>Предложить уборку</${Button}></div>
    <div class="faint">Сон предлагает уборку сам, когда MEMORY.md или USER.md заполнен на 85%${s.fat?.length ? ` — сейчас: ${s.fat.join(", ")}` : ""}. Ничего не меняется, пока ты не нажмёшь «Применить»; прежний вид остаётся в self/history/.</div>
    ${p ? html`<div style="margin-top:10px"><div class="faint">${new Date(p.at * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })} · ${p.reason}. Сними галочку с того, что надо оставить.</div>
      ${p.items.map((it) => html`<label class="row" style="align-items:flex-start;flex-wrap:nowrap;margin-top:6px"><input type="checkbox" checked=${!off[it.k]} onChange=${() => setOff({ ...off, [it.k]: !off[it.k] })}/>
        <span><span class="faint">${it.file} · ${it.op === "merge" ? "склеить" : "убрать"}</span><br/>
        ${it.op === "merge" ? html`${it.old.map((o) => html`<s>${o}</s><br/>`)}→ <b>${it.new}</b>` : html`<s>${it.old[0]}</s> <span class="faint">— ${it.why}</span>`}</span></label>`)}
      <div class="row" style="margin-top:10px"><${Button} kind="primary" small busy=${busy === "apply"} onClick=${act("apply", { id: p.id, keys }, (r) => `Применено: ${r.applied}`)}>Применить отмеченное (${keys.length})</${Button}>
        <${Button} small kind="ghost" busy=${busy === "reject"} onClick=${act("reject", {}, () => "Отклонено")}>Отклонить</${Button}></div></div>` : null}
  </div>`;
}

export function DreamsScreen() {
  const [s, setS] = useState(null);
  const [err, setErr] = useState(null);
  const [busy, setBusy] = useState("");
  const [dry, setDry] = useState(null);
  const load = () => get("/api/sleep").then(setS, setErr);
  useEffect(() => { load(); }, []);
  useEvents((ev) => { if (ev.type === "sleep" || ev.type === "tidy") load(); }, []);
  if (!s) return html`<div class="page"><div class="page-inner"><${ErrorBox} error=${err} /><${Visor} mode="busy" /></div></div>`;
  const run = (isDry) => async () => {
    setBusy(isDry ? "dry" : "real");
    try {
      const r = await post("/api/sleep", { dry: isDry });
      if (isDry) setDry(r); else { toast(r.status === "nothing" ? "Нового не было — сон не нужен" : `Поспал: запомнил ${r.promoted.length}`, "ok"); setDry(null); }
      load();
    } catch (e) { toast(e.message, "error"); } finally { setBusy(""); }
  };
  const body = (s.dreams || "").replace(/^# .*\n+(Что агент видел во сне.*\n+)?/, "");
  return html`<div class="page"><div class="page-inner">
    <div class="card"><div class="card-title"><${Icon} name="moon" /><h3>Сон</h3>
      <${Button} small busy=${busy === "dry"} onClick=${run(true)}>Вхолостую</${Button}>
      <${Button} small kind="primary" busy=${busy === "real"} onClick=${run(false)}>Уснуть сейчас</${Button}></div>
      <div class="faint">Ночью агент перебирает день: что нового, что пригождалось, что запомнить надолго, что ужать. Модель только предлагает — что записать, решают проверки (дубли, секреты, лимит).</div>
      <div style="margin-top:8px">${!s.enabled ? html`<span class="badge">выключен</span>` : html`<span class="badge green">включён</span>`}
        <span class="faint"> · последний сон: ${s.last_at ? timeAgo(s.last_at) : "ещё не было"}${s.next_at ? ` · следующий: ${new Date(s.next_at * 1000).toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })}` : ""}
        ${s.tried && s.tried.at > (s.last_at || 0) + 60 ? ` · последняя попытка ${timeAgo(s.tried.at)}: ${{ nothing: "нового не было", missed: "не уснул до 06:00", error: "ошибка" }[s.tried.status] || "поспал"}` : ""}
        ${" "}· нового для сна: ${s.has_new ? "есть" : "нет"} · модель: роль «${{ dreamer: "Сновидец", fast: "Быстрая", main: "Главная" }[s.role] || s.role}» · <a href="#/settings">настройки сна</a></span></div>
      ${s.enabled && !s.heart ? html`<div class="note amber" style="margin-top:10px">Сердце выключено — без него сон сам не наступит. Включи его на экране «Сердце» или нажми «Уснуть сейчас».</div>` : null}
      ${dry ? html`<div class="note" style="margin-top:10px"><b>Вхолостую — ничего не записано.</b>
        ${dry.status === "nothing" ? html`<div>Нового с прошлого сна нет.</div>` : html`
        ${dry.dream?.story ? html`<p><i>${dry.dream.story}</i></p>` : null}
        ${dry.promoted?.length ? html`<div>Запомнил бы:</div><ul>${dry.promoted.map((p) => html`<li>${p.text} → ${p.to}</li>`)}</ul>` : html`<div>Закреплять нечего.</div>`}
        ${dry.skipped?.length ? html`<div class="faint">Не стал бы: ${dry.skipped.map((p) => `${p.text} (${p.why})`).join("; ")}</div>` : null}
        ${dry.rewrote ? html`<div>Ядро: ${dry.rewrote}</div>` : null}
        ${dry.errors?.length ? html`<div class="red">${dry.errors.join("; ")}</div>` : null}`}</div>` : null}
    </div>
    <${TidyCard} s=${s} reload=${load} />
    <div class="card"><div class="card-title"><${Icon} name="book" /><h3>DREAMS.md</h3></div>
      ${body.trim() ? html`<div class="m-text dreams" dangerouslySetInnerHTML=${{ __html: mdToHtml(body) }}></div>` : html`<div class="faint">Снов ещё не было.</div>`}
    </div>
  </div></div>`;
}
