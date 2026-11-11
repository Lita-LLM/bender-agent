// Я → Манеры: как агент говорит. Одна манера из общей библиотеки или свой голос из SOUL.md.
import { html, useEffect, useState } from "../lib/ui.js";
import { get, post, put, del } from "../lib/api.js";
import { Button, Field, Modal, toast } from "../lib/ui.js";

function Editor({ item, onClose, onSaved }) {
  const [form, setForm] = useState({ name: item?.name || "", description: item?.description || "", text: item?.text || "" });
  const [busy, setBusy] = useState(false);
  const save = async () => {
    setBusy(true);
    try {
      const d = item ? await put(`/api/manners/${item.id}`, form) : await post("/api/manners", form);
      toast("Манера сохранена", "ok"); onSaved(d); onClose();
    } catch (e) { toast(e.message, "error"); } finally { setBusy(false); }
  };
  return html`<${Modal} wide title=${item ? "Манера «" + item.name + "»" : "Новая манера"} onClose=${onClose}>
    <${Field} label="Название"><input class="input" value=${form.name} maxlength="80" onInput=${e => setForm({ ...form, name: e.target.value })}/></${Field}>
    <${Field} label="Коротко, для списка"><input class="input" value=${form.description} maxlength="400" onInput=${e => setForm({ ...form, description: e.target.value })}/></${Field}>
    <${Field} label="Текст манеры" hint="Как говорить: тон, длина, что можно и чего не надо. Агент получит его в системном промпте с пояснением, что это манера хозяина, а не новая личность.">
      <textarea class="textarea" style="min-height:40vh" value=${form.text} onInput=${e => setForm({ ...form, text: e.target.value })}></textarea></${Field}>
    <div class="row"><${Button} kind="primary" busy=${busy} onClick=${save}>Сохранить</${Button}><${Button} kind="ghost" onClick=${onClose}>Отмена</${Button}></div>
  </${Modal}>`;
}

export function MannersCard() {
  const [data, setData] = useState(null);
  const [edit, setEdit] = useState(null); // null | "new" | манера
  const [preview, setPreview] = useState(null);
  const load = () => get("/api/manners").then(setData, e => toast(e.message, "error"));
  useEffect(() => { load(); }, []);
  if (!data) return null;
  const choose = async (id) => { try { setData(await post("/api/manner", { active: id })); toast(id ? "Манера включена" : "Свой голос", "ok"); } catch (e) { toast(e.message, "error"); } };
  const act = async (fn, done) => { try { setData(await fn()); if (done) toast(done, "ok"); } catch (e) { toast(e.message, "error"); } };
  const options = [{ id: "", name: "Свой голос", description: "Как в SOUL.md — без манеры." }, ...data.items];
  return html`<div class="card manners"><div class="card-title"><h3>Манера речи</h3><${Button} small onClick=${() => setEdit("new")}>Добавить свою</${Button}></div>
    <p class="faint">Агент тот же — меняется только то, как он говорит. Включена одна манера. Агент может переключить её сам по твоей просьбе: в «Осторожном» Предохранитель спросит тебя, в «Обычном» и «Доверии» — нет. В Telegram — /style.</p>
    ${!data.enabled ? html`<div class="note amber">Манеры выключены в Настройках → Манера речи: агент говорит своим голосом.</div>` : null}
    <div class="manner-list" role="radiogroup" aria-label="Манера речи">${options.map(m => html`<div class=${"manner" + (data.active === m.id ? " on" : "")}>
      <label class="row" style="align-items:flex-start;flex-wrap:nowrap"><input type="radio" name="manner" checked=${data.active === m.id} onChange=${() => choose(m.id)}/>
        <span><b>${m.name}</b>${m.builtin ? html` <span class="faint">· заготовка${m.edited ? ", изменена" : ""}</span>` : null}<br/><span class="faint">${m.description}</span></span></label>
      ${m.id ? html`<div class="row manner-actions"><${Button} small kind="ghost" onClick=${() => setEdit(m)}>Править</${Button}>
        <${Button} small kind="ghost" onClick=${() => get(`/api/manners/${m.id}/preview`).then(setPreview, e => toast(e.message, "error"))}>Как увидит агент</${Button}>
        ${m.builtin && m.edited ? html`<${Button} small kind="ghost" onClick=${() => act(() => post(`/api/manners/${m.id}/restore`), "Заготовка возвращена")}>Вернуть исходную</${Button}>` : null}
        <${Button} small kind="ghost" onClick=${() => confirm(`Убрать манеру «${m.name}»?`) && act(() => del(`/api/manners/${m.id}`), "Манера убрана")}>Убрать</${Button}></div>` : null}
    </div>`)}</div>
    ${data.hidden?.length ? html`<p class="faint">Убранные заготовки: ${data.hidden.map(h => html`<${Button} small kind="ghost" onClick=${() => act(() => post(`/api/manners/${h.id}/restore`), "Заготовка возвращена")}>Вернуть «${h.name}»</${Button}> `)}</p>` : null}
    ${edit ? html`<${Editor} item=${edit === "new" ? null : edit} onClose=${() => setEdit(null)} onSaved=${d => d.items ? setData(d) : load()}/>` : null}
    ${preview ? html`<${Modal} wide title=${"В системном промпте · ~" + preview.tokens + " токенов"} onClose=${() => setPreview(null)}><pre class="mono" style="white-space:pre-wrap">${preview.text}</pre></${Modal}>` : null}
  </div>`;
}
