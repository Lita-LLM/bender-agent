// Бендер: точка входа пульта.
import { html, render, useEffect } from "./lib/ui.js";
import { listen, post } from "./lib/api.js";
import { emit, refresh, refreshSoon, setState, state, useApp } from "./lib/store.js";
import { Button, ErrorBox, Icon, IconButton, Visor, money, toast } from "./lib/ui.js";
import {ControlButton} from './lib/lifecycle.js';
import { AgentsScreen, switchAgent } from "./screens/agents.js";
import { RightsScreen } from "./screens/rights.js";
import { CorrespondenceScreen } from "./screens/correspondence.js";
import { ChatScreen } from "./screens/chat.js";
import { ModelsScreen } from "./screens/models.js";
import { GuardScreen } from "./screens/guard.js";
import { JournalScreen } from "./screens/journal.js";
import { SettingsScreen } from "./screens/settings.js";
import { SelfScreen } from "./screens/self.js";
import { PolygonScreen } from "./screens/polygon.js";
import { MemoryScreen } from "./screens/memory.js";
import { HeartScreen } from "./screens/heart.js";
import { DreamsScreen } from "./screens/dreams.js";
import { BodyScreen } from "./screens/body.js";
import { ChannelsScreen } from "./screens/channels.js";
import { TasksScreen } from "./screens/tasks.js";
import { McpScreen } from "./screens/mcp.js";
import { SkillsScreen } from "./screens/skills.js";
import { BackupsScreen } from "./screens/backups.js";
import { VersionsScreen } from "./screens/versions.js";
import { WorkshopScreen } from "./screens/workshop.js";
import { ModulesScreen } from "./screens/modules.js";
import { RelocateModal } from "./screens/relocate.js";
import { Finder } from "./lib/find.js";

import { DoctorScreen } from "./screens/doctor.js";

const NAV = [
  ["#/", "chat", "Разговор"],
  ["#/agents", "users", "Стая"],
  ["#/tasks", "users", "Задачи"],
  ["#/self", "robot", "Личность"],
  ["#/memory", "memory", "Память"],
  ["#/heart", "heart", "Сердце"],
  ["#/dreams", "moon", "Сны"],
  ["#/body", "activity", "Тело"],
  ["#/channels", "send", "Связь"],
  ["#/models", "cpu", "Модели"],
  ["#/mcp", "settings", "MCP"],
  ["#/skills", "list", "Навыки"],
  ["#/backups", "list", "Бекапы"],
  ["#/versions", "list", "Версии и откат"],
  ["#/workshop", "flask", "Мастерская"],
  ["#/modules", "settings", "Модули"],
  ["#/guard", "shield", "Права"],
  ["#/journal", "list", "Бортжурнал"],
  ["#/doctor", "activity", "Самопроверка"],
  ["#/polygon", "flask", "Полигон"],
  ["#/settings", "settings", "Настройки"],
];
const TITLES = { doctor: "Самопроверка", backups: "Бекапы", "": "Разговор", self: "Личность", memory: "Память", heart: "Сердце", dreams: "Сны", body: "Тело", channels: "Связь", tasks: "Задачи", models: "Модели", mcp: "Внешние MCP-серверы", skills: "Навыки", modules: "Модули", versions: "Версии и откат", workshop: "Мастерская", guard: "Права и Предохранитель", journal: "Бортжурнал", polygon: "Полигон", settings: "Настройки" };

