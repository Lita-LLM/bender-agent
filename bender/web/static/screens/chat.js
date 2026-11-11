// Разговор с агентом: поток ответа, тулы, размышления, план, карточки одобрения и вопросы.
import { html, useEffect, useRef, useState } from "../lib/ui.js";
import { get, patch, post } from "../lib/api.js";
import { mdToHtml } from "../lib/md.js";
import { refresh, setState, state, useApp, useEvents } from "../lib/store.js";
import { AutoTextarea, Button, ErrorBox, Icon, IconButton, Modal, Visor, money, timeOf, toast } from "../lib/ui.js";
import { VoiceControls,SpeechButton } from "./voice.js";
import { SetupModel } from "./models.js";
import { RelocateCard } from "./relocate.js";

const CHANNEL_NAMES = { agent:'переписка',pult:'пульт',telegram: "Telegram", cron: "задача", heartbeat: "пульс", terminal: "терминал", "полигон": "Полигон", mail: "почта", hook: "вебхук", api: "API" };
const ACTOR_NAMES = { schedule: "По расписанию", mail: "Почта", hook: "Вебхук", team: "Команда" };

function BotFace({ busy }) {
  return html`<div class="bot-face"><${Visor} mode=${busy ? "busy" : ""} /></div>`;
}

export function ApprovalCard({ card }) {
  const [busy, setBusy] = useState(false);
  const answer = async (choice) => {
    setBusy(true);
    try { await post(`/api/approvals/${card.id}`, { choice }); } catch (e) { toast(e.message, "error"); }
    setBusy(false);
  };
  return html`<div class="approval">
    <div class="a-title"><${Icon} name="shield" /> Предохранитель спрашивает</div>
    <div style="margin-top:4px"><b>${card.summary || card.what}</b></div>
    ${card.target && card.target !== card.summary ? html`<div class="a-target">${card.target}</div>` : null}
    <div class="a-reason">${card.reason}</div>
    ${(card.capability?.startsWith("mcp.") || card.capability === "skill.run") && card.detail ? html`<details open><summary>Что будет передано или запущено</summary><pre class="mono" style="white-space:pre-wrap;overflow-wrap:anywhere">${JSON.stringify(card.detail, null, 2)}</pre></details>` : null}
    ${card.tainted ? html`<div class="a-taint">⚠ В этом ходе агент читал недоверенное (сеть, чужие сообщения, внешние тулы) — его могли обмануть.</div>` : null}
    <div class="row">
      <${Button} kind="primary" small busy=${busy} onClick=${() => answer("once")}>Разрешить один раз</${Button}>
      ${card.can_session !== false ? html`<${Button} small disabled=${busy} title="Эту способность — без вопросов до конца разговора (/new или перезапуск)" onClick=${() => answer("task")}>В этом разговоре</${Button}>` : null}
      ${card.can_remember ? html`<${Button} small disabled=${busy} title="Запомнить правилом; убрать — Предохранитель → Правила хозяина" onClick=${() => answer("always")}>Всегда: ${card.remember || "такое"}</${Button}>` : null}
      <${Button} kind="danger" small disabled=${busy} onClick=${() => answer("deny")}>Нет</${Button}>
    </div>
  </div>`;
}

export function QuestionCard({ card }) {
  const [text, setText] = useState("");
  const send = async (answer) => {
    try { await post(`/api/questions/${card.id}`, { answer }); } catch (e) { toast(e.message, "error"); }
  };
  return html`<div class="approval" style="border-color:var(--blue);background:var(--blue-soft)">
    <div class="a-title"><${Icon} name="question" /> Агент спрашивает</div>
    <div style="margin:6px 0">${card.question}</div>
    <div class="row">${(card.options || []).map((o) => html`<${Button} small onClick=${() => send(o)}>${o}</${Button}>`)}</div>
    <div class="row"><input class="input grow" value=${text} placeholder="Свой ответ…" onInput=${(e) => setText(e.target.value)}
      onKeyDown=${(e) => { if (e.key === "Enter" && text.trim()) send(text.trim()); }} />
      <${Button} small kind="primary" disabled=${!text.trim()} onClick=${() => send(text.trim())}>Ответить</${Button}></div>
  </div>`;
}

