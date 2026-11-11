// Модели: провайдеры, каталог, роли с цепочками запасных, проверка вызовом.
import { html, useEffect, useState } from "../lib/ui.js";
import { del, get, patch, post } from "../lib/api.js";
import { Button, ErrorBox, Field, Icon, IconButton, Modal, Toggle, Visor, copyText, money, toast } from "../lib/ui.js";
import { PendingCards } from "./chat.js";

import { useEvents } from "../lib/store.js";

const KEYLESS = ["ollama", "llamacpp", "lmstudio", "fake", "omniroute", "claude_code", "codex_cli"];

function OtherCliSetup({ kind, busy, onConnect }) {
  const [command, setCommand] = useState("");
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);
  const [checking, setChecking] = useState(false);
  const check = async () => {
    setChecking(true); setError(null);
    try { setStatus(await post(`/api/cli/${kind}/check`, { command })); } catch (e) { setError(e); }
    setChecking(false);
  };
  return html`<div class="other-cli-setup">
    <p class="note">Codex пока не подключается как движок: не подтверждён способ выключить все встроенные инструменты. Проверка программы доступна.</p>
    <${Field} label=${`Команда ${kind}`}><input aria-label=${kind} class="input mono" value=${command} placeholder=${kind} onInput=${e => setCommand(e.target.value)} /></${Field}>
    <div class="row"><${Button} busy=${checking} onClick=${check}>Проверить CLI</${Button}>
      <${Button} kind="primary" busy=${busy} disabled=${!status?.safe_tools || kind === "codex"} onClick=${() => onConnect({ command: command || kind, billing: "subscription" })}>Подключить CLI</${Button}></div>
    <${PendingCards} /><${ErrorBox} error=${error} />
    ${status ? html`<p class="cli-status">${status.found ? `Найден ${kind} ${status.version || "?"}. ` : "Программа не найдена. "}${status.reason}</p>` : null}
  </div>`;
}