function Sidebar() {
  const s = useApp();
  const st = s.st;
  const cur = s.route.parts[0] || "";
  const pend = s.pending.length;
  return html`<aside class=${`sidebar${s.sidebar ? " open" : ""}`}>
    <div class="brand"><${Visor} avatar=${st?.avatar} mode=${st?.halted?"halted":st?.safe?"safe":pend?"approval":st?.sleeping?"sleep":s.face==="error"?"error":s.face==="listening"?"listening":st?.running?s.face:(s.expression||"idle")} />
      <div><span class="brand-name">БЕНДЕР</span><span class="brand-sub">${st?.identity?.name ? `${st.identity.emoji || ""} ${st.identity.name}` : "агент без имени"}</span></div></div>
    <nav class="nav">${NAV.map(([href, icon, label]) => html`<a href=${href} class=${(href.slice(2) || "") === cur ? "active" : ""}><${Icon} name=${icon} /> ${label}
      ${href === "#/guard" && pend ? html`<span class="count">${pend}</span>` : null}</a>`)}</nav>
    ${st?.update_available ? html`<a class="note" href="#/versions">Доступно обновление · Обновить</a>`:null}
    <div class="side-foot">
      <div class="row"><${ControlButton} small/><${ControlButton} action="shutdown" small/></div>
      <div>Сегодня потрачено: ${money(st?.cost_today || 0)}</div>
      ${st?.mood && st.mood.code !== "ok" ? html`<div><a href="#/body">Самочувствие: ${st.mood.word}</a></div>` : null}
      <div>${st?.guard?.sandbox?.kind === "none" ? "Песочницы нет — опасное спрашивает" : "Песочница: " + (st?.guard?.sandbox?.kind || "")}</div>
      <div class="faint">v${st?.version || ""}${s.connected ? "" : " · нет связи"}</div>
    </div>
  </aside>`;
}

function Banners() {
  const { st } = useApp();
  if (!st) return null;
  const out = [];
  if(st.lifecycle?.restart_fields?.length)out.push(html`<div class="banner amber"><span class="grow">Нужен перезапуск после изменения: ${st.lifecycle.restart_fields.join(', ')}.</span><${ControlButton} small/></div>`);
  if(st.import_rights_offer)out.push(html`<div class="banner amber"><span class="grow">Агент ${st.identity?.name||st.agent_id} получил шаблон Помощник при переезде — сделать Полным?</span><${Button} small onClick=${async()=>{if(confirm('Дать агенту Полный шаблон, включая команды, сеть и SSH?')){await post('/api/import/rights-reviewed',{full:true});refresh();}}}>Сделать Полным</${Button}><${Button} small onClick=${async()=>{await post('/api/import/rights-reviewed',{});refresh();}}>Оставить Помощником</${Button}></div>`);
  if (st.model_notice) out.push(html`<div class="banner amber"><span class="grow">${st.model_notice}</span><${Button} small onClick=${async()=>{await post('/api/models/notice/ack');refresh();}}>Понятно</${Button}></div>`);
  if (st.halted) out.push(html`<div class="banner red"><${Icon} name="power" /><span class="grow">Стоп-кран: агент остановлен.</span><${Button} small kind="green" onClick=${async () => { await post("/api/resume"); refresh(); }}>Снять стоп</${Button}></div>`);
  if (st.guard && (!st.guard.enabled || st.guard.profile === "off")) out.push(html`<div class="banner red"><${Icon} name="shield" /><span class="grow">Предохранитель выключен — агент ничего не спрашивает.</span><a href="#/guard">Права</a></div>`);
  else if (st.guard && !st.guard.hardline) out.push(html`<div class="banner red"><${Icon} name="alert" /><span class="grow">Жёсткий список выключен.</span><a href="#/guard">Права</a></div>`);
  if (st.safe) {
    const why = st.boot?.safe?.reason;
    const normal = async () => { try { await post("/api/restart", { mode: "normal" }); toast("Перезапускаю в обычном режиме…"); setTimeout(() => location.reload(), 2500); } catch (e) { toast(e.message, "error"); } };
    out.push(html`<div class="banner amber"><${Icon} name="alert" /><span class="grow">Безопасный режим: без модулей, каналов и сердца${why ? ` — ${why}` : ""}.${st.boot?.crash_line ? html`<div class="faint mono">${st.boot.crash_line}</div>` : null}</span>
      ${st.boot?.watched ? html`<${Button} small onClick=${normal}>Обычный режим</${Button}>` : null}</div>`);
  }
  return out;
}