export function PendingCards() {
  const { pending } = useApp();
  if (!pending.length) return null;
  return html`<div>${pending.map((c) => c.kind === "question" || c.question ? html`<${QuestionCard} key=${c.id} card=${c} />` : html`<${ApprovalCard} key=${c.id} card=${c} />`)}</div>`;
}

function ToolCall({ call, result }) {
  const ok = result ? result.meta?.ok !== false : true;
  const summary = result?.meta?.summary || (result ? "" : "выполняется…");
  const taint = result?.meta?.tainted;
  return html`<details class=${`toolcall${!ok ? " err" : ""}${taint ? " taint" : ""}`}>
    <summary>${result ? (ok ? html`<span style="color:var(--green)">✓</span>` : html`<span style="color:var(--red)">✗</span>`) : html`<${Visor} mode="busy" />`}
      <span class="t-name">${call.name}</span><span class="t-sum">${summary}</span></summary>
    <pre class="block">${JSON.stringify(call.arguments || {}, null, 1)}</pre>
    ${result ? html`<pre class="block">${result.content}</pre>` : null}
  </details>`;
}

export function WhatWent({ callId, onClose }) {
  const [call, setCall] = useState(null);
  const [err, setErr] = useState(null);
  useEffect(() => { get(`/api/calls/${callId}`).then(setCall, setErr); }, [callId]);
  return html`<${Modal} wide title="Что ушло в модель" onClose=${onClose}>
    <${ErrorBox} error=${err} />
    ${call ? html`<div>
      <dl class="kv"><dt>Модель</dt><dd>${call.provider} · ${call.model}</dd><dt>Токены</dt><dd>${call.usage?.input ?? "—"} → ${call.usage?.output ?? "—"}${call.usage?.cache_read ? ` (из кэша ${call.usage.cache_read})` : ""}</dd>
        <dt>Цена</dt><dd>${money(call.cost)}</dd><dt>Время</dt><dd>${call.seconds?.toFixed?.(2) ?? "—"} с</dd>${call.notice ? html`<dt>Подмена</dt><dd>${call.notice}</dd>` : null}
        <dt>Тулы</dt><dd>${(call.request?.tools || []).join(", ")}</dd></dl>
      ${!call.request?.messages?html`<pre class="block">${JSON.stringify(call.request,null,2)}</pre>`:null}
      ${(call.request?.messages || []).map((m, i) => html`<details open=${i === 0 ? false : i === call.request.messages.length - 1}>
        <summary><b>${m.role}</b>${m.name ? ` · ${m.name}` : ""} <span class="faint">${String(typeof m.content === "string" ? m.content.length : JSON.stringify(m.content || "").length)} симв.</span></summary>
        <pre class="block">${typeof m.content === "string" ? m.content : JSON.stringify(m.content, null, 1)}${m.tool_calls ? "\n\n→ " + JSON.stringify(m.tool_calls, null, 1) : ""}</pre></details>`)}
    </div>` : html`<${Visor} mode="busy" />`}
  </${Modal}>`;
}

function groupMessages(msgs) {
  // результат тула прикрепляется к вызову в сообщении ассистента
  const results = {};
  for (const m of msgs) if (m.role === "tool") results[m.tool_call_id] = m;
  return { list: msgs.filter((m) => m.role !== "tool"), results };
}

function Attachments({items=[]}) {
  const download=async item=>{try{
    const response=await get('/api/attachments/'+encodeURIComponent(item.id));
    const url=URL.createObjectURL(await response.blob());
    const link=document.createElement('a');link.href=url;link.download=item.name;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }catch(error){toast(error.message,'error');}};
  return html`<div class="attachments">${items.map(item=>html`<button class="btn small" title="Скачать вложение" onClick=${()=>download(item)}>📎 ${item.name} · ${item.size} байт</button>`)}</div>`;
}
function messageText(value){return Array.isArray(value)?value.filter(p=>p.type==='text').map(p=>p.text).join('\n'):value||'';}

