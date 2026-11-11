// Полигон: потрогать часть агента отдельно — модель, тул, Предохранитель, самочувствие, системный промпт.
import { html, useEffect, useState } from "../lib/ui.js";
import { get, post } from "../lib/api.js";
import { Button, ErrorBox, Field, Icon, money } from "../lib/ui.js";

function Box({ icon, title, children }) {
  return html`<div class="card"><div class="card-title"><${Icon} name=${icon} /><h3>${title}</h3></div>${children}</div>`;
}

export function PolygonScreen() {
  const [mText, setMText] = useState("Кто ты? Одной фразой.");
  const [mRes, setMRes] = useState(null);
  const [tools, setTools] = useState([]);
  const [tool, setTool] = useState("list_dir");
  const [tArgs, setTArgs] = useState("{}");
  const [tRes, setTRes] = useState(null);
  const [status, setStatus] = useState(null);
  const [prompt, setPrompt] = useState(null);
  useEffect(() => { get("/api/tools").then((r) => setTools(r.tools)); }, []);
  const run = async (fn, set) => { set({ busy: true }); try { set(await fn()); } catch (e) { set({ error: e }); } };
  return html`<div class="page"><div class="page-inner">
    <div class="card"><h3>Усилия моделей</h3><p><a href="#/models">Умолчание агента и отдельный выбор в каждой роли</a>: пульт показывает выбранный и передаваемый уровень. <code>bender try effort</code> — без сети и вызова модели; <code>/effort high</code> и <code>/effort default</code> — в разговоре или Telegram. Для посредника можно явно выбрать формат API.</p></div>
    <div class="card"><h3>Зрение и слух моделей</h3><p><a href="#/models">Модели → карточка роли → Возможности</a>: да/нет/неизвестно, одна ручная проба PNG или коротким WAV от Piper. Без Piper проверка слуха недоступна с причиной. Терминал: <code>bender try media</code> — только сохранённые признаки; <code>bender try media ПРОВАЙДЕР МОДЕЛЬ vision</code> — запрос по явному выбору хозяина.</p></div>
    <div class="note">Каждая часть агента проверяется отдельно, как только появилась. То же есть в терминале: <code>python -m bender try</code>.</div>
    <div class="card"><h3>Стая</h3><p><a href="#/agents">Создать агента, архив и ZIP</a>; переключатель в шапке, чаты — в разговоре. Терминал: <code>bender try agents</code>, <code>bender chat --agent main</code>.</p></div>
    <div class="card"><h3>Вложения</h3><p>«Прикрепить» в разговоре; send_file ниже отправляет файл хозяину. read_file ниже открывает картинку или аудио с диска, включая inbox; медиа недоверенные. Число/размер за ход и срок картинок в контексте — Настройки → Вложения. Терминал: <code>bender try attachments</code> показывает свои файлы без чтения содержимого; <code>bender try tool read_file '{"path":"inbox/фото.png"}'</code> читает файл с проверкой доступа.</p></div><div class="card"><h3>Живой Telegram</h3><p><a href="#/channels">Связи → Telegram</a>; live full/text/off — в Настройках. Напиши боту: черновик показывает ход, Стоп останавливает. Терминал <code>bender try telegram</code> показывает настройки без отправки; <code>try telegram rich</code> — пример таблицы и экранирования, <code>try telegram reactions</code> — реакции, <code>try telegram commands</code> — меню, <code>try telegram profile</code> — публичное лицо, <code>try telegram chats</code> — чаты, <code>try telegram topics</code> — темы без создания.</p></div>
    <div class="card"><h3>Топливо</h3><p><a href="#/models">Квоты и здоровье моделей</a>. Терминал: <code>bender try fuel</code>; refresh — отдельное чтение квот OmniRoute без модели.</p></div>
    <div class="card"><h3>Самопроверка и отчёт</h3><p><a href="#/doctor">Состояние и состав архива</a>. Терминал: <code>doctor</code> / <code>report</code>; <code>try doctor</code> и <code>try report</code> только читают.</p></div>
    <div class="card"><h3>Обкатка</h3><p>В терминале <code>try soak 60</code>: час событий на фальшивках, только временные данные. <code>try measure 3600</code>: отдельный час полного простоя.</p></div>
    <div class="card"><h3>Архив разговоров</h3><p>Тулы sessions/session_read ниже: свои разговоры, импорт OpenClaw и страницы по дате/id из recall. Старое — справка, не новое поручение. <code>bender try sessions</code>, <code>bender try sessions read ID</code>; «Сжать сейчас» в чате и Памяти, Telegram <code>/compact</code>. Пересказ не удаляет историю, сон делает другую работу.</p></div>
    <div class="card"><h3>Переписка у получателя</h3><p>Выбери агента вверху → Разговор → раздел «Переписка»: отправитель, тулы и ответ, только чтение. Из главного чата — «Открыть переписку». Выключатель итогов — <a href="#/correspondence">Общение стаи</a>. <code>bender try agents chats main</code> — свои чаты/нити без модели; Telegram <code>/chats</code> → Переписка.</p></div>
    <div class="card"><h3>События разговора</h3><p>Команды и кнопки Telegram видны в истории без нового хода; правка известного сообщения дополняет текущую работу. Режим — Настройки → Telegram → Служебные события. В пульте «Ответить» прикладывает цитату. <code>bender try telegram events</code> — режим и счётчик без чтения переписки.</p></div>
    <div class="card"><h3>Запуск и остановка</h3><p><a href="#/settings">Состояние запуска, перезапуск, выключение и автозапуск</a>. <code>bender try lifecycle</code> показывает PID и текущий режим без остановки. <code>bender service status</code> проверяет автозапуск; действия требуют подтверждения хозяина.</p></div>
    <div class="card"><h3>Ресурсы дочерних процессов</h3><p><code>bender try sandbox</code> показывает фактическую границу ОС и настройки лимитов. Windows Job Object ограничивает дерево по памяти, ЦП и числу процессов и завершает потомков с ядром. Выключатели — Настройки → Предохранитель; на Windows проверка живого дерева остаётся хозяину.</p></div>
    <div class="card"><h3>Сетевой посредник</h3><p><a href="#/workshop">Статус изоляции и домены тулов-кода</a>; выключатель — Настройки → Мастерская. Терминал: <code>try proxy</code>.</p></div>
    <div class="card"><h3>Инспекторы</h3><p><a href="#/memory">Память → Поиск → Инспектор</a>, <a href="#/journal">журнал → фильтры/экспорт</a>. Терминал <code>try inspector ID</code>.</p></div>
    <div class="card"><h3>Голос</h3><p>Микрофон и прослушивание — <a href="#/">Разговор</a>. Терминал <code>try voice</code> или <code>try voice speak Текст</code>. Локальный пакет: <code>bender voice install</code>, подключение без загрузки — <code>bender voice connect all</code> или <a href="#/models">Модели → Локальный голос</a>. Замер <code>bender voice bench</code>; <code>try voice local</code> показывает состояние всех агентов. Ниже тулы <code>voice_connect</code> и <code>speak</code>; Telegram <code>/voice reply</code> — отвечать голосом.</p></div>
    <div class="card"><h3>Лицо</h3><p><a href="#/self">Визор и внешность</a>, тема — в настройках. Терминал <code>try face</code>, тул update_self file=avatar.</p></div>
    <div class="card"><h3>Бекапы</h3><p><a href="#/backups">Сохранить, проверить и восстановить слот</a>. Терминал: <code>try backup</code>, <code>backup now/list/verify/restore</code>.</p></div>
    <div class="card"><h3>Мастерская и экзамены</h3><p><a href="#/workshop">Каталог и история проверок</a>. Ниже — propose_tool и run_exams; терминал: <code>try exams run</code>.</p></div>
    <div class="card"><h3>Версии и откат</h3><p><a href="#/versions">Обновить свою ветку, проверить пакеты и слоты</a>. Терминал: <code>try update check</code> — коммиты без установки, <code>bender update</code> — обновление; <code>try update</code> / <code>try rollback</code> — слоты.</p></div>
    <div class="card"><h3>Роли команды</h3><p><a href="#/tasks">Задачи → Роли</a>: создать или выключить роль. Проверка ниже: spawn_task и wait_tasks; терминал <code>try roles</code>.</p></div>
    <div class="card"><h3>Переезд</h3><p>«Знакомство» или «Я» → Hermes/Claude Code: предпросмотр и выбор. Терминал: <code>try migration hermes|claude [папка]</code>.</p></div>
    <div class="card"><h3>Модули</h3><p>Карантин и страницы — в <a href="#/modules">разделе «Модули»</a>. Одобренные MCP-тулы и module_send доступны ниже; терминал: <code>try modules</code>.</p></div>
    <div class="card"><h3>Поиск в сети</h3><p>Выбери web_search ниже или проверь поставщика в <a href="#/settings">настройках поиска</a>. Терминал: <code>python -m bender try search "запрос"</code>.</p></div>
    <div class="card"><h3>Внешние MCP-тулы</h3><p>Подключить сервер и просмотреть определения можно в <a href="#/mcp">разделе «MCP»</a>. Одобренные тулы появятся в списке ниже; каждый вызов проходит Предохранитель.</p></div>
    <div class="card"><h3>Навыки SKILL.md</h3><p>Импорт и карантин — в <a href="#/skills">разделе «Навыки»</a>. Ниже доступны skill_read (каталог, навык, отдельный файл) и skill_run (скрипт с проверкой Предохранителя).</p></div>
    <${Box} icon="cpu" title="Модель напрямую (Главная роль)">
      <div class="row"><input aria-label="Текст для модели" class="input grow" value=${mText} onInput=${(e) => setMText(e.target.value)} /><${Button} busy=${mRes?.busy} onClick=${() => run(() => post("/api/try/model", { text: mText }), setMRes)}>Вызвать</${Button}></div>
      ${mRes?.error ? html`<${ErrorBox} error=${mRes.error} />` : mRes?.text !== undefined ? html`<div style="margin-top:8px">${mRes.reasoning ? html`<details class="reasoning"><summary>Размышления</summary><div>${mRes.reasoning}</div></details>` : null}
        <div>${mRes.text}</div><div class="faint">${mRes.provider} · ${mRes.model} · ${mRes.usage?.input}→${mRes.usage?.output} · ${money(mRes.cost)} · ${mRes.seconds} с · ${mRes.finish}</div>
        ${mRes.notice ? html`<div class="note amber">${mRes.notice}</div>` : null}</div>` : null}
    </${Box}>
    <${Box} icon="robot" title="Тул (как его вызовет агент — через Предохранитель)">
      <${Field} label="Тул"><select class="select" value=${tool} onChange=${(e) => setTool(e.target.value)}>${tools.map((t) => html`<option value=${t.name}>${t.name} — ${t.title}</option>`)}</select></${Field}>
      <div class="faint mono" style="margin-bottom:6px">${JSON.stringify(tools.find((t) => t.name === tool)?.parameters?.properties || {})}</div>
      <textarea aria-label="Аргументы тула в JSON" class="textarea mono" value=${tArgs} onInput=${(e) => setTArgs(e.target.value)}></textarea>
      <${Button} busy=${tRes?.busy} onClick=${() => run(() => post(`/api/try/tool/${tool}`, JSON.parse(tArgs || "{}")), setTRes)}>Выполнить</${Button}>
      ${tRes?.error ? html`<${ErrorBox} error=${tRes.error} />` : tRes?.content !== undefined ? html`<div style="margin-top:8px"><span class=${`badge ${tRes.ok ? "green" : "red"}`}>${tRes.ok ? "ок" : "ошибка"}</span> ${tRes.summary} ${tRes.tainted ? html`<span class="badge amber">недоверенное</span>` : null}
        <pre class="block">${tRes.content}</pre></div>` : null}
    </${Box}>
    <${Box} icon="heart" title="Самочувствие">
      <${Button} small onClick=${() => run(() => get("/api/status"), setStatus)}>Осмотреться</${Button}>
      ${status?.text ? html`<pre class="block">${status.text}</pre>` : status?.error ? html`<${ErrorBox} error=${status.error} />` : null}
    </${Box}>
    <${Box} icon="eye" title="Системный промпт — что агент знает о себе">
      <${Button} small onClick=${() => run(() => get("/api/prompt"), setPrompt)}>Показать</${Button}>
      ${prompt?.text ? html`<div class="faint" style="margin-top:6px">≈${prompt.tokens} токенов</div><pre class="block" style="max-height:60vh">${prompt.text}</pre>` : null}
    </${Box}>
  </div></div>`;
}