const FIRST_STEPS={
 doctor:['Проверь состояние и просмотри состав отчёта перед скачиванием.','#/doctor','Проверить состояние'],
 '':['Напиши, что сделать. Если модели ещё нет, начни с подключения.','#/models','Подключить модель'],
 tasks:['Здесь поручения помощникам и их результаты. Сначала выбери роли, затем дай Бендеру задачу.','#/tasks','Роли команды'],
 self:['Личность, правила и лицо агента. Начни с имени или перенеси прежнего агента.','#/self','Открыть личность'],
 memory:['Здесь память и её источники. Найди запись и открой инспектор для правки с объяснением.','#/memory','Поиск памяти'],
 heart:['Сердце будит агента по времени. Добавь первое напоминание или задачу.','#/heart','Настроить Сердце'],
 dreams:['Во сне агент разбирает опыт и приводит память в порядок. Начни с обычной работы.','#/','Открыть разговор'],
 body:['Самочувствие, машина и дневной бюджет. Укажи лимит расходов, если нужна экономия.','#/settings','Настроить бюджет'],
 channels:['Свяжи свой Telegram, почту или другой канал. Привязка хозяина даёт право управлять агентом.','#/channels','Подключить канал'],
 models:['Выбери провайдера, проверь соединение и назначь Главную роль.','#/models','Подключить провайдера'],
 mcp:['Внешний сервер добавляет инструменты. Сначала просмотри программу и определения, затем одобри.','#/mcp','Добавить сервер'],
 skills:['Навык — памятка для работы. После полезного решения напиши /научись или импортируй свой пакет.','#/skills','Открыть навыки'],
 backups:['Сохраняй снимки на другом носителе. Выбери папку, затем сделай первый бекап.','#/backups','Выбрать папку'],
 versions:['Обновления хозяина проверяются до переключения; при сбое Сторож возвращает прежний слот.','#/versions','Просмотреть обновление'],
 workshop:['Агент предлагает отдельные модули и учится на опыте. Начни с проверки среды и экзаменов.','#/workshop','Открыть проверки'],
 modules:['Модуль получает только одобренные способности. Сначала просмотри пакет в карантине.','#/modules','Импортировать модуль'],
 guard:['Права и стоп-кран. Проверь действие вхолостую, прежде чем менять правила.','#/guard','Проверить действие'],
 journal:['История действий с цепочкой хешей. Выбери фильтр или проверь целостность.','#/journal','Открыть журнал'],
 polygon:['Проверь часть Бендера отдельно: модель, тул, память или экзамены.','#/polygon','Открыть Полигон'],
 settings:['Здесь выключатели и параметры. Меняй нужную группу — изменения сохраняются сразу.','#/settings','Открыть настройки']
};
function FirstSteps(){const s=useApp(),key=s.route.parts[0]||'',info=FIRST_STEPS[key];if(!info||s.st?.appearance?.first_steps===false)return null;return html`<details class="first-steps"><summary>Что здесь и с чего начать</summary><p>${info[0]}</p><a class="btn small" href=${info[1]} onClick=${e=>{if(info[1]===location.hash){e.preventDefault();const node=[...document.querySelectorAll("main .page input,main .page textarea,main .page select,main .page summary,main .page button")].find(n=>!n.disabled&&n.getClientRects().length);node?.focus();node?.scrollIntoView({block:"center"});}}}>${info[1]===location.hash?"К первому действию":info[2]}</a></details>`;}