function channelText(meta={}){const route=meta.telegram_route||{};return (CHANNEL_NAMES[meta.channel]||meta.channel||'')+(meta.channel==='telegram'?(meta.telegram_owner_requested||route.chat<0?' · группа':'')+(route.thread?' · тема #'+route.thread:''):'');}

function LinkRequest({ link }) {
  const [done, setDone] = useState("");
  const decide = async (mode, label) => { try { await post("/api/correspondence/link", { sender: link.sender, recipient: link.recipient, mode }); setDone(label); toast(label); } catch (e) { toast(e.message, "error"); } };
  if (done) return html` <span class="faint">${done}</span>`;
  return html` <span class="row"><${Button} small kind="primary" onClick=${() => decide("yes", "Связь открыта")}>Разрешить</${Button}><${Button} small onClick=${() => decide("ask", "Разрешено с одобрением каждого письма")}>С одобрением</${Button}><${Button} small onClick=${() => decide("no", "Агент узнает, что пока нет")}>Не сейчас</${Button}></span>`;
}

function Message({ m, results, name, onWhat, onReply, onThread, agentNames }) {
  if(m.meta?.service_event)return html`<div class="event service-event">${m.meta.display||messageText(m.content)}${m.meta.link_request?html`<${LinkRequest} link=${m.meta.link_request}/>`:null}${m.meta.correspondence_notice&&onThread?html` <button class="btn small" onClick=${()=>onThread(m.meta.correspondence_notice.session)}>Открыть переписку</button>`:null}</div>`;
  if(m.meta?.tool_media)return html`<div class="event tool-media"><div class="m-text" dangerouslySetInnerHTML=${{__html:mdToHtml(messageText(m.content))}}></div></div>`;
  if (m.role === "event") {
    return html`<div class=${`event ${m.kind || ""}`}>${m.kind === "error" ? "⚠ " : ""}${m.content}${m.kind==='skill'&&m.data?.name?html` <${Button} small onClick=${async e=>{await post(`/api/skills/${encodeURIComponent(m.data.name)}/state`,{archived:true});e.target.disabled=true;e.target.textContent='Убран';}}>Убрать навык</${Button}>`:null}<${Attachments} items=${m.data?.attachments||[]}/>${m.data?.hint ? html`<div class="faint">${m.data.hint}</div>` : null}</div>`;
  }
  if (m.role === "user") {
    return html`<div class=${`msg human${m.meta?.queued ? " queued" : ""}`}><div class="me-face">Я</div><div class="m-body">
      <div class="m-head"><span class="m-name">${m.meta?.correspondence_sender?.name?"Агент "+m.meta.correspondence_sender.name:m.meta?.actor?.startsWith("agent:")?"Агент "+(agentNames?.[m.meta.actor.slice(6)]||m.meta.actor.slice(6)):ACTOR_NAMES[m.meta?.actor] || (m.meta?.actor && m.meta.actor !== "owner" ? "Не хозяин" : "Хозяин")}</span><span class="m-meta">${m.created_at ? timeOf(m.created_at) : ""}${m.meta?.channel ? ' · '+channelText(m.meta) : ''}${m.meta?.queued ? " · вставлю в идущий ход" : ""}${m.meta?.imported ? ` · из ${m.meta.imported === "openclaw" ? "OpenClaw" : m.meta.imported}` : ""}</span>${m.id&&onReply?html`<button class="btn small" onClick=${()=>onReply(m)}>Ответить</button>`:null}</div>
      <div class="m-text" dangerouslySetInnerHTML=${{ __html: mdToHtml(messageText(m.content)) }}></div><${Attachments} items=${m.meta?.attachments||[]}/></div></div>`;
  }
  const meta = m.meta || {};
  return html`<div class="msg"><${BotFace} /><div class="m-body">
    <div class="m-head"><span class="m-name">${name}</span><span class="m-meta">${m.created_at ? timeOf(m.created_at) : ""}${meta.model ? ` · ${meta.model}` : ""}${meta.channel?' · '+channelText(meta):''}${meta.cost ? ` · ${money(meta.cost)}` : ""}${meta.partial ? " · оборвалось" : ""}${meta.imported ? ` · из ${meta.imported === "openclaw" ? "OpenClaw" : meta.imported}` : ""}</span>${m.id&&onReply?html`<button class="btn small" onClick=${()=>onReply(m)}>Ответить</button>`:null}
      ${meta.call ? html`<button class="icon-btn small" title="Что ушло в модель" onClick=${() => onWhat(meta.call)}><${Icon} name="eye" /></button>` : null}</div>
    ${m.reasoning ? html`<details class="reasoning"><summary>Размышления</summary><div>${m.reasoning}</div></details>` : null}
    ${m.content ? html`<div class="m-text" dangerouslySetInnerHTML=${{ __html: mdToHtml(m.content) }}></div>` : null}
    ${m.content?html`<${SpeechButton} text=${m.content}/>`:null}
    ${(m.tool_calls || []).map((c) => html`<${ToolCall} key=${c.id} call=${c} result=${results[c.id]} />`)}
  </div></div>`;
}

