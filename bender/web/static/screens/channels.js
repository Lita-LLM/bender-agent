import { SpeakersPanel } from "./speakers.js";
import {MatrixPanel} from './matrix.js';
import {UserbotPanel} from './userbot.js';
// Связь: каналы, через которые хозяин говорит с Бендером (пульт и терминал есть всегда): Telegram, почта, вебхуки, API.
import { html, useEffect, useState } from "../lib/ui.js";
import { del, get, patch, post } from "../lib/api.js";
import { useEvents } from "../lib/store.js";
import { Button, ErrorBox, Field, Icon, Modal, Toggle, Visor, copyText, timeAgo, toast } from "../lib/ui.js";

const STATE = { ok: ["green", "на связи"], connecting: ["amber", "подключаюсь…"], error: ["red", "ошибка"], off: ["", "выключен"] };

function BotProfile({enabled,reload}) {
  const [values,setValues]=useState(null),[avatar,setAvatar]=useState(null),[busy,setBusy]=useState(false),[error,setError]=useState(null);
  const load=()=>get('/api/channels/telegram/profile').then(data=>setValues(data.values)).catch(setError);
  useEffect(()=>{load();},[]);
  const upload=async event=>{const file=event.target.files?.[0];event.target.value='';if(!file)return;setBusy(true);setError(null);
    try{if(file.size>10*1024*1024)throw new Error('JPG больше 10 МБ. Уменьши фотографию.');
      const data=await new Promise((resolve,reject)=>{const reader=new FileReader();reader.onload=()=>resolve(reader.result.split(',')[1]);reader.onerror=reject;reader.readAsDataURL(file);});
      setAvatar(await post('/api/attachments',{name:file.name,mime:file.type,data}));
    }catch(problem){setError(problem);}finally{setBusy(false);}
  };
  const apply=async()=>{setBusy(true);setError(null);try{
    await post('/api/channels/telegram/profile',{values,avatar:avatar?.id,reviewed:true});toast('Публичный профиль обновлён','ok');setAvatar(null);await reload();
  }catch(problem){setError(problem);}finally{setBusy(false);}};
  return html`<details class="telegram-profile"><summary>Публичное лицо общего бота</summary>
    <p class="muted small">Бот принадлежит главному агенту. Проверь поля перед публикацией; «Взять из личности» читает его IDENTITY. Для аватара нужен готовый JPG; можно прислать своё фото боту с подписью /avatar.</p>
    <${ErrorBox} error=${error}/>
    ${values?html`<${Field} label="Публичное имя"><input class="input" aria-label="Публичное имя" maxlength="64" value=${values.name||''} onInput=${e=>setValues({...values,name:e.target.value})}/></${Field}>
    <${Field} label="Описание бота"><textarea class="textarea" aria-label="Описание бота" maxlength="512" value=${values.description||''} onInput=${e=>setValues({...values,description:e.target.value})}/></${Field}>
    <${Field} label="Короткое описание"><input class="input" aria-label="Короткое описание" maxlength="120" value=${values.short_description||''} onInput=${e=>setValues({...values,short_description:e.target.value})}/></${Field}>
    <${Field} label="JPG аватара"><input type="file" accept="image/jpeg,.jpg,.jpeg" aria-label="JPG аватара" disabled=${busy} onChange=${upload}/>${avatar?html`<span class="faint small">${avatar.name} · ${avatar.size} байт</span>`:null}</${Field}>
    <div class="row"><${Button} small onClick=${load} disabled=${busy}>Взять из личности</${Button}><${Button} kind="primary" busy=${busy} disabled=${!enabled} onClick=${apply}>Применить публичный профиль</${Button}></div>`:html`<${Visor} mode="busy"/>`}
  </details>`;
}