function Routes() {
  const { route } = useApp();
  const a = route.parts[0] || "";
  if (a === "") return html`<${ChatScreen} />`;
  if (a === "agents") return html`<${AgentsScreen} />`;
  if (a === "rights") return html`<${RightsScreen} />`;
  if (a === "correspondence") return html`<${CorrespondenceScreen} />`;
  if (a === "self") return html`<${SelfScreen} />`;
  if (a === "memory") return html`<${MemoryScreen} />`;
  if (a === "heart") return html`<${HeartScreen} />`;
  if (a === "dreams") return html`<${DreamsScreen} />`;
  if (a === "body") return html`<${BodyScreen} />`;
  if (a === "channels") return html`<${ChannelsScreen} />`;
  if (a === "tasks") return html`<${TasksScreen} />`;
  if (a === "models") return html`<${ModelsScreen} />`;
  if (a === "mcp") return html`<${McpScreen} />`;
  if (a === "skills") return html`<${SkillsScreen} />`;
  if (a === "backups") return html`<${BackupsScreen} />`;
  if (a === "versions") return html`<${VersionsScreen} />`;
  if (a === "workshop") return html`<${WorkshopScreen} />`;
  if (a === "modules") return html`<${ModulesScreen} />`;
  if (a === "guard") return html`<${GuardScreen} />`;
  if (a === "journal") return html`<${JournalScreen} />`;
  if (a === "doctor") return html`<${DoctorScreen} />`;
  if (a === "polygon") return html`<${PolygonScreen} />`;
  if (a === "settings") return html`<${SettingsScreen} />`;
  return html`<div class="page"><div class="page-inner"><h2>Нет такой страницы</h2><a href="#/">В разговор</a></div></div>`;
}

function Shell() {
  const s = useApp();
  const a = s.route.parts[0] || "";
  const halt = async () => { try { await post("/api/halt"); refresh(); toast("Стоп-кран: всё остановлено", "error"); } catch (e) { toast(e.message, "error"); } };
  return html`<div class="shell">
    <${Sidebar} />
    ${s.sidebar ? html`<div class="scrim" onClick=${() => setState({ sidebar: false })}></div>` : null}
    <main class="main">
      <div class="topbar">
        <span class="only-mobile"><${IconButton} icon="menu" title="Меню" onClick=${() => setState({ sidebar: true })} /></span>
        <${Visor} mode=${s.st?.running?'busy':''}/><select class="input agent-select" style="max-width:155px;width:auto" aria-label="Выбрать агента" value=${s.st?.agent_id||'main'} onChange=${e=>switchAgent(e.target.value)}>${(s.st?.agents||[]).filter(x=>!x.archived).map(x=>html`<option value=${x.id}>${x.name}</option>`)}</select>
        <h1>${a==='agents'?'Стая':TITLES[a] || "Бендер"}${s.st?.running ? html`<span class="sub">агент работает…</span>` : null}</h1>
        <${IconButton} icon="search" title="Найти в пульте (Ctrl+K)" onClick=${() => setState({ finder: true })} />
        ${s.pending.length && a !== "" && a !== "guard" ? html`<a class="badge amber" href="#/guard">ждут ответа: ${s.pending.length}</a>` : null}
        ${!s.st?.halted ? html`<${IconButton} icon="power" title="Стоп-кран — остановить всё" danger onClick=${halt} />` : null}
      </div>
      <${Banners} /><${FirstSteps}/>
      ${s.stError && !s.st ? html`<div class="page"><div class="page-inner"><${ErrorBox} error=${s.stError} /></div></div>` : s.st ? html`<${Routes} />` : html`<div class="page"><div class="page-inner"><${Visor} mode="busy" /></div></div>`}
    </main>
    <${RelocateModal} />
    ${s.finder ? html`<${Finder} onClose=${() => setState({ finder: false })} />` : null}
  </div>`;
}