export function ChatScreen() {
  const { st } = useApp();
  const agent=st?.agent_id||'main';
  const [chosen,setChosen]=useState(sessionStorage.getItem('bender.chat.'+agent));
  const [chats,setChats]=useState([]);
  const sid = chosen || st?.main_session;
  const chatLoad=()=>get('/api/chats').then(r=>setChats(r.sessions.filter(x=>!x.meta.archived&&['main','chat','imported','correspondence'].includes(x.kind))));
  useEffect(()=>{chatLoad();},[agent]);
  const choose=id=>{setChosen(id);sessionStorage.setItem('bender.chat.'+agent,id);};

  const [msgs, setMsgs] = useState([]);
  const [session,setSession]=useState(null);
  const current=chats.find(c=>c.id===sid)||(session?.id===sid?session:null);
  const readOnly=current?.kind==='correspondence';
  const displayChats=current&&!chats.some(c=>c.id===current.id)?[...chats,current]:chats;
  const agentNames=Object.fromEntries((st?.agents||[]).map(a=>[a.id,a.name]));
  const [live, setLive] = useState(null); // {text, reasoning, tools: [{id,name,arguments,done,ok,summary}]}
  const [plan, setPlan] = useState(null);
  const [running, setRunning] = useState(false);
  const [text, setText] = useState("");
  const [err, setErr] = useState(null);
  const [what, setWhat] = useState(null);
  const [quote,setQuote]=useState(null);
  const [compacting,setCompacting]=useState(false);
  useEffect(()=>setQuote(null),[sid]);
  const [files,setFiles]=useState([]),[uploading,setUploading]=useState(false);
  const picker=useRef(null);
  const upload=async event=>{
    const selected=Array.from(event.target.files||[]);event.target.value='';
    if(files.length+selected.length>10){setErr(new Error('В одном сообщении не больше 10 файлов.'));return;}
    setUploading(true);
    try{for(const file of selected){
      if(file.size>20*1024*1024)throw new Error('Входящий файл больше 20 МБ. Раздели его перед отправкой.');
      const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file);});
      const item=await post('/api/attachments',{name:file.name,mime:file.type,data,session:sid});
      setFiles(current=>[...current,item]);
    }}catch(error){setErr(error);}finally{setUploading(false);}
  };
  const scroller = useRef(null);
  const stick = useRef(true);
  const name = st?.identity?.name || "Агент";

  const load = async () => {
    if (!sid) return;
    try { const r = await get(`/api/session/${sid}/messages?limit=300`); setMsgs(r.messages); setRunning(r.running);setSession(r.session); } catch (e) { setErr(e); }
  };
  useEffect(() => { load(); }, [sid, st?.has_model]);

  useEvents((ev) => {
    if(ev.type==='correspondence')chatLoad();
    if (ev.session && ev.session !== sid) return;
    if (ev.type === "turn_start") { setRunning(true); setLive({ text: "", reasoning: "", tools: [] }); }
    else if (ev.type === "delta") {
      setLive((l) => { l = l || { text: "", reasoning: "", tools: [] }; return ev.kind === "text" ? { ...l, text: l.text + ev.text } : ev.kind === "reasoning" ? { ...l, reasoning: l.reasoning + ev.text } : l; });
    } else if (ev.type === "message") {
      const m = ev.message;
      if (m.role === "assistant") setLive((l) => l && { ...l, text: "", reasoning: "" });
      setMsgs((xs) => [...xs, { ...m, created_at: m.created_at || Date.now() / 1000 }]);
    } else if (ev.type === "tool_start") {
      setLive((l) => ({ ...(l || { text: "", reasoning: "" }), tools: [...(l?.tools || []), { id: ev.id, name: ev.name, arguments: ev.arguments }] }));
    } else if (ev.type === "tool_end") {
      setMsgs((xs) => [...xs, { role: "tool", tool_call_id: ev.id, content: ev.content, meta: { ok: ev.ok, summary: ev.summary, tainted: ev.tainted } }]);
    } else if (ev.type === "plan") { setPlan(ev.items); }
    else if (ev.type === "turn_end") { setRunning(false); setLive(null); load(); refresh().catch(() => {}); }
    else if (ev.type === "imported") { load(); }
  }, [sid]);

  useEffect(() => {
    const el = scroller.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  });

  const send = async () => {
    const t = text.trim();
    if (readOnly||(!t&&!files.length)||uploading) return;
    setText("");
    stick.current = true;
    try { await post("/api/say", { text: t, session: sid,attachments:files.map(f=>f.id),...(quote?{reply_to:quote.id}:{}) });setFiles([]);setQuote(null); } catch (e) { setErr(e); setText(t); }
  };
  const compactNow=async()=>{setCompacting(true);try{await post('/api/memory/compact',{session:sid,action:'now'});toast('Начало разговора пересказано, история сохранена');await load();}catch(error){setErr(error);}finally{setCompacting(false);}};
  const stop = async () => { try { await post("/api/stop", { session: sid }); } catch (e) { toast(e.message, "error"); } };

  if (st && !st.has_model && !readOnly) {
    return html`<div class="page"><div class="page-inner"><${Welcome} /></div></div>`;
  }
  const { list, results } = groupMessages(msgs);
  return html`<div class="chat">
    <div class="row" style="padding:6px;flex-wrap:wrap"><select class="select grow chat-picker" aria-label="Выбрать чат" value=${sid} onChange=${e=>choose(e.target.value)}><optgroup label="Разговоры">${displayChats.filter(c=>c.kind!=="correspondence").map(c=>html`<option value=${c.id}>${c.title}</option>`)}</optgroup>${displayChats.some(c=>c.kind==="correspondence")?html`<optgroup label="Переписка">${displayChats.filter(c=>c.kind==="correspondence").map(c=>html`<option value=${c.id}>${c.title}</option>`)}</optgroup>`:null}</select>
    <${Button} small onClick=${async()=>{const r=await post('/api/chats',{title:'Новый чат'});choose(r.session.id);chatLoad();}}>Новый чат</${Button}>
    <${Button} small disabled=${readOnly||running} busy=${compacting} onClick=${compactNow}>Сжать сейчас</${Button}>
    <${Button} small disabled=${readOnly} onClick=${async()=>{const title=prompt('Название чата');if(title){await patch('/api/chats/'+sid,{title});chatLoad();}}}>Имя чата</${Button}>
    <${Button} small disabled=${readOnly||sid===st?.main_session} onClick=${async()=>{await patch('/api/chats/'+sid,{archived:true});choose(st.main_session);chatLoad();}}>В архив</${Button}></div>
    <div class="messages" ref=${scroller} onScroll=${(e) => { const el = e.target; stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80; }}>
      <div class="messages-inner">
        ${!list.length && !live ? html`<div class="empty"><div style="font-size:2.4rem"><${Visor} /></div>
          <h2>${st?.born ? `${name} на связи` : "Агент только что проснулся"}</h2>
          <p class="muted">${st?.born ? "Напиши, что сделать." : "Он ещё не знает, кто он и где оказался. Напиши что-нибудь — начнётся знакомство."}</p></div>` : null}
        ${list.map((m, i) => html`<${Message} key=${m.id || i} m=${m} results=${results} name=${name} onWhat=${setWhat} onReply=${readOnly?null:setQuote} onThread=${id=>{chatLoad();choose(id);}} agentNames=${agentNames} />`)}
        ${live && (live.text || live.reasoning) ? html`<div class="msg"><${BotFace} busy /><div class="m-body">
          <div class="m-head"><span class="m-name">${name}</span><span class="m-meta">пишет…</span></div>
          ${live.reasoning ? html`<details class="reasoning"><summary>Размышления</summary><div>${live.reasoning}</div></details>` : null}
          ${live.text ? html`<div class="m-text cursor" dangerouslySetInnerHTML=${{ __html: mdToHtml(live.text) }}></div>` : null}
        </div></div>` : running ? html`<div class="msg"><${BotFace} busy /><div class="m-body faint">думает…</div></div>` : null}
        ${plan && plan.length ? html`<div class="plan"><b>План</b>${plan.map((p) => html`<div class=${p.status}>${p.status === "done" ? "☑" : p.status === "doing" ? "◐" : "☐"} ${p.text}</div>`)}</div>` : null}
        <${PendingCards} />
        <${ErrorBox} error=${err} onClose=${() => setErr(null)} />
      </div>
    </div>
    ${readOnly?html`<div class="note correspondence-readonly">Переписка агентов · только чтение. Поручение отправь в обычном чате.</div>`:html`<div class="composer">${quote?html`<div class="note quote-preview">Цитата: ${messageText(quote.content).slice(0,240)} <button class="btn small" aria-label="Отменить цитату" onClick=${()=>setQuote(null)}>×</button></div>`:null}<div class="row">${files.map(file=>html`<span class="note small">📎 ${file.name} <button aria-label=${'Убрать '+file.name} onClick=${()=>setFiles(files.filter(f=>f.id!==file.id))}>×</button></span>`)}</div><input ref=${picker} type="file" multiple hidden onChange=${upload}/><button class="btn small" disabled=${uploading} onClick=${()=>picker.current.click()}>${uploading?'Загружаю…':'Прикрепить'}</button><${VoiceControls} sid=${sid} onTranscript=${setText} onAttachment=${item=>setFiles(current=>[...current,item])}/><div class="composer-inner">
      <${AutoTextarea} value=${text} onInput=${setText} placeholder=${running ? "Написать во время работы — вставлю в идущий ход" : "Сообщение…"}
        onKeyDown=${(e) => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing && window.innerWidth >= 700) { e.preventDefault(); send(); } }} />
      ${running ? html`<${IconButton} icon="stop" title="Остановить ход" danger onClick=${stop} />` : null}
      <${Button} kind="primary" icon="send" onClick=${send} disabled=${uploading||(!text.trim()&&!files.length)} title="Отправить"></${Button}>
    </div></div>`}
    ${what ? html`<${WhatWent} callId=${what} onClose=${() => setWhat(null)} />` : null}
  </div>`;
}

function Welcome() {
  const { st } = useApp();
  return html`<div>
    <div class="empty" style="padding-bottom:10px"><div style="font-size:2.6rem"><${Visor} mode="sleep" /></div>
      <h2>Агент ещё спит</h2><p class="muted">Чтобы проснуться, ему нужна модель — чем думать. Подключи любую: ключ провайдера, OmniRoute, локальная модель или тестовая без токенов.</p></div>
    <${SetupModel} onDone=${() => refresh()} />
    <${RelocateCard} /><${RelocateCard} kind="hermes" /><${RelocateCard} kind="claude" />
    ${st?.safe ? html`<div class="note red">Безопасный режим: без модулей, каналов и сердца.</div>` : null}
  </div>`;
}

export { setState, state };