function TelegramChats({settings,setS}) {
  const [rows,setRows]=useState([]),[error,setError]=useState(null);
  const load=()=>get('/api/channels/telegram/chats').then(data=>setRows(data.chats)).catch(setError);
  useEffect(()=>{load();},[]);
  const change=async(row,values)=>{try{await patch('/api/channels/telegram/chats/'+row.id,values);await load();}catch(problem){setError(problem);}};
  return html`<details class="telegram-chats"><summary>Где находится бот</summary>
    <${ErrorBox} error=${error}/>
    <${Toggle} label="Отвечать в разрешённых группах" checked=${settings.groups_enabled} onChange=${value=>setS({groups_enabled:value})}/>
    <${Toggle} label="Выходить из групп, куда добавил не хозяин" checked=${settings.leave_unknown_groups} onChange=${value=>setS({leave_unknown_groups:value})}/>
    <p class="faint small">В группе бот отвечает только на упоминание или реплай хозяина. Контекст остальных участников включается отдельно и считается недоверенным.</p>
    ${rows.length?rows.map(row=>html`<div class="list-item" data-chat=${row.id}><div class="li-main"><b>${row.chat.title||row.chat.first_name||row.id}</b><div class="faint small">${row.chat.type} · ${row.id} · добавил ${row.added_by?.first_name||row.added_by?.id||'неизвестно'}${row.added_at?' · '+new Date(row.added_at*1000).toLocaleString('ru-RU'):''}</div>
      ${row.member?html`<details><summary class="faint small">Права, сообщённые Telegram</summary><pre class="block">${JSON.stringify(row.member,null,2)}</pre></details>`:null}
      <${Toggle} label=${'Разрешить группу '+row.id} checked=${row.allowed} onChange=${value=>change(row,{enabled:value})}/>
      <${Toggle} label=${'Контекст участников '+row.id} checked=${row.context} onChange=${value=>change(row,{context:value})}/></div></div>`):html`<div class="faint">Telegram пока не прислал обновлений о чатах.</div>`}
  </details>`;
}

