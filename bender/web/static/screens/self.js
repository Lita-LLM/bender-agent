// Личность и память: файлы, которые и есть агент. Хозяин может править любой.
import { html, useEffect, useState } from "../lib/ui.js";
import { get, put, post } from "../lib/api.js";
import { Button, ErrorBox, Tabs, Visor, toast } from "../lib/ui.js";
import { RelocateCard } from "./relocate.js";
import { MannersCard } from "./manners.js";

const FILES = [["identity", "Кто я"], ["soul", "Душа"], ["user", "Хозяин"], ["memory", "Память"], ["agents", "Правила"], ["heartbeat", "Пульс"], ["dreams", "Сны"]];
const HINT = {
  identity: "IDENTITY.md — имя, кто он, вайб, эмодзи. Агент заполняет сам при пробуждении.",
  soul: "SOUL.md — голос и характер. Агент находит его сам и развивает; меняя, говорит тебе.",
  user: "USER.md — директивы о тебе: «Всегда / Никогда / Предпочитай» с датой.",
  memory: "MEMORY.md — долговременная память, всегда у агента в голове. Ограничена по размеру — агент сам ужимает.",
  agents: "AGENTS.md — правила работы. Агент может только предложить правку, менять — тебе.",
  heartbeat: "HEARTBEAT.md — что проверять на пульсе.",
  dreams: "DREAMS.md — дневник снов (появится с вехой 3).",
};


function FaceEditor(){
 const [face,setFace]=useState(null),[imported,setImported]=useState([]);
 useEffect(()=>{get('/api/avatar').then(d=>{setFace(d.avatar);setImported(d.imported);});},[]);
 if(!face)return null;
 return html`<div class="card"><h3>Моё лицо</h3><${Visor} avatar=${face}/><p>Параметры хранятся в AVATAR.json рядом с IDENTITY.md. Агент предлагает их при пробуждении; ты можешь поправить.</p>
 <label>Свечение <input aria-label="Свечение" type="color" value=${face.color} onInput=${e=>setFace({...face,color:e.target.value})}/></label>
 ${[['shape','Форма',['visor','round','square'],['Визор','Круглая','Квадратная']],['eyes','Глаза',['round','slit','square'],['Круглые','Щели','Квадратные']]].map(([key,label,values,words])=>html`<label>${label}<select class="select" aria-label=${label} value=${face[key]} onChange=${e=>setFace({...face,[key]:e.target.value})}>${values.map((v,i)=>html`<option value=${v}>${words[i]}</option>`)}</select></label>`)}
 <${Button} onClick=${async()=>{try{await post('/api/avatar',face);toast('Лицо сохранено');}catch(e){toast(e.message,'error');}}}>Сохранить лицо</${Button}>
 <p><a href="#/settings">Тема, крупный шрифт, контраст и выключатель визора → Настройки → Лицо и тема</a></p>${imported.length?html`<p>Перенесены аватары OpenClaw: ${imported.join(', ')}. Оригиналы в self/avatars; внешний SVG не исполняется в пульте.</p>`:null}</div>`;
}

export function SelfScreen() {
  const [data, setData] = useState(null);
  const [tab, setTab] = useState("identity");
  const [text, setText] = useState("");
  const [err, setErr] = useState(null);
  const load = () => get("/api/self").then((d) => { setData(d); setText(d.files[tab] || ""); }, setErr);
  useEffect(() => { load(); }, []);
  useEffect(() => { if (data) setText(data.files[tab] || ""); }, [tab]);
  if (!data) return html`<div class="page"><div class="page-inner"><${ErrorBox} error=${err} /><${Visor} mode="busy" /></div></div>`;
  const dirty = text !== (data.files[tab] || "");
  return html`<div class="page"><div class="page-inner">
    ${!data.born ? html`<div class="note amber">Агент ещё не прошёл пробуждение — характера нет, имени нет. Напиши ему в «Разговоре».</div>` : null}
    ${data.relocating ? html`<div class="note amber">Агент только что переехал из OpenClaw. При первом разговоре он пройдёт Переселение: осмотрит новое тело, перечитает себя и поправит устаревшее.</div>` : null}
    <${FaceEditor}/><${MannersCard}/><${Tabs} value=${tab} tabs=${FILES} onChange=${setTab} />
    <div class="faint" style="margin:10px 0">${HINT[tab]}</div>
    <textarea aria-label="Текст файла личности" class="textarea mono" style="min-height:55vh" value=${text} onInput=${(e) => setText(e.target.value)}></textarea>
    <div class="row" style="margin-top:8px"><${Button} kind="primary" disabled=${!dirty} onClick=${async () => { try { await put(`/api/self/${tab}`, { text }); toast("Сохранено", "ok"); load(); } catch (e) { toast(e.message, "error"); } }}>Сохранить</${Button}>
      ${dirty ? html`<${Button} kind="ghost" onClick=${() => setText(data.files[tab] || "")}>Отменить</${Button}>` : null}</div>
    <div style="margin-top:22px"><${RelocateCard} compact /><${RelocateCard} compact kind="hermes" /><${RelocateCard} compact kind="claude" /></div>
  </div></div>`;
}