// Вход через ChatGPT: код устройства (по умолчанию) или браузер на этом устройстве.
function ChatGPTLogin({ onConnected }) {
  const [flow, setFlow] = useState(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [paste, setPaste] = useState("");
  const start = async (method) => {
    setBusy(true); setErr(null);
    try {
      const f = await post("/api/auth/chatgpt/start", { method });
      setFlow(f);
      if (method === "browser") window.open(f.url, "_blank", "noopener");
    } catch (e) {
      if (e.code === "chatgpt_no_device" && method === "device") { setBusy(false); return start("browser"); }
      setErr(e);
    }
    setBusy(false);
  };
  useEffect(() => {
    if (!flow || flow.state !== "waiting") return undefined;
    const t = setInterval(async () => {
      try {
        const f = await get(`/api/auth/chatgpt/${flow.id}`);
        if (f.state === "waiting") return;
        setFlow(f);
        if (f.state === "done") onConnected(f.provider, f.main_model);
        else if (f.state === "error") setErr(f.error);
      } catch (e) { setErr(e); setFlow(null); }
    }, 1500);
    return () => clearInterval(t);
  }, [flow?.id, flow?.state]);
  const cancel = async () => { if (flow) await post(`/api/auth/chatgpt/${flow.id}/cancel`).catch(() => {}); setFlow(null); };
  const sendPaste = async () => { try { await post(`/api/auth/chatgpt/${flow.id}/paste`, { text: paste }); setBusy(true); } catch (e) { setErr(e); } };
  const waiting = flow && flow.state === "waiting";
  return html`<div>
    ${!waiting ? html`<div>
      <div class="note">Подписка ChatGPT (Plus, Pro, Business…) — модели OpenAI без оплаты за токены, в пределах лимитов твоего плана. OpenAI разрешает такой вход в сторонних программах; Бендер входит под своим именем.</div>
      <div class="row"><${Button} kind="primary" icon="key" busy=${busy} onClick=${() => start("device")}>Войти через ChatGPT</${Button}>
        <${Button} kind="ghost" disabled=${busy} onClick=${() => start("browser")}>Через браузер на этом устройстве</${Button}></div>
    </div>` : flow.method === "device" ? html`<div class="login-code">
      <div>1. Открой на любом устройстве: <a href=${flow.verification_url} target="_blank" rel="noopener">${flow.verification_url.replace(/^https?:\/\//, "")}</a></div>
      <div>2. Войди в ChatGPT и введи код:</div>
      <div class="row"><span class="big-code mono">${flow.user_code}</span><${Button} small icon="copy" onClick=${() => copyText(flow.user_code).then(() => toast("Код скопирован", "ok"))}>Скопировать</${Button}></div>
      <div class="row faint"><${Visor} mode="busy" /> Жду подтверждения… Код живёт 15 минут.</div>
      <${Button} small kind="ghost" onClick=${cancel}>Отмена</${Button}>
    </div>` : html`<div class="login-code">
      <div>Страница входа OpenAI открыта в новой вкладке${flow.listening ? "" : " (порт 1455 занят — адрес придётся вставить)"}. <a href=${flow.url} target="_blank" rel="noopener">Открыть ещё раз</a></div>
      <div class="row faint"><${Visor} mode="busy" /> Жду, пока войдёшь…</div>
      <${Field} label="Вкладка не вернулась в Бендер?" hint="Если браузер на другом устройстве — после входа скопируй адрес из его адресной строки (http://localhost:1455/auth/callback?code=…) и вставь сюда.">
        <div class="row"><input aria-label="http://localhost:1455/auth/callback?code=…" class="input mono grow" value=${paste} onInput=${(e) => setPaste(e.target.value)} placeholder="http://localhost:1455/auth/callback?code=…" />
          <${Button} disabled=${!paste.trim()} onClick=${sendPaste}>Готово</${Button}></div></${Field}>
      <${Button} small kind="ghost" onClick=${cancel}>Отмена</${Button}>
    </div>`}
    <${ErrorBox} error=${err} onClose=${() => setErr(null)} />
  </div>`;
}

function ClaudeCodeSetup({ busy, onConnect }) {
  const [st, setSt] = useState(null);
  const [cmd, setCmd] = useState("");
  const [checking, setChecking] = useState(false);
  const check = async () => {
    setChecking(true);
    try { setSt(await get(`/api/claude-code/check${cmd.trim() ? `?command=${encodeURIComponent(cmd.trim())}` : ""}`)); } catch (e) { setSt({ found: false, error: e.message }); }
    setChecking(false);
  };
  useEffect(() => { check(); }, []);
  const ready = st && st.found && st.logged_in;
  return html`<div class="cc-setup">
    <div class="note amber">Бендер запускает <b>официальный Claude Code</b>, в который ты сам вошёл на этой машине: свои тулы Claude Code выключены, руки — только Бендера, через Предохранитель.
      <br />Правила Anthropic про такое использование подписки в 2026 году менялись. По справке Anthropic (октябрь 2026) <span class="mono">claude -p</span> со входом по подписке расходует лимиты подписки. Вход в Claude Code — только твой собственный: не давай его другим людям. Правила могут поменяться снова — держи запасную модель в цепочке.</div>
    ${!st ? html`<div class="faint">Ищу claude…</div>` : html`<dl class="kv cc-status">
      <dt>Программа</dt><dd>${st.found ? html`<span class="green">найдена</span> <span class="mono faint">${st.path}</span>` : html`<span class="red">не найдена</span>`}</dd>
      ${st.found ? html`<dt>Версия</dt><dd class="mono">${st.version || "?"}</dd>
      <dt>Вход</dt><dd>${st.logged_in ? html`<span class="green">вошёл</span> · ${st.billing === "api" ? "ключом API (ходы платные)" : "подпиской"}` : html`<span class="red">не вошёл</span> — в терминале: <code>claude</code>, потом <code>/login</code>`}</dd>` : null}
    </dl>`}
    ${st && !st.found ? html`<div>
      <div class="note">Поставь Claude Code (<code>npm install -g @anthropic-ai/claude-code</code> или установщик с claude.com) и войди в него. Если он стоит не там, где ищу, — укажи путь.</div>
      <${Field} label="Путь к claude"><input aria-label="/usr/local/bin/claude" class="input mono" value=${cmd} placeholder="/usr/local/bin/claude" onInput=${(e) => setCmd(e.target.value)} /></${Field}></div>` : null}
    <div class="row">
      <${Button} kind=${ready ? "ghost" : "primary"} busy=${checking} onClick=${check}>Проверить снова</${Button}>
      <${Button} kind="primary" busy=${busy} disabled=${!ready} onClick=${() => onConnect({ command: cmd.trim() || undefined, billing: st.billing || undefined })}>Подключить</${Button}>
    </div>
  </div>`;
}

export function SetupModel({ onDone, role = "main" }) {
  const [presets, setPresets] = useState({});
  const [preset, setPreset] = useState(role==='ears'?'groq_voice':role==='voice'?'gemini_voice':'openrouter');
  const [url, setUrl] = useState("");
  const [kind, setKind] = useState("openai");
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const [provider, setProvider] = useState(null);
  const [models, setModels] = useState(null);
  const [q, setQ] = useState("");
  const [manual, setManual] = useState("");
  const [previous,setPrevious]=useState([]);
  const [localInstalled,setLocalInstalled]=useState(false);
  useEffect(() => { get("/api/models").then((r) => {setPresets(r.presets);setPrevious(r.roles[role]||[]);setLocalInstalled(r.local_voice_installed);}).catch(setErr); }, []);

  const connect = async (more = {}) => {
    setBusy(true); setErr(null);
    try {
      if(preset==='local_voice'){await post('/api/voice/local/connect');onDone&&onDone();setBusy(false);return;}
      const body = preset === "custom" ? { name: "Свой провайдер", base_url: url, kind, api_key: key } : { preset, base_url: url || undefined, api_key: key, ...more };
      const r = await post("/api/providers", body);
      setProvider(r.provider);
      try { const c = await post(`/api/providers/${r.provider.id}/catalog`); setModels(c.models); if (!role) { toast(`Подключён ${r.provider.name}: моделей ${c.models.length}`, "ok"); onDone && onDone(); } }
      catch (e) { setErr(e); setModels([]); if (!role) { onDone && onDone(); } }
    } catch (e) { setErr(e); }
    setBusy(false);
  };
  const choose = async (model) => {
    setBusy(true);
    try {
      const rest=['ears','voice'].includes(role)?previous.filter(entry=>entry.provider!==provider.id||entry.model!==model):[];
      await post(`/api/roles/${role}`, { entries: [{ provider: provider.id, model },...rest] });
      if(['ears','voice'].includes(role)){toast('Аудиомодель выбрана. Проверить можно микрофоном или кнопкой «Озвучить».','ok');onDone&&onDone();setBusy(false);return;}
      const t = await post("/api/try/model", { text: "Ответь одним словом: работаешь?" }).catch((e) => ({ error: e }));
      if (t.error) toast(`Модель выбрана, но проверка не прошла: ${t.error.message}`, "error"); else toast(`Модель отвечает: «${t.text.slice(0, 60)}»`, "ok");
      onDone && onDone();
    } catch (e) { setErr(e); }
    setBusy(false);
  };
  const shown = (models || []).filter(m=>!m.audio_role||m.audio_role===role).filter((m) => !q || m.id.toLowerCase().includes(q.toLowerCase()) || (q.toLowerCase() === "free" && m.free)).slice(0, 80);
  return html`<div class="card">
    <div class="card-title"><${Icon} name="cpu" /><h3>${provider ? `Модель у ${provider.name}` : "Подключить модель"}</h3></div>
    ${!provider ? html`<div>
      <${Field} label="Откуда"><select class="select" value=${preset} onChange=${(e) => { setPreset(e.target.value); setUrl(""); }}>
        ${Object.entries(presets).filter(([k,p])=>(k!=='local_voice'||localInstalled)&&(!p.audio_only||['ears','voice'].includes(role))).map(([k, p]) => html`<option value=${k}>${p.name}${p.free ? " — бесплатно, локально" : ""}</option>`)}
        <option value="custom">Свой адрес (OpenAI- или Anthropic-совместимый)</option></select></${Field}>
      ${presets[preset]?.note?html`<p class="note">${presets[preset].note}</p>`:null}
      ${preset!=='local_voice'&&(preset === "custom" || presets[preset]?.is_local || presets[preset]?.audio_only) ? html`<${Field} label="Адрес API" hint=${preset === "custom" ? "Например http://127.0.0.1:20128/v1" : `По умолчанию ${presets[preset]?.base_url}`}>
        <input aria-label="Адрес API" class="input mono" value=${url} placeholder=${presets[preset]?.base_url || "https://…/v1"} onInput=${(e) => setUrl(e.target.value)} /></${Field}>` : null}
      ${preset === "custom" ? html`<${Field} label="Вид API"><select class="select" value=${kind} onChange=${(e) => setKind(e.target.value)}><option value="openai">OpenAI-совместимый</option><option value="anthropic">Anthropic</option></select></${Field}>` : null}
      ${preset === "chatgpt" ? html`<${ChatGPTLogin} onConnected=${() => onDone && onDone()} />` : preset === "claude_code" ? html`<${ClaudeCodeSetup} busy=${busy} onConnect=${connect} />` : preset === "codex_cli" ? html`<${OtherCliSetup} key=${preset} kind="codex" busy=${busy} onConnect=${connect} />` : html`<div>
      ${preset!=='local_voice'&&(!KEYLESS.includes(preset) || preset === "custom") ? html`<${Field} label="API-ключ" hint="Хранится в сейфе на этой машине. В модель и в интерфейс не попадает.">
        <input class="input mono" type="password" value=${key} autocomplete="off" onInput=${(e) => setKey(e.target.value)} /></${Field}>` : null}
      ${preset === "fake" ? html`<div class="note">Тестовые модели без токенов: запусти в терминале <code>python -m bender fake-llm</code>.</div>` : null}
      <${Button} kind="primary" busy=${busy} onClick=${() => connect()}>${preset==='local_voice'?'Подключить установленный голос':'Подключить'}</${Button}></div>`}
    </div>` : html`<div>
      ${provider.login_command ? html`<p class="note">Перед первым вызовом войди официальным Gemini в отдельный профиль: <code>${provider.login_command}</code></p>` : null}
      ${models && models.length ? html`<div>
        <${Field} label=${`Моделей: ${models.length}. Поиск`} hint="«free» — только бесплатные"><input aria-label="название или free" class="input" value=${q} onInput=${(e) => setQ(e.target.value)} placeholder="название или free" /></${Field}>
        <div class="list" style="max-height:340px;overflow:auto">${shown.map((m) => html`<div class="list-item">
          <div class="li-main"><div class="li-title mono">${m.id}</div><div class="li-sub">${m.subscription ? "по подписке" : m.free ? "бесплатно" : m.price ? `$${m.price.in.toFixed(2)} / $${m.price.out.toFixed(2)} за 1M` : ""}${m.note ? ` · ${m.note}` : ""}${m.context ? ` · контекст ${m.context}` : ""}${m.tools ? " · тулы" : ""}</div></div>
          <${Button} small kind="primary" disabled=${busy} onClick=${() => choose(m.id)}>Взять</${Button}></div>`)}</div></div>` : null}
      <${Field} label="Или название модели вручную"><div class="row"><input aria-label="например gpt-4o-mini" class="input mono grow" value=${manual} onInput=${(e) => setManual(e.target.value)} placeholder="например gpt-4o-mini" />
        <${Button} kind="primary" disabled=${!manual.trim() || busy} onClick=${() => choose(manual.trim())}>Взять</${Button}></div></${Field}>
    </div>`}
    <${ErrorBox} error=${err} onClose=${() => setErr(null)} />
  </div>`;
}

function ModelPicker({ provider, value, fuel, role, onChange, onRefresh }) {
  const [manual, setManual] = useState(false);
  const [query, setQuery] = useState('');
  const [busy, setBusy] = useState(false);
  const models = (provider?.models || []).filter(model=>!model.audio_role||model.audio_role===role);
  const known = models.some(m=>m.id===value);
  const freeInput = !models.length || manual || (!!value && !known);
  useEffect(()=>{setManual(false);setQuery('');}, [provider?.id]);
  const refresh = async () => {
    setBusy(true);
    try { await post(`/api/providers/${provider.id}/catalog`); onRefresh(); }
    catch(e) { toast(e.message, 'error'); }
    setBusy(false);
  };
  const forget = async () => {
    if (!confirm(`Убрать «${value}» из списка ${provider.name}? В уже настроенных ролях она останется.`)) return;
    setBusy(true);
    try { await post(`/api/providers/${provider.id}/forget-model`, {model:value}); onRefresh(); }
    catch(e) { toast(e.message, 'error'); }
    setBusy(false);
  };
  const label = m => {
    const states = (fuel?.accounts||[]).filter(a=>a.provider===provider?.id).flatMap(a=>a.models).filter(x=>x.id===m.id);
    const lamp = states.some(x=>x.health==='cooling') ? '🟠' : states.some(x=>x.health==='ok') ? '🟢' : '⚪';
    const access = m.subscription || provider?.subscription || ['chatgpt','claude_code'].includes(provider?.kind) && provider?.billing!=='api' ? 'подписка' : provider?.is_local ? 'локальная' : 'ключ';
    const price = m.price || provider?.prices?.[m.id];
    return `${lamp} ${m.id} · ${m.manual ? 'введена вручную' : access}${m.context ? ' · '+m.context+' контекст' : ''}${price ? ' · $'+price.in+' / $'+price.out+' за 1M' : ''}`;
  };
  const shown = models.filter(m=>!query || m.id.toLowerCase().includes(query.toLowerCase()) || m.id===value);
  return html`<div class="model-picker grow">
    ${models.length>15 && !freeInput ? html`<input aria-label="Поиск модели" class="input" placeholder="Поиск модели" value=${query} onInput=${e=>setQuery(e.target.value)}/>` : null}
    ${models.length ? html`<select aria-label="модель" class="select" value=${freeInput ? '__manual__' : value || ''} onChange=${e=>{if(e.target.value==='__manual__')setManual(true);else{setManual(false);onChange(e.target.value);}}}>
      <option value="">Выбери модель</option>${shown.map(m=>html`<option value=${m.id}>${label(m)}</option>`)}<option value="__manual__">Ввести вручную…</option></select>` : null}
    ${freeInput ? html`<input aria-label="Модель вручную" class="input mono" value=${value} placeholder="Название модели" onInput=${e=>onChange(e.target.value)}/>` : null}
    ${!models.length ? html`<p class="faint">Каталог пока недоступен. Введи название вручную или обнови список.</p>` : null}
    <${Button} small busy=${busy} disabled=${!provider} onClick=${refresh}>Обновить список</${Button}>
    ${models.find(m=>m.id===value&&m.manual) ? html`<${Button} small kind="ghost" busy=${busy} title="Убрать из списка провайдера: модель вводили вручную" onClick=${forget}>Забыть введённую</${Button}>` : null}
  </div>`;
}

function MediaCapabilities({provider,model,audioAvailable,onSaved}){
  const [busy,setBusy]=useState(false),[result,setResult]=useState(null),[error,setError]=useState(null);
  useEffect(()=>{setResult(null);setError(null);},[provider?.id,model]);
  if(!provider||!model||provider.audio_only||provider.kind==='codex_cli')return null;
  const cli=provider.kind==='claude_code';
  const catalog=provider.models?.find(item=>item.id===model)||{};
  const record=key=>provider.media_capabilities?.[model]?.[key]||provider.model_capabilities?.[model]?.[key]||{value:typeof catalog[key]==='boolean'?catalog[key]:null};
  const state=key=>record(key).value===true?'yes':record(key).value===false?'no':'unknown';
  const word=key=>({yes:'да',no:'нет',unknown:'неизвестно'})[state(key)];
  const run=async fn=>{setBusy(true);setError(null);try{setResult(await fn());onSaved();}catch(e){setError(e);}finally{setBusy(false);}};
  const change=(feature,value)=>run(()=>post(`/api/providers/${provider.id}/capability`,{model,feature,value:value==='unknown'?null:value==='yes'}));
  const probe=feature=>run(()=>post('/api/model/probe',{provider:provider.id,model,feature}));
  const audioProtocol=['openai','chatgpt'].includes(provider.kind);
  return html`<details class="model-media"><summary>Возможности: зрение — ${word('vision')}, слух — ${word('audio_input')}</summary>
    <p class="faint small">Неизвестно — пробуем отправить. Явный отказ API запоминается с датой; повторный запрос идёт без неподдерживаемого вложения. Проверка ниже — один короткий запрос без истории, может стоить денег.</p>
    ${cli?html`<p class="faint small">Claude Code получает картинки изображениями; проверка зрения — один разовый вызов без истории.</p>`:null}
    ${(cli?[]:[['vision','Зрение'],['audio_input','Слух']]).map(([key,title])=>html`<${Field} label=${title+' модели '+model}><select class="select" value=${state(key)} disabled=${busy} onChange=${e=>change(key,e.target.value)}><option value="unknown">Неизвестно — пробовать</option><option value="yes">Да</option><option value="no">Нет</option></select></${Field}>
      ${record(key).at?html`<p class="faint small">${title}: ${new Date(record(key).at*1000).toLocaleString('ru')}; ${record(key).source==='owner'?'выбор хозяина':record(key).source==='catalog'?'каталог':'фактическая проверка/отказ'}.</p>`:null}
      ${record(key).reason?html`<details><summary>${title}: причина</summary><pre class="block">${record(key).reason}</pre></details>`:null}`)}
    <div class="row"><${Button} small busy=${busy} onClick=${()=>probe('vision')}>Проверить зрение</${Button}>
    ${cli?null:audioAvailable&&audioProtocol?html`<${Button} small busy=${busy} onClick=${()=>probe('audio_input')}>Проверить слух</${Button}>`:html`<span class="faint small">Проверка слуха недоступна: ${!audioAvailable?'нужен установленный и включённый локальный Piper для короткого WAV':'адаптер не имеет подтверждённого протокола аудио'}.</span>`}</div>
    ${result?.message?html`<p class="note">${result.message}${result.answer?' Ответ: '+result.answer:''}${result.expected?' Ожидалось: '+result.expected:''}</p>`:null}
    ${result?.unseen&&!cli&&state('vision')!=='no'?html`<div class="row"><${Button} small busy=${busy} onClick=${()=>change('vision','no')}>Поставить «Нет»</${Button}></div>`:null}
    ${result?.reason?html`<details><summary>Ответ провайдера</summary><pre class="block">${result.reason}</pre></details>`:null}<${ErrorBox} error=${error}/></details>`;
}

function EffortEditor({entry,role,index,levels,modes,reasoning,onChange}) {
  const [info,setInfo]=useState(null),[error,setError]=useState(null);
  const effort=entry.params?.effort??'',mode=entry.params?.effort_mode||'auto';
  useEffect(()=>{
    let alive=true;setInfo(null);setError(null);
    if(entry.provider&&entry.model)get('/api/models/effort?'+new URLSearchParams({provider:entry.provider,model:entry.model,mode,effort})).then(result=>{if(alive)setInfo(result);},problem=>{if(alive)setError(problem);});
    return()=>{alive=false;};
  },[entry.provider,entry.model,mode,effort,reasoning.enabled,reasoning.effort]);
  const change=(key,value)=>{const params={...(entry.params||{})};if(value)params[key]=value;else delete params[key];onChange(params);};
  return html`<details class="effort-editor" style="margin:0 0 12px"><summary>Усилия модели${effort?' · '+effort:''}</summary>
    <div class="row"><${Field} label="Уровень усилий"><select class="select" aria-label=${`Усилия ${role} ${index+1}`} value=${effort} onChange=${e=>change('effort',e.target.value)}>
      <option value="">По умолчанию у агента</option>${Object.entries(levels).map(([key,title])=>html`<option value=${key}>${key} · ${title}</option>`)}
    </select></${Field}><${Field} label="Формат усилий"><select class="select" aria-label=${`Формат усилий ${role} ${index+1}`} value=${mode} onChange=${e=>change('effort_mode',e.target.value==='auto'?'':e.target.value)}>
      ${Object.entries(modes).map(([key,title])=>html`<option value=${key}>${title}</option>`)}
    </select></${Field}></div>
    ${info?html`<p class="faint">Запрошено: ${info.requested}. В запросе: ${info.effective} · ${info.mode}. ${info.note}</p>`:null}
    <${ErrorBox} error=${error}/><p class="faint small">default оставляет выбор провайдеру. Усилия могут увеличить время и стоимость. Изменения вступят в силу после «Сохранить».</p>
  </details>`;
}

function Reasoning({data,levels,onSaved}) {
  const change=async values=>{try{await post('/api/settings/reasoning',values);onSaved();}catch(e){toast(e.message,'error');}};
  return html`<div class="card"><h3>Усилия моделей этого агента</h3><p class="faint">Умолчание для всех его текстовых ролей и запасных. Отдельный выбор у кандидата роли важнее. Команда: /effort high; /effort default.</p>
    <div class="row"><${Toggle} label="Передавать выбранные усилия" checked=${data.enabled} onChange=${enabled=>change({enabled})}/>
    <select class="select" aria-label="Усилия по умолчанию" value=${data.effort} onChange=${e=>change({effort:e.target.value})}>${Object.entries(levels).map(([key,title])=>html`<option value=${key}>${key} · ${title}</option>`)}</select></div>
    ${data.models.map(row=>html`<div class="small faint">${row.role} · ${row.model}: ${row.requested} → ${row.effective} · ${row.mode}${row.note?' · '+row.note:''}</div>`)}
  </div>`;
}

function RoleEditor({ role, info, chain, providers, fuel, audioAvailable, effortLevels,effortModes,reasoning,agentsCount,onSaved,onConnect }) {
  const [items, setItems] = useState(chain);
  const [busy, setBusy] = useState(false);
  useEffect(() => setItems(chain), [JSON.stringify(chain)]);
  const save = async (next) => {
    setBusy(true);
    try { await post(`/api/roles/${role}`, { entries: next }); onSaved(); } catch (e) { toast(e.message, "error"); }
    setBusy(false);
  };
  const saveAll = async () => {
    const next = items.filter((x) => x.provider && x.model);
    if (!confirm(`Поставить эту цепочку роли «${info.title}» всем агентам стаи? Потом каждого можно поправить отдельно.`)) return;
    setBusy(true);
    try {
      const r = await post(`/api/roles/${role}/all`, { entries: next });
      toast(`Роль «${info.title}» применена: агентов ${r.agents.length}` + (r.skipped.length ? `, пропущено ${r.skipped.length} (${r.skipped.map(x=>x.name+': '+x.reason).join('; ')})` : ''), 'ok');
      onSaved();
    } catch (e) { toast(e.message, "error"); }
    setBusy(false);
  };
  const add = () => setItems([...items, { provider: providers[0]?.id || "", model: "", enabled: true }]);
  const upd = (i, p) => setItems(items.map((x, j) => (j === i ? { ...x, ...p } : x)));
  const [probe, setProbe] = useState(null);
  const check = async e => {
    setBusy(true);
    try { const r = await post('/api/try/model-tools', {provider:e.provider,model:e.model,params:e.params||{}}); setProbe(r); }
    catch (err) { setProbe({state:err.message}); }
    setBusy(false);
  };
  return html`<div class="card">
    <div class="card-title"><h3>${info.title}</h3><span class="faint">${info.hint}</span>${['ears','voice'].includes(role)?html`<${Button} small onClick=${onConnect}>${'Подключить '+info.title}</${Button}>`:null}</div>
    ${!items.length ? html`<div class="faint" style="margin-bottom:8px">${role === "main" ? "Не выбрана — агенту нечем думать." : ["ears","voice","embed"].includes(role)?"Не задана — эта часть не подключена.":"Не задана — работает Главная."}</div>` : null}
    ${items.map((e, i) => html`<div class="row role-entry">
      <span class="badge">${i === 0 ? "основная" : `запасная ${i}`}</span>
      <select aria-label=${"Провайдер роли "+info.title} class="select" style="width:auto" value=${e.provider} onChange=${(ev) => upd(i, { provider: ev.target.value, model: "" })}>${providers.map((p) => html`<option value=${p.id}>${p.name}</option>`)}</select>
      <${ModelPicker} provider=${providers.find(p=>p.id===e.provider)} value=${e.model} fuel=${fuel} role=${role} onChange=${model=>upd(i,{model})} onRefresh=${onSaved}/>
      ${providers.find((p) => p.id === e.provider)?.kind === "claude_code" ? html`<span class="faint" title="Тулы Бендера идут в Claude Code через петлевой MCP">тулы: MCP</span>`
        : html`<select aria-label=${"Передача тулов для роли "+info.title} class="select" style="width:auto" title="Как отдавать модели тулы" value=${e.params?.tool_protocol || ""}
        onChange=${(ev) => upd(i, { params: { ...(e.params || {}), tool_protocol: ev.target.value || undefined } })}>
        <option value="">тулы: сам</option><option value="native">тулы: через API</option><option value="text">тулы: текстом</option></select>`}
      <${IconButton} small icon="x" title="Убрать" onClick=${() => setItems(items.filter((_, j) => j !== i))} />
      ${!["ears","voice","embed"].includes(role) ? html`<${Button} small disabled=${busy||!e.model} onClick=${()=>check(e)}>Проверить</${Button}>`:null}
    </div>${!["ears","voice","embed"].includes(role)?html`<${EffortEditor} entry=${e} role=${role} index=${i} levels=${effortLevels} modes=${effortModes} reasoning=${reasoning} onChange=${params=>upd(i,{params})}/>`:null}<${MediaCapabilities} provider=${providers.find(p=>p.id===e.provider)} model=${e.model} audioAvailable=${audioAvailable} onSaved=${onSaved}/>`)}
    <div class="row"><${Button} small icon="plus" onClick=${add} disabled=${!providers.length}>Добавить ${items.length ? "запасную" : "модель"}</${Button}>
      <${Button} small kind="primary" busy=${busy} onClick=${() => save(items.filter((x) => x.provider && x.model))}>Сохранить</${Button}>
      ${agentsCount > 1 ? html`<${Button} small busy=${busy} title="Та же цепочка всем агентам стаи" onClick=${saveAll}>Применить ко всем агентам</${Button}>` : null}</div>
    ${probe ? html`<p class="note">${probe.state}${probe.http ? ` · HTTP ${probe.http}` : ''}</p>${probe.body ? html`<details><summary>Подробности</summary><pre class="block">${probe.body}</pre></details>`:null}`:null}
  </div>`;
}

function Fuel({ data, onChange }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  useEvents(ev => { if (ev.type === 'fuel') onChange(ev.fuel); }, [onChange]);
  const refresh = async () => {
    setBusy(true); setError(null);
    try { onChange(await post('/api/fuel/refresh')); } catch(e) { setError(e); }
    setBusy(false);
  };
  const date = ts => ts ? new Date(ts * 1000).toLocaleString('ru', {day:'2-digit',month:'2-digit',hour:'2-digit',minute:'2-digit'}) : 'неизвестно';
  return html`<div class="card fuel"><div class="card-title"><h3>⛽ Топливо</h3><${Button} small busy=${busy} onClick=${refresh}>Обновить</${Button}></div>
    <p class="note">Окна из последних ответов. Кнопка читает сохранённые квоты OmniRoute; модель не вызывается. Выключатель и уведомления — Настройки → Топливо.</p>
    ${!data?.enabled ? html`<p>Сбор выключен.</p>` : !data?.accounts?.length ? html`<p class="faint">Пока нет данных. Они появятся после ответа провайдера.</p>` : null}
    ${(data?.accounts || []).map(a => html`<div class="fuel-account"><b>${a.name} · аккаунт ${a.account}</b>
      ${a.windows.map(w => { const expired = w.reset_at && w.reset_at <= Date.now()/1000; return html`<div class="fuel-window"><div class="row"><span class="grow">${w.name}</span><span>${expired ? 'окно истекло, ждём измерения' : w.used_percent == null ? `остаток ${w.remaining}` : `${Math.round(100-w.used_percent)}% осталось`}</span></div>
        ${w.used_percent != null && !expired ? html`<progress aria-label=${a.name+' '+w.name} max="100" value=${w.used_percent} style="width:100%"></progress>` : null}
        <div class="faint">Измерено ${date(w.observed_at)} · сброс ${date(w.reset_at)}</div></div>`; })}
      ${a.models.map(m => html`<div class="note"><span title=${m.reason||m.health}>${m.health==='ok' ? '🟢' : m.health==='cooling' ? '🟠' : '⚪'}</span> ${m.id} · ${m.seconds == null ? 'задержка неизвестна' : m.seconds+' с'} · успех ${date(m.last_success)}
        ${m.error ? html`<details><summary>Последняя ошибка ${m.error.http || m.error.class} · ${date(m.error.at)}</summary><pre class="block">${m.error.body}</pre></details>` : null}</div>`)}
      ${a.last_limit_error ? html`<p class="note amber">Лимит: HTTP ${a.last_limit_error.http} · ${date(a.last_limit_error.at)}</p>` : null}
    </div>`)}
    ${data?.refresh_error ? html`<p class="note amber">${data.refresh_error}</p>` : null}<${ErrorBox} error=${error}/>
  </div>`;
}

function LocalVoice({onChange}){
  const [data,setData]=useState(null),[result,setResult]=useState(null),[error,setError]=useState(null),[busy,setBusy]=useState(false);
  const load=()=>get('/api/voice/local').then(setData,setError);useEffect(()=>{load();},[]);
  const action=async name=>{setBusy(true);setError(null);try{setResult(await post('/api/voice/local/'+name));await load();if(onChange)onChange();}catch(problem){setError(problem);}finally{setBusy(false);}};
  return html`<div class="card"><h3>Локальный голос на этой машине</h3><p class="faint">GigaAM v3 и Piper работают отдельной необязательной службой. Установка из терминала: <code>${'bender voice install --agent '+(data?.agent||'main')}</code>. Для Pi нужна 64-битная ОС; файлы моделей проверяются по хешам.</p>
    ${data?html`<p>${data.installed?'Пакет установлен.':'Пакет ещё не установлен.'} ${data.settings.enabled?'Локальная цепочка включена.':'Локальная цепочка выключена.'}</p><p class="faint small">WAV/Opus; порт ${data.settings.port}. Порт и потоки применяются после перезапуска отдельной голосовой службы; другой голос — повторной установкой. Перезапуск ядра не нужен. Выключатели — <a href="#/settings">Настройки → Локальный голос</a>.</p>`:null}
    ${data?html`<p>Агент ${data.agent}: Уши ${data.roles.ears?'подключены':'не подключены'}, Голос ${data.roles.voice?'подключён':'не подключён'}; голос ${data.enabled?'включён':'выключен'}.</p><ul>${data.agents.map(agent=>html`<li>${agent.name}: Уши ${agent.roles.ears?'да':'нет'}, Голос ${agent.roles.voice?'да':'нет'}, включён ${agent.enabled?'да':'нет'}</li>`)}</ul>`:null}
    <div class="row"><${Button} small onClick=${()=>action('connect')} busy=${busy} disabled=${!data?.installed}>Подключить этому агенту</${Button}><${Button} small onClick=${load} disabled=${busy}>Обновить состояние</${Button}><${Button} small onClick=${()=>action('bench')} busy=${busy} disabled=${!data?.installed||!data?.settings.enabled}>Измерить скорость</${Button}><${Button} small onClick=${()=>action('stop')} disabled=${busy||!data?.installed}>Остановить локальный голос</${Button}></div>
    ${result?html`<p class="note">${result.note}</p>${result.stt?html`<p>RTF: распознавание ${result.stt.rtf.toFixed(2)}, синтез ${result.tts.rtf.toFixed(2)}. ${result.advice}</p>`:null}`:null}<${ErrorBox} error=${error}/></div>`;
}

export function ModelsScreen() {
  const [data, setData] = useState(null);
  const [err, setErr] = useState(null);
  const [adding, setAdding] = useState(false);
  const [test, setTest] = useState(null);
  const load = () => get("/api/models").then(setData, setErr);
  useEffect(() => { load(); }, []);
  useEvents(ev=>{if(ev.type==='settings'&&ev.group==='reasoning')load();},[]);
  if (!data) return html`<div class="page"><div class="page-inner"><${ErrorBox} error=${err} />${!err ? html`<${Visor} mode="busy" />` : null}</div></div>`;
  const runTest = async () => {
    setTest({ busy: true });
    try { setTest(await post("/api/try/model", { text: "Кто ты? Одной фразой." })); } catch (e) { setTest({ error: e }); }
  };
  return html`<div class="page"><div class="page-inner">
    <div class="section-title">Провайдеры</div>
    <div class="list">${data.providers.map((p) => html`<div class="list-item">
      <div class="li-main"><div class="li-title">${p.name} ${p.enabled === false ? html`<span class="badge red">выключен</span>` : null} ${p.free ? html`<span class="badge green">бесплатно</span>` : p.subscription ? html`<span class="badge blue">подписка</span>` : null}</div>
        <div class="li-sub mono">${p.kind === "claude_code" ? `claude на этой машине${p.command ? ` · ${[].concat(p.command).join(" ")}` : ""}${p.billing === "api" ? " · ключ API, ходы платные" : ""}`
          : `${p.kind} · ${p.base_url}${p.keys.length ? ` · ключ ${p.keys.map((k) => k.masked).join(", ")}` : " · без ключа"}`}${p.models ? ` · моделей ${p.models.length}` : ""}</div>
        ${p.login_command ? html`<details><summary>Вход Gemini в отдельный профиль</summary><code style="overflow-wrap:anywhere">${p.login_command}</code></details>` : null}</div>
      <${Toggle} label="" title=${"Включить провайдера "+p.name} checked=${p.enabled !== false} onChange=${async (v) => { await patch(`/api/providers/${p.id}`, { enabled: v }); load(); }} />
      <${IconButton} small icon="refresh" title="Обновить каталог" onClick=${async () => { try { await post(`/api/providers/${p.id}/catalog`); toast("Каталог обновлён", "ok"); load(); } catch (e) { toast(e.message, "error"); } }} />
      <${IconButton} small icon="trash" danger title="Удалить" onClick=${async () => { if (confirm(`Удалить ${p.name}? Ключ из сейфа тоже удалится.`)) { await del(`/api/providers/${p.id}`); load(); } }} />
    </div>`)}</div>
    <div style="margin:10px 0 0"><${Button} icon="plus" onClick=${() => setAdding(true)}>Подключить провайдера</${Button}></div>
    <${Fuel} data=${data.fuel} onChange=${fuel=>setData(current=>({...current, fuel}))} /><${LocalVoice} onChange=${load}/>
    <${Reasoning} data=${data.reasoning} levels=${data.effort_levels} onSaved=${load}/>
    <div class="section-title">Роли моделей</div>
    ${data.role_notice?html`<p class="note">Удалены повреждённые кандидаты: ${data.role_notice.count}. Причина — неверная ссылка на провайдера или модель; запись сохранена в журнале.</p>`:null}
    ${data.broken_roles?.length?html`<div class="card"><h3>Повреждённые ссылки в ролях: ${data.broken_roles.length}</h3><ul>${data.broken_roles.map(item=>html`<li>${data.role_titles[item.role]?.title||item.role}, кандидат ${item.index+1}: ${item.reason} (${item.provider||'без провайдера'} · ${item.model||'без модели'})</li>`)}</ul><${Button} onClick=${async()=>{try{await post('/api/roles/repair');load();}catch(e){setErr(e);}}}>Починить ссылки в ролях</${Button}></div>`:null}
    <div class="note">Экономия встроена: дорогая модель разговаривает, дешёвая — убирается. Запасные подменяют основную только на один ответ, и это видно в разговоре.</div>
    ${Object.entries(data.role_titles).map(([role, info]) => html`<${RoleEditor} key=${role} role=${role} info=${info} chain=${data.roles[role] || []} providers=${data.providers} fuel=${data.fuel} audioAvailable=${data.audio_probe_available} effortLevels=${data.effort_levels} effortModes=${data.effort_modes} reasoning=${data.reasoning} agentsCount=${data.agents_count||1} onSaved=${load} onConnect=${()=>setAdding(role)} />`)}
    <div class="card"><div class="card-title"><${Icon} name="play" /><h3>Проверить Главную</h3><${Button} small onClick=${runTest} busy=${test?.busy}>Спросить</${Button}></div>
      ${test && !test.busy ? test.error ? html`<${ErrorBox} error=${test.error} />` : html`<div><div class="m-text">${test.text}</div>
        <div class="faint">${test.provider} · ${test.model} · ${test.usage?.input}→${test.usage?.output} токенов · ${money(test.cost)} · ${test.seconds} с</div>
        ${test.notice ? html`<div class="note amber">${test.notice}</div>` : null}</div>` : null}</div>
    ${data.health.keys.length || data.health.models.length ? html`<div class="section-title">Здоровье</div><pre class="block">${JSON.stringify(data.health, null, 1)}</pre>` : null}
    ${adding ? html`<${Modal} title="Подключить провайдера" onClose=${() => setAdding(false)}><${SetupModel} role=${adding===true?null:adding} onDone=${() => { setAdding(false); load(); }} /></${Modal}>` : null}
  </div></div>`;
}