function Telegram({ tg, reload }) {
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState("");
  const topics=tg.topics||[];
  const act = (name, fn) => async () => { setBusy(name); try { await fn(); await reload(); } catch (e) { toast(e.message, "error"); } finally { setBusy(""); } };
  const setS = (changes) => act("s", () => post("/api/settings/telegram", changes))();
  const [cls, word] = STATE[tg.state] || ["", tg.state];
  const link = tg.bot && tg.code ? `https://t.me/${tg.bot.username}?start=${tg.code}` : null;
  return html`<div class="card"><div class="card-title"><${Icon} name="send" /><h3>Telegram</h3>
      ${tg.has_token ? html`<span class=${`badge ${cls}`}>${tg.enabled ? word : "выключен"}</span>` : null}</div>
    ${!tg.has_token ? html`
      <div class="steps">
        <div><b>1.</b> Открой в Telegram <a href="https://t.me/BotFather" target="_blank" rel="noopener">@BotFather</a>, отправь <span class="mono">/newbot</span>, придумай имя — он пришлёт токен.</div>
        <div><b>2.</b> Вставь токен сюда:</div>
        <div class="row"><input aria-label="123456789:AAE…" class="input mono grow" value=${token} placeholder="123456789:AAE…" onInput=${(e) => setToken(e.target.value.trim())} />
          <${Button} kind="primary" busy=${busy === "token"} disabled=${!token} onClick=${act("token", () => post("/api/channels/telegram/token", { token }))}>Подключить</${Button}></div>
        <div class="faint small">Токен хранится в сейфе Бендера и уходит только на api.telegram.org.</div>
      </div>` : html`
      ${tg.bot ? html`<div>Бот: <a href=${`https://t.me/${tg.bot.username}`} target="_blank" rel="noopener">@${tg.bot.username}</a></div>` : null}
      ${tg.error ? html`<div class="note red" style="margin-top:8px">${tg.error}</div>` : null}
      ${tg.pause ? html`<div class="note amber" style="margin-top:8px">Telegram попросил паузу ещё ${tg.pause < 120 ? tg.pause + " с" : Math.ceil(tg.pause / 60) + " мин"}: слишком частые сообщения. Входящие и команды доходят, ответы уйдут после паузы. Если повторяется — выключи живой ответ командой /live off.</div>` : null}
      ${tg.owner ? html`<div style="margin-top:6px">Хозяин: <b>${tg.owner.name || tg.owner.username || "привязан"}</b>
          <${Button} small kind="ghost" onClick=${act("unbind", () => { if (confirm("Отвязать хозяина? Бот перестанет отвечать, пока не привяжешь заново.")) return del("/api/channels/telegram/owner"); })}>Отвязать</${Button}></div>
        ${tg.owner.name === "хозяин из OpenClaw" && !tg.enabled ? html`<div class="note amber" style="margin-top:8px">Бот и хозяин пришли из OpenClaw. Останови OpenClaw (бота может читать только один) и включи канал.</div>` : null}`
      : html`<div class="steps" style="margin-top:6px"><div><b>3.</b> Привяжи себя: ${link ? html`открой <a href=${link} target="_blank" rel="noopener">ссылку</a> и нажми «Старт» — или отправь боту`
          : "отправь боту"} <span class="mono big-code-sm">/start ${tg.code || "…"}</span> ${tg.code ? html`<${Button} small kind="ghost" icon="copy" onClick=${() => copyText(`/start ${tg.code}`).then(() => toast("Скопировано", "ok"))}>Копировать</${Button}>` : null}</div>
          <div class="faint small">Код живёт 15 минут. <a href="#" onClick=${(e) => { e.preventDefault(); act("code", () => post("/api/channels/telegram/code"))(); }}>Новый код</a></div></div>`}
      <div style="margin-top:10px">
        <${Toggle} label="Канал включён" checked=${tg.enabled} onChange=${(v) => setS({ enabled: v })} hint="Выключенный бот молчит; включённый держит один долгий запрос к Telegram — в простое ни ЦП, ни записи." />
        <${Toggle} label="Обновлять меню команд" checked=${tg.settings.commands} onChange=${v=>setS({commands:v})} hint="Старое меню удаляется, свои команды описаны по-русски. Полный обход языков нужен один раз." />
        <${Toggle} label="Темы разговоров в личке" checked=${tg.settings.topics} onChange=${v=>setS({topics:v})} hint="Включи темы у @BotFather и переподключи канал. /new создаёт тему выбранного агента; общий поток ведёт его главный разговор." />
        <div class="faint small">Темы у бота: ${tg.bot?.has_topics_enabled?'включены':'ещё не включены у @BotFather'}.</div>
        <details><summary>Темы агентов</summary>${topics.length?topics.map(topic=>html`<div class="faint small">${topic.name}</div>`):html`<p class="faint small">Создай /new или выбери «Новый чат» в /chats.</p>`}</details>
        <${Toggle} label="Имя другого агента перед ответом" checked=${tg.settings.agent_label} onChange=${v=>setS({agent_label:v})} hint="Когда темы выключены; главный агент представлен профилем бота." />
        <div class="faint small">Меню: ${tg.commands?.running?'обновляется':tg.commands?.ready?'обновлено':'ещё не обновлялось'}. /help — команды по разделам, /agent и /chats — выбор собеседника.</div>
        <${Toggle} label="Расширенное оформление" checked=${tg.settings.rich} onChange=${v=>setS({rich:v})} hint="Таблицы, заголовки и мысли в черновике. Старый Bot API получает обычный HTML." />
        <div class="faint small">Rich: ${tg.rich?.messages===true?'поддерживается':tg.rich?.messages===false?'обычный HTML':'проверится при отправке'} · черновик: ${tg.rich?.drafts===true?'Rich':tg.rich?.drafts===false?'обычный':'ещё не проверен'}</div>
        <${Toggle} label="Напоминания и тревоги — сюда" checked=${tg.settings.notify} onChange=${(v) => setS({ notify: v })} />
        <${Toggle} label="Одобрения и вопросы — кнопками" checked=${tg.settings.approvals} onChange=${(v) => setS({ approvals: v })} />
      </div>
      <div class="faint small" style="margin-top:6px">Команды боту: /stop — стоп-кран, /resume — снять стоп, /status — самочувствие. Чужим бот не отвечает (только вежливо раз в час).</div>
      <${BotProfile} enabled=${tg.enabled} reload=${reload}/><${TelegramChats} settings=${tg.settings} setS=${setS}/><div style="margin-top:8px"><${Button} small kind="ghost" onClick=${act("forget", () => { if (confirm("Удалить токен бота из Бендера?")) return del("/api/channels/telegram/token"); })}>Удалить токен</${Button}></div>`}
  </div>`;
}

const SECURITY = [["ssl", "SSL/TLS"], ["starttls", "STARTTLS"], ["none", "без шифрования"]];
const MAIL_IMAGES = [["known", "только от знакомых и от меня"], ["all", "от всех"], ["off", "не сохранять"]];
const mailNames = (list) => (list || []).map((a) => (typeof a === "string" ? a : a?.name || "")).filter(Boolean);
const ON_NEW = [["important", "сказать мне о важных"], ["all", "сказать мне о каждом"], ["agent", "отдать агенту (он решит сам)"], ["quiet", "только складывать"]];

function verdictBadges(v) {
  if (!v) return null;
  return html`${v.spam ? html`<span class="badge red">спам</span>` : null}${v.important ? html`<span class="badge amber">важное</span>` : null}${v.needs_reply ? html`<span class="badge blue">ждёт ответа</span>` : null}<span class="badge">${v.category}</span>`;
}

function MailSetup({ m, reload }) {
  const s = m.settings || {};
  const [f, setF] = useState({ address: s.address || "", login: s.login || "", imap_host: s.imap_host || "", imap_port: s.imap_port || 993, imap_security: s.imap_security || "ssl",
    smtp_host: s.smtp_host || "", smtp_port: s.smtp_port || 465, smtp_security: s.smtp_security || "ssl", folder: s.folder || "INBOX" });
  const [pw, setPw] = useState("");
  const [hint, setHint] = useState(m.guess);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const up = (k) => (e) => setF({ ...f, [k]: e.target.value });
  const guessFrom = async () => {
    if (!/@.+\./.test(f.address)) return;
    try { const g = await get(`/api/channels/mail/guess?address=${encodeURIComponent(f.address)}`); setHint(g);
      setF((x) => ({ ...x, imap_host: g.imap_host, imap_port: g.imap_port, imap_security: g.imap_security, smtp_host: g.smtp_host, smtp_port: g.smtp_port, smtp_security: g.smtp_security })); } catch {}
  };
  const connect = async () => {
    setBusy(true); setErr(null);
    try { await post("/api/channels/mail", { ...f, imap_port: +f.imap_port, smtp_port: +f.smtp_port, password: pw || undefined, check: true, enabled: true }); toast("Почта подключена", "ok"); reload(); }
    catch (e) { setErr(e); }
    setBusy(false);
  };
  return html`<div class="mail-setup">
    <${Field} label="Адрес"><input aria-label="me@example.com" class="input" type="email" value=${f.address} placeholder="me@example.com" onInput=${up("address")} onBlur=${guessFrom} /></${Field}>
    ${hint?.hint ? html`<div class=${`note ${/отключил|Bridge/.test(hint.hint) ? "amber" : ""}`}>${hint.hint}</div>` : null}
    <${Field} label="Пароль" hint="Лучше пароль приложения: его можно отозвать, не меняя основного. Хранится в сейфе на этой машине.">
      <input aria-label=${m.has_password ? "уже сохранён — оставь пустым" : ""} class="input mono" type="password" autocomplete="off" value=${pw} placeholder=${m.has_password ? "уже сохранён — оставь пустым" : ""} onInput=${(e) => setPw(e.target.value)} /></${Field}>
    <details class="mail-servers"><summary class="faint">Серверы${f.imap_host ? `: ${f.imap_host}, ${f.smtp_host || "без отправки"}` : ""}</summary>
      <div class="grid2">
        <${Field} label="IMAP (входящие)"><input class="input mono" name="imap_host" value=${f.imap_host} onInput=${up("imap_host")} /></${Field}>
        <${Field} label="Порт и защита"><div class="row port-row"><input class="input mono port" name="imap_port" value=${f.imap_port} onInput=${up("imap_port")} />
          <select class="select" name="imap_security" value=${f.imap_security} onChange=${up("imap_security")}>${SECURITY.map(([k, t]) => html`<option value=${k}>${t}</option>`)}</select></div></${Field}>
        <${Field} label="SMTP (исходящие)"><input class="input mono" name="smtp_host" value=${f.smtp_host} onInput=${up("smtp_host")} /></${Field}>
        <${Field} label="Порт и защита"><div class="row port-row"><input class="input mono port" name="smtp_port" value=${f.smtp_port} onInput=${up("smtp_port")} />
          <select class="select" name="smtp_security" value=${f.smtp_security} onChange=${up("smtp_security")}>${SECURITY.map(([k, t]) => html`<option value=${k}>${t}</option>`)}</select></div></${Field}>
        <${Field} label="Логин" hint="Пусто — как адрес"><input class="input mono" name="login" value=${f.login} onInput=${up("login")} /></${Field}>
        <${Field} label="Папка"><input class="input mono" name="folder" value=${f.folder} onInput=${up("folder")} /></${Field}>
      </div></details>
    <${Button} kind="primary" busy=${busy} disabled=${!f.address || !f.imap_host || (!pw && !m.has_password)} onClick=${connect}>Проверить и подключить</${Button}>
    <${ErrorBox} error=${err} onClose=${() => setErr(null)} />
  </div>`;
}

function Letter({ id, onClose }) {
  const [L, setL] = useState(null);
  useEffect(() => { get(`/api/mail/${id}`).then((r) => setL(r.letter), (e) => toast(e.message, "error")); }, [id]);
  return html`<${Modal} title=${L ? L.subject || "без темы" : "Письмо"} onClose=${onClose} wide>${!L ? html`<${Visor} mode="busy" />` : html`<div>
    <dl class="kv"><dt>От</dt><dd>${L.from_name} <span class="mono faint">${L.from_addr}</span></dd><dt>Когда</dt><dd>${new Date(L.date * 1000).toLocaleString("ru-RU")}</dd>
      ${L.verdict ? html`<dt>Чтец</dt><dd>${verdictBadges(L.verdict)}${L.summary ? html`<div class="faint">${L.summary}</div>` : null}</dd>` : null}
      ${mailNames(L.attachments).length ? html`<dt>Вложения</dt><dd>${mailNames(L.attachments).join(", ")}</dd>` : null}
      ${L.images?.length ? html`<dt>Картинки</dt><dd>${L.images.map((i) => (i.path ? i.name : `${i.name} — не сохранена: ${i.skipped || "?"}`)).join(", ")}</dd>` : null}</dl>
    <pre class="block letter-body">${L.body}</pre></div>`}</${Modal}>`;
}

function Mail({ m, reload }) {
  const [busy, setBusy] = useState("");
  const [edit, setEdit] = useState(false);
  const [open, setOpen] = useState(null);
  const s = m.settings || {};
  const [known, setKnown] = useState(Object.entries(s.known || {}).map(([a, l]) => `${a} = ${l}`).join("\n"));
  const act = (name, fn) => async () => { setBusy(name); try { await fn(); await reload(); } catch (e) { toast(e.message, "error"); } finally { setBusy(""); } };
  const setS = (changes) => act("s", () => post("/api/channels/mail", changes))();
  const saveKnown = () => setS({ known: Object.fromEntries(known.split("\n").map((l) => l.split("=")).filter((p) => p.length >= 2 && p[0].includes("@")).map(([a, ...l]) => [a.trim().toLowerCase(), l.join("=").trim()])) });
  const [cls, word] = { ok: ["green", "на связи"], checking: ["amber", "проверяю…"], error: ["red", "ошибка"], off: ["", "выключена"] }[m.state] || ["", m.state];
  return html`<div class="card"><div class="card-title"><${Icon} name="mail" /><h3>Почта</h3>
      ${m.configured ? html`<span class=${`badge ${cls}`}>${m.enabled ? word : "выключена"}</span>` : null}</div>
    ${!m.configured || edit ? html`<div>
      <div class="faint">Бендер смотрит твой ящик только для чтения: у тебя письма не становятся прочитанными. Новые письма разбирает Чтец — модель без рук; сам агент письмо читает, только когда нужно, и тогда его ход считается заражённым.</div>
      <${MailSetup} m=${m} reload=${() => { setEdit(false); reload(); }} />
      ${edit ? html`<${Button} small kind="ghost" onClick=${() => setEdit(false)}>Отмена</${Button}>` : null}</div>` : html`<div>
      <div class="row wrap"><span class="mono">${s.address}</span><span class="faint">${m.last_check ? `проверено ${timeAgo(m.last_check)}` : "ещё не проверялась"} · писем ${m.count}${m.new ? `, новых ${m.new}` : ""}</span></div>
      ${m.error ? html`<div class="note red" style="margin-top:8px">${m.error}</div>` : null}
      <div style="margin-top:8px">
        <${Toggle} label="Почта включена" checked=${m.enabled} onChange=${(v) => setS({ enabled: v })} hint=${`Короткое соединение раз в ${s.poll_minutes} мин; в промежутках — ни сети, ни записи.`} />
        <${Toggle} label="Чтец разбирает новые письма" checked=${s.triage} onChange=${(v) => setS({ triage: v })} hint="Спам, важное, ждёт ли ответа — дешёвой моделью, без рук." />
        <${Field} label="Новое письмо"><select class="select" value=${s.on_new} onChange=${(e) => setS({ on_new: e.target.value })}>${ON_NEW.map(([k, t]) => html`<option value=${k}>${t}</option>`)}</select></${Field}>
        <${Field} label="Картинки из писем" hint="Сохранять во входящие и показывать агенту при чтении письма. Адрес отправителя можно подделать."><select class="select" name="images" value=${s.images || "known"} onChange=${(e) => setS({ images: e.target.value })}>${MAIL_IMAGES.map(([k, t]) => html`<option value=${k}>${t}</option>`)}</select></${Field}>
        <${Field} label="Проверять раз в, минут"><input class="input port" type="number" min="1" value=${s.poll_minutes} onChange=${(e) => setS({ poll_minutes: +e.target.value })} /></${Field}>
        <details><summary class="faint">Знакомые адреса — агент видит подпись вместо адреса</summary>
          <textarea aria-label="boss@work.ru = начальник" class="textarea mono" rows="3" value=${known} placeholder="boss@work.ru = начальник" onInput=${(e) => setKnown(e.target.value)}></textarea>
          <${Button} small onClick=${saveKnown}>Сохранить</${Button}></details>
      </div>
      <div class="row wrap" style="margin-top:8px">
        <${Button} small icon="refresh" busy=${busy === "poll"} onClick=${act("poll", () => post("/api/channels/mail/poll"))}>Проверить сейчас</${Button}>
        <${Button} small kind="ghost" onClick=${() => setEdit(true)}>Ящик и серверы</${Button}>
        <${Button} small kind="ghost" onClick=${act("forget", () => { if (confirm("Отключить почту и удалить пароль из Бендера?")) return del("/api/channels/mail"); })}>Отключить</${Button}>
      </div>
      ${m.letters?.length ? html`<div class="list mail-list" style="margin-top:10px">${m.letters.map((L) => html`<div class="list-item clickable" role="button" tabindex="0" aria-label=${L.subject||"Письмо"} onKeyDown=${e=>{if(e.target===e.currentTarget&&["Enter"," "].includes(e.key)){e.preventDefault();setOpen(L.id);}}} onClick=${() => setOpen(L.id)}>
        <div class="li-main"><div class="li-title">${L.dir === "out" ? html`→ ${L.to_addr}` : L.from_name || L.from_addr} <span class="faint small">${timeAgo(L.date)}</span></div>
          <div class="li-sub">${L.subject || "без темы"}</div>
          ${L.verdict || L.summary ? html`<div class="li-sub">${verdictBadges(L.verdict)} ${L.summary ? html`<span class="faint">${L.summary}</span>` : null}</div>` : null}</div></div>`)}</div>` : null}
    </div>`}
    ${open ? html`<${Letter} id=${open} onClose=${() => setOpen(null)} />` : null}
  </div>`;
}

function Hooks({ reload: reloadAll }) {
  const [d, setD] = useState(null);
  const [f, setF] = useState({ name: "", action: "notify", instruction: "", per_hour: 30, agent: "main" });
  const [events, setEvents] = useState({});
  const [adding, setAdding] = useState(false);
  const [busy, setBusy] = useState(false);
  const load = () => get("/api/hooks").then(setD, (e) => toast(e.message, "error"));
  useEffect(() => { load(); }, []);
  useEvents((ev) => { if (ev.type === "channel" && ev.channel === "hooks") load(); }, []);
  const create = async () => {
    setBusy(true);
    try { await post("/api/hooks", { ...f, per_hour: +f.per_hour }); setF({ name: "", action: "notify", instruction: "", per_hour: 30, agent: "main" }); setAdding(false); load(); } catch (e) { toast(e.message, "error"); }
    setBusy(false);
  };
  const upd = async (h, changes) => { try { await patch(`/api/hooks/${h.id}`, changes); load(); } catch (e) { toast(e.message, "error"); } };
  const loadEvents = (h) => get(`/api/hooks/${h.id}/events`).then((r) => setEvents((x) => ({ ...x, [h.id]: r.events })), (e) => toast(e.message, "error"));
  const retry = async (h, ev) => { try { await post(`/api/hook-events/${ev.event}/retry`); loadEvents(h); } catch (e) { toast(e.message, "error"); } };
  const STATES = { queued: "принято", waiting: "ждёт агента", running: "в ходе", done: "ход завершён", failed: "ошибка", interrupted: "прервано перезапуском" };
  const agentName = (id) => (d && d.agents.find((a) => a.id === id) || { name: id }).name;
  if (!d) return null;
  return html`<div class="card"><div class="card-title"><${Icon} name="link" /><h3>Вебхуки</h3></div>
    <div class="faint">Адрес, в который стучатся другие программы: GitHub, мониторинг, умный дом, свои скрипты. Вход — секретом (заголовок, параметр <span class="mono">token</span> или подпись как у GitHub). Снаружи адрес доступен, только если пульт открыт в сеть или через туннель (cloudflared, Tailscale Funnel).</div>
    ${d.hooks.length ? html`<div class="list" style="margin-top:8px">${d.hooks.map((h) => {
      const url = `${location.origin}${h.path}`;
      const curl = `curl -X POST ${url} \\\n  -H "Authorization: Bearer ${h.secret}" -H "Content-Type: application/json" \\\n  -d '{"text": "проверка"}'`;
      return html`<div class="list-item hook">
        <div class="li-main"><div class="li-title">${h.name} <span class="badge">${h.action_title}</span> ${h.last_status === "лимит" ? html`<span class="badge red">лимит</span>` : null}</div>
          <div class="li-sub">${h.count ? `запросов ${h.count}, последний ${timeAgo(h.last_at)}` : "запросов ещё не было"} · не больше ${h.per_hour} в час</div>
          ${h.action === "agent" ? html`<div class="li-sub">Поручение: ${h.instruction}</div>` : null}
          ${d.agents.length > 1 ? html`<div class="li-sub">Агент: <select class="select small" aria-label="Агент-адресат" value=${h.agent} onChange=${(e) => upd(h, { agent: e.target.value })}>
            ${d.agents.map((a) => html`<option value=${a.id}>${a.name}</option>`)}${d.agents.some((a) => a.id === h.agent) ? null : html`<option value=${h.agent}>${h.agent} (недоступен)</option>`}</select></div>` : null}
          <div class="li-sub mono api-key">${url}</div>
          <div class="row wrap"><${Button} small kind="ghost" icon="copy" onClick=${() => copyText(url).then(() => toast("Адрес скопирован", "ok"))}>Адрес</${Button}>
            <${Button} small kind="ghost" icon="key" onClick=${() => copyText(h.secret).then(() => toast("Секрет скопирован", "ok"))}>Секрет</${Button}></div>
          <details><summary class="faint">Пример</summary><pre class="block">${curl}</pre>
            <${Button} small kind="ghost" onClick=${async () => { if (confirm("Новый секрет? Со старым программы перестанут достукиваться.")) { await post(`/api/hooks/${h.id}/secret`); load(); } }}>Новый секрет</${Button}></details>
          ${h.action === "agent" ? html`<details onToggle=${(e) => e.target.open && loadEvents(h)}><summary class="faint">События</summary>
            ${(events[h.id] || []).length ? html`<div class="list">${events[h.id].map((ev) => html`<div class="li-sub">${timeAgo(ev.received)} · ${agentName(ev.agent)} · ${STATES[ev.state] || ev.state}${ev.error ? ` — ${ev.error}` : ""}
              ${["failed", "interrupted"].includes(ev.state) ? html` <${Button} small kind="ghost" onClick=${() => retry(h, ev)}>Повторить</${Button}>` : null}</div>`)}</div>` : html`<div class="faint">Событий ещё не было.</div>`}</details>` : null}</div>
        <${Toggle} label="" checked=${h.enabled} onChange=${(v) => upd(h, { enabled: v })} />
        <button class="icon-btn small danger" title="Удалить" onClick=${async () => { if (confirm(`Удалить вебхук «${h.name}»?`)) { await del(`/api/hooks/${h.id}`); load(); } }}><${Icon} name="trash" /></button>
      </div>`; })}</div>` : null}
    ${adding ? html`<div class="hook-form" style="margin-top:10px">
      <${Field} label="Имя"><input aria-label="GitHub, датчик в ванной…" class="input" value=${f.name} placeholder="GitHub, датчик в ванной…" onInput=${(e) => setF({ ...f, name: e.target.value })} /></${Field}>
      <${Field} label="Что делать"><select class="select" value=${f.action} onChange=${(e) => setF({ ...f, action: e.target.value })}>
        <option value="notify">сказать мне (без модели)</option><option value="agent">ход агента</option></select></${Field}>
      ${d.agents.length > 1 ? html`<${Field} label="Агент" hint="Кому идёт запрос. Заголовок или параметр в самом запросе адресата не меняет.">
        <select class="select" value=${f.agent} onChange=${(e) => setF({ ...f, agent: e.target.value })}>${d.agents.map((a) => html`<option value=${a.id}>${a.name}</option>`)}</select></${Field}>` : null}
      ${f.action === "agent" ? html`<${Field} label="Поручение агенту" hint="Содержимое запроса агент получит как недоверенное; хозяина рядом нет — рискованное без одобрения не пройдёт.">
        <textarea aria-label="Если сборка упала — разберись, что сломалось, и скажи мне." class="textarea" rows="2" value=${f.instruction} placeholder="Если сборка упала — разберись, что сломалось, и скажи мне." onInput=${(e) => setF({ ...f, instruction: e.target.value })}></textarea></${Field}>` : null}
      <${Field} label="Не больше запросов в час"><input class="input port" type="number" min="1" value=${f.per_hour} onInput=${(e) => setF({ ...f, per_hour: e.target.value })} /></${Field}>
      <div class="row"><${Button} kind="primary" busy=${busy} disabled=${!f.name.trim()} onClick=${create}>Создать</${Button}><${Button} kind="ghost" onClick=${() => setAdding(false)}>Отмена</${Button}></div>
    </div>` : html`<div style="margin-top:8px"><${Button} small icon="plus" onClick=${() => setAdding(true)}>Новый вебхук</${Button}></div>`}
  </div>`;
}

function Api({ a, reload }) {
  const [busy, setBusy] = useState(false);
  const base = `${location.origin}/v1`;
  const setS = async (changes) => { try { await post("/api/settings/api", changes); reload(); } catch (e) { toast(e.message, "error"); } };
  const newKey = async () => {
    if (a.key && !confirm("Сделать новый ключ? Программы со старым ключом перестанут подключаться.")) return;
    setBusy(true); try { await post("/api/channels/api/key"); reload(); } catch (e) { toast(e.message, "error"); } finally { setBusy(false); }
  };
  const example = `curl ${base}/chat/completions \\\n  -H "Authorization: Bearer ${a.key || "КЛЮЧ"}" -H "Content-Type: application/json" \\\n  -d '{"model": "bender/${a.agent_id||'main'}", "user":"чат-пример", "messages": [{"role": "user", "content": "привет"}]}'`;
  return html`<div class="card api-channel"><div class="card-title"><${Icon} name="cpu" /><h3>API для программ</h3>${a.enabled ? html`<span class="badge green">включено</span>` : null}</div>
    <div class="faint">Бендер как «модель» OpenAI: любая программа, которая умеет OpenAI API (чат-клиент, редактор, скрипт), говорит с ним — с его памятью, тулами и Предохранителем.</div>
    <${Toggle} label="API включено" checked=${a.enabled} onChange=${(v) => setS({ enabled: v })} />
    <div class="kv" style="margin-top:8px"><dt>Адрес</dt><dd class="mono">${base}</dd>
      <dt>Ключ</dt><dd>${a.key ? html`<span class="mono api-key">${a.key}</span> <${Button} small kind="ghost" icon="copy" onClick=${() => copyText(a.key).then(() => toast("Скопировано", "ok"))}>Копировать</${Button}>` : html`<span class="faint">нет</span>`}
        <${Button} small busy=${busy} onClick=${newKey}>${a.key ? "Новый ключ" : "Создать ключ"}</${Button}></dd>
      <dt>Модели</dt><dd>${Object.entries(a.models).map(([m, d]) => html`<div><span class="mono">${m}</span> <span class="faint">— ${d}</span></div>`)}</dd></div>
    <p class="note">У каждого агента модель bender/id и отдельные чаты API. Передавай уникальный id разговора в user или X-Bender-Chat (заголовок имеет приоритет). Без id — новый чат на запрос. Права канала по умолчанию «Помощник», меняет хозяин в <a href=${'#/rights?agent='+(a.agent_id||'main')}>Правах агента → API</a>.</p>
    <details style="margin-top:8px"><summary class="faint">Пример</summary><pre class="block">${example}</pre></details>
  </div>`;
}

export function ChannelsScreen() {
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);
  const load = () => Promise.all([get("/api/channels"), get("/api/channels/api"), get("/api/channels/mail")]).then(([c, a, m]) => setD({ ...c, api: a, mail: m }), setErr);
  useEffect(() => { load(); }, []);
  useEvents((ev) => { if ((ev.type === "channel" && ev.channel !== "hooks") || (ev.type === "settings" && ["telegram", "mail", "api", "api_rights", "flock", "agent_rights"].includes(ev.group))) load(); }, []);
  if (!d) return html`<div class="page"><div class="page-inner"><${ErrorBox} error=${err} /><${Visor} mode="busy" /></div></div>`;
  return html`<div class="page"><div class="page-inner">
    <div class="row"><h2 class="grow">Связь</h2><${Button} small onClick=${load}>Обновить связи</${Button}></div>
    <div class="note">У каждого агента свои разговоры. Их можно продолжать в пульте и Telegram; ответ приходит в канал и тему обращения.</div>
    <${Telegram} tg=${d.telegram} reload=${load} />
    <${UserbotPanel}/>
    <${MatrixPanel}/>
    <${SpeakersPanel}/>
    <${Mail} m=${d.mail} reload=${load} />
    <${Hooks} reload=${load} />
    <${Api} a=${d.api} reload=${load} />
  </div></div>`;
}