window.addEventListener("keydown", (e) => {
  const typing = e.target.closest?.("input, textarea, select, [contenteditable='true']");
  if (((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "k") || (e.key === "/" && !typing && !e.ctrlKey && !e.metaKey && !e.altKey)) {
    e.preventDefault();
    setState({ finder: true });
  }
});

function onServerEvent(ev) {
  if (ev.type === "agents") refreshSoon();
  if (ev.type === "expression") { setState({ expression: ev.mood }); clearTimeout(window.__expressionTimer); window.__expressionTimer = setTimeout(() => setState({ expression: "" }), (ev.seconds || 8) * 1000); }
  if (ev.type === "turn_start") setState({face:"thinking", expression: ""});
  if (ev.type === "tool_start") setState({face:"tool"});
  if (ev.type === "tool_end") setState({face:ev.ok?"thinking":"error"});
  if (ev.type === "turn_end") setState({face:ev.result?.error?"error":"idle"});
  if (ev.type === "sleep") {setState({st:{...state.st,sleeping:ev.state==="asleep"}});}

  if (ev.type === "approval") {
    setState({ pending: [...state.pending.filter((p) => p.id !== ev.card.id), ev.card] });
    if (document.hidden && "Notification" in window && Notification.permission === "granted") new Notification("Бендер спрашивает", { body: ev.card.summary || ev.card.what });
  } else if (ev.type === "question") {
    setState({ pending: [...state.pending.filter((p) => p.id !== ev.card.id), { kind: "question", ...ev.card }] });
  } else if (ev.type === "approval_done" || ev.type === "question_done") {
    setState({ pending: state.pending.filter((p) => p.id !== ev.id) });
  } else if (ev.type === "notify") {
    if (ev.update) refresh().catch(() => {});
    toast(`${ev.kind === "remind" ? "⏰ " : ev.kind === "alarm" ? "🚨 " : ""}${ev.text}`.slice(0, 200), ev.kind === "alarm" ? "error" : "ok");
    if (document.hidden && "Notification" in window && Notification.permission === "granted") new Notification(ev.title || "Бендер", { body: ev.text });
  } else if (ev.type === "chatgpt_login" && ev.state === "done") {
    toast(ev.main_model ? `Вход в ChatGPT выполнен${ev.email ? ` (${ev.email})` : ""}. Главная модель — ${ev.main_model}; сменить можно в «Моделях».`
      : `Вход в ChatGPT выполнен${ev.email ? ` (${ev.email})` : ""}. Выбери модель в роли — «Модели».`, "ok");
    refresh().catch(() => {});
  } else if (["halt", "settings", "identity", "turn_start", "turn_end", "imported"].includes(ev.type)) {
    if (ev.type === "halt") refresh().catch(() => {}); else refreshSoon();
  }
  emit(ev);
}

function App() {
  const s=useApp();
  useEffect(()=>{
    const cfg=s.st?.appearance||{};
    document.documentElement.dataset.theme=cfg.theme||'system';
    document.documentElement.dataset.large=String(!!cfg.large_font);
    document.documentElement.dataset.contrast=String(!!cfg.high_contrast);
    document.documentElement.dataset.face=String(cfg.enabled!==false);
  },[s.st?.appearance]);
  useEffect(() => {
    const visibility=()=>{document.documentElement.dataset.hidden=String(document.hidden);};
    document.addEventListener('visibilitychange',visibility);visibility();
    refresh().catch(() => {});
    const stop = listen("/api/events", onServerEvent, { onOpen: () => { setState({ connected: true }); refresh().catch(() => {}); }, onError: () => setState({ connected: false }) });
    if ("Notification" in window && Notification.permission === "default") setTimeout(() => Notification.requestPermission().catch(() => {}), 4000);
    return ()=>{stop();document.removeEventListener("visibilitychange",visibility);};
  }, []);
  return html`<${Shell} />`;
}

// Клавиатура телефона: оболочка занимает видимую часть экрана (iOS не сжимает раскладку и сдвигает страницу, Android — по-разному).
(function followVisualViewport() {
  const vv = window.visualViewport;
  if (!vv) return;
  const root = document.documentElement;
  let frame = 0;
  const apply = () => {
    frame = 0;
    root.style.setProperty("--app-h", Math.round(vv.height) + "px");
    root.style.setProperty("--app-top", Math.round(vv.offsetTop) + "px");
    if (vv.offsetTop > 0 && document.activeElement && /^(TEXTAREA|INPUT)$/.test(document.activeElement.tagName)) {
      document.activeElement.scrollIntoView({ block: "nearest" });
    }
  };
  const schedule = () => { if (!frame) frame = requestAnimationFrame(apply); };
  vv.addEventListener("resize", schedule);
  vv.addEventListener("scroll", schedule);
  window.addEventListener("orientationchange", schedule);
  apply();
})();

render(html`<${App} />`, document.getElementById("root"));
