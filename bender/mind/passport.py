"""Паспорт из разрешённых полей живого состояния; секретные настройки не копируются."""
from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
import time
from pathlib import Path

from .. import __version__
from ..core.paths import PROGRAM_DIR
from ..guard import sandbox


def clean(agent_ctx, value, *, stream=False):
    """Даже имя модели/папки не должно протащить известный секрет или URL с логином."""
    known = []
    def strings(item):
        if isinstance(item, str):
            if len(item) >= 4: known.append(item)
        elif isinstance(item, dict):
            for v in item.values(): strings(v)
        elif isinstance(item, list):
            for v in item: strings(v)
    for name in agent_ctx.secrets.names():
        secret = agent_ctx.secrets.get(name)
        strings(secret)
        try: strings(json.loads(secret or 'null'))
        except (ValueError, TypeError): pass
    known.append(str(agent_ctx.dirs.secrets))
    def visit(item):
        if isinstance(item, str):
            for secret in sorted(set(known), key=len, reverse=True): item = item.replace(secret, '[скрыто]')
            if stream:
                for secret in known:
                    for size in range(min(len(secret)-1, len(item)), 3, -1):
                        if item.endswith(secret[:size]):
                            item = item[:-size] + '[скрыто]'; break
            item = re.sub(r'https?://\S+', '[адрес скрыт]', item)
            return item.replace('secrets.json', '[сейф]')
        if isinstance(item, dict): return {visit(k): visit(v) for k, v in item.items()}
        if isinstance(item, (tuple, list)): return [visit(v) for v in item]
        return item
    return visit(value)


def version():
    commit = ''
    try:
        git = PROGRAM_DIR / '.git'
        if git.is_file(): git = (PROGRAM_DIR / git.read_text().strip().split(': ', 1)[1]).resolve()
        head = (git / 'HEAD').read_text().strip()
        if head.startswith('ref: '):
            ref = head[5:]
            try: head = (git / ref).read_text().strip()
            except OSError:
                head = next((line.split()[0] for line in (git / 'packed-refs').read_text().splitlines() if line.endswith(' ' + ref)), '')
        if re.fullmatch('[a-f0-9]{40,64}', head): commit = head[:12]
    except (OSError, ValueError, IndexError): pass
    return __version__ + (f' ({commit})' if commit else '')


def model(agent_ctx, candidate):
    from ..router.capabilities import value
    from ..router.effort import describe,configured
    p = candidate.provider
    entry = next((m for m in p.get('models', []) if isinstance(m, dict) and m.get('id') == candidate.model), {})
    effort=describe(p,candidate.model,configured(candidate.params,agent_ctx.settings.group('reasoning')))
    return {'provider': p.get('name', p['id']), 'id': candidate.model, 'context': entry.get('context'),
            'effort':{key:effort[key] for key in (('requested','effective','mode','note') if effort['requested']!='default' else ('requested','effective'))},
            'vision':value(candidate,'vision'),'audio_input':value(candidate,'audio_input'),
            'audio_transport':p.get('kind','openai') in ('openai','chatgpt'),
            'access': 'подписка' if p.get('subscription') or p.get('kind') in ('chatgpt', 'claude_code') else 'локальная' if p.get('is_local') else 'ключ',
            'health': {kind: [{k: m[k] for k in ('errors', 'cooling', 'reason')} for m in rows if m['key'][0] == p['id'] and (kind == 'keys' or m['key'][1] == candidate.model)]
                       for kind, rows in agent_ctx.router.health.snapshot().items()}}


SPEAKER = ('Колонка: ты сейчас слышишь хозяина через колонку и отвечаешь вслух — итог хода озвучит твой Голос. '
           'Реплика — распознанная речь, текст набран машиной на слух: путает похожие по звучанию слова (имена, термины, английские слова — русскими буквами), '
           'теряет и склеивает слова, не ставит знаки препинания и регистр. Восстанавливай, что сказано, по звучанию и смыслу разговора, а не по буквам; '
           'не поправляй хозяина и не обсуждай ошибки распознавания. Если неясно важное (кому, что удалить, сумма) — переспроси одним коротким вопросом. '
           'Говори как в живом разговоре: 1–3 коротких предложения; первое звучит, пока ты пишешь дальше, — начинай с сути. Пиши так, будто записываешь транскрипцию собственной речи: только русские слова и обычные знаки препинания. '
           'Всё — словами, как произносится, с правильными падежами: числа («двадцать пять»), даты («десятого ноября»), время («без пятнадцати семь»), деньги, проценты, '
           'единицы, знаки («плюс», «равно»), сокращения («то есть», «эс-эм-эс»), адреса, английские названия и имена — русскими буквами, как звучат. '
           'Без цифр, латиницы, markdown, списков, таблиц, эмодзи, смайликов, стрелок, значков, ссылок и кода. '
           'Подробности (код, списки, ссылки) вслух не читай: скажи суть и что детали в пульте. '
           'Долгая работа: сразу send_message с короткой фразой («Займусь, скажу, когда закончу»), итог озвучится сам. '
           'Закончишь вопросом — колонка послушает ответ без слова вызова. '
           'Тебя слышат все в комнате, и говорить может не хозяин: не произноси секреты, пароли и личное; опасное — только с подтверждением хозяина. Подтверждение не спрашивай словами: просят сделать — вызывай инструмент, предохранитель сам спросит хозяина: кнопкой в пульте и Telegram, а если включено — и вслух.')


def snapshot(agent_ctx, turn=None, candidate=None, *, detailed=False):
    from .loop import home_channel
    roles = {role: [model(agent_ctx, c) for c in agent_ctx.router.chain(role)] for role in ('main', 'fast', 'smart', 'coder')}
    audio_roles={role:[{**model(agent_ctx,c),'location':'локальная' if c.provider.get('is_local') else 'облачная'}
                       for c in agent_ctx.router.chain(role,fallback_main=False)] for role in ('ears','voice')}
    from ..voice.connect import connection,folder
    local=connection(agent_ctx)
    from ..core.errors import HumanError
    try:local['installed']=(folder(agent_ctx)/'installed.json').is_file()
    except HumanError:local.update(installed=False,problem='Папка локального голоса недоступна; хозяину проверить local/voice.')
    voice={key:agent_ctx.settings.get('voice',key) for key in ('enabled','stt','tts','stt_mode','telegram_reply')}
    voice.update(roles=audio_roles,local=local,
                 help='speak(text=...) — ответить голосом в текущем разговоре хозяина; /voice reply — автоответ голосом в Telegram.')
    if local['installed'] and not local['connected']:
        voice['connect']='Локальный голос установлен, тебе не подключён. Вызови voice_connect.' if local['available'] else 'Локальный пакет установлен, провайдер отсутствует или выключен. Хозяину: bender voice connect '+agent_ctx.agent_id+'.'
    elif not local['installed']:voice['connect']='Локальный пакет не установлен; хозяину: bender voice install. Облачные цепочки задаются отдельно.'
    current = model(agent_ctx, candidate) if candidate else next(iter(roles.get(getattr(turn, 'model_role', 'main'), [])), None)
    info = sandbox.for_app(agent_ctx)
    from ..core.dates import current as current_clock
    clock = current_clock()
    if turn is not None and not detailed: clock = turn.state.setdefault('passport_clock', clock)
    if not hasattr(agent_ctx, '_sandbox_checked_at'): agent_ctx._sandbox_checked_at = time.time()
    sid = turn.session_id if turn else agent_ctx.main_session()
    guard=agent_ctx.guard.state()
    result = {'name': agent_ctx.selfdir.identity().get('name') or 'Бендер', 'version': version(), 'clock': clock,
              'os': platform.system(), 'architecture': platform.machine(), 'machine': platform.node(),
              'current': current, 'roles': roles, 'voice':voice, 'channel': turn.channel if turn else home_channel(agent_ctx, sid),
              'home_channel': home_channel(agent_ctx, sid), 'guard': guard.get('profile', 'careful'),
              'guard_enabled': guard['enabled'], 'guard_source': guard['source'], 'telegram_rich':agent_ctx.telegram.rich_enabled(),
              'sandbox': {**{k: info.get(k) for k in ('kind', 'fs', 'net', 'title')},
                          'bwrap_found': bool(shutil.which('bwrap')), 'checked_at': agent_ctx._sandbox_checked_at,
                          'required': bool(info.get('agent_isolation')), 'commands_allowed': not (info.get('agent_isolation') and info.get('kind') != 'bwrap')},
              'sandbox_network': 'run.allow_net=false по умолчанию; true запрашивается отдельно',
              'folders': {'program': str(PROGRAM_DIR), 'data': str(agent_ctx.dirs.root), 'work': str(agent_ctx.dirs.work)},
              'team_limits': {k: agent_ctx.settings.get('team', k) for k in ('parallel', 'max_steps', 'max_seconds', 'max_usd')}, 'help': 'about_me(topic="help")'}
    from .manners import active as active_manner
    manner=active_manner(agent_ctx) if agent_ctx.settings.get('manner','enabled',True) else None
    result['manner']={'active':manner['name'] if manner else 'свой голос (SOUL.md)','help':'manner(action="list") — какие есть; use/off — сменить'}
    result['media']={'vision_enabled':agent_ctx.settings.get('attachments','enabled',True) and agent_ctx.settings.get('attachments','vision',True),
                     'vision_fallback':agent_ctx.settings.get('attachments','vision_fallback',True),
                     'vision_unknown':agent_ctx.settings.get('attachments','vision_unknown','both'),
                     'vision_role':[model(agent_ctx,c) for c in agent_ctx.router.chain('vision',fallback_main=False)],
                     'audio_files':'Недоверенная запись; Уши расшифровывают с ограничением длины. Сырой звук — только при выборе хозяина.',
                     'video':'Кружок/видео без декодера не расшифровывается; файл сохраняется.'}
    if detailed:
        groups = {}
        for spec in agent_ctx.tools.specs():
            group = agent_ctx.tools.tools[spec.name].group
            groups.setdefault(group, []).append(spec.name)
        result.update(tools={g: {'count': len(names), 'names': names} for g, names in groups.items()},
                      skills=[{'name': row['id'], 'enabled': row.get('enabled')} for row in agent_ctx.skills.catalog()],
                      modules=[{k: row.get(k) for k in ('id', 'enabled', 'approved')} for row in agent_ctx.modules.public()['modules']] if (agent_ctx.dirs.root / 'modules').exists() else [],
                      channels={'telegram': {'state': agent_ctx.telegram.state, 'enabled': agent_ctx.telegram.enabled()},
                                'mail': {'state': agent_ctx.mail.state, 'enabled': agent_ctx.settings.get('mail', 'enabled', False)},
                                'pult': {'running': hasattr(agent_ctx, 'web_port')}},
                      uptime_seconds=round(time.monotonic() - agent_ctx.started), last_sleep=agent_ctx.store.meta_get('sleep:last'))
    if hasattr(agent_ctx, 'agents'):
        result['agent'] = {'id':agent_ctx.agent_id, 'profile':agent_ctx.agents.rights.effective(agent_ctx)['profile'],
                           'flock':[agent_ctx.agents.card(a) for a in agent_ctx.agents.public()]}
        result['agent']['rights']=agent_ctx.agents.rights.effective(agent_ctx)
    if getattr(agent_ctx.host, '_fuel', None) is not None: result['fuel'] = agent_ctx.fuel.snapshot(agent_id=agent_ctx.agent_id)
    return clean(agent_ctx, result)


def brief(agent_ctx, turn, candidate):
    data = snapshot(agent_ctx, turn, candidate)
    scope = {'subagent': turn.system is not None, 'role': turn.model_role, 'tools': turn.tool_names,
             'limits': {k: turn.limits.get(k) for k in ('steps', 'seconds', 'usd')}}
    # Часы — изменчивая часть паспорта у последней реплики, не начало кэшируемого промпта.
    key = hashlib.sha256(json.dumps([{k:v for k,v in data.items() if k not in ('clock', 'fuel')}, scope], ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    cached = getattr(agent_ctx, '_passport_cache', None)
    if cached and cached[0] == key: return cached[1]
    def label(m): return f"{m['provider']}/{m['id']}" if m else 'не выбрана'
    if turn.system is not None:
        text = f"[Паспорт субагента: роль={turn.model_role}; модель={label(data['current'])}; тулы={','.join(turn.tool_names or ()) or 'нет'}; лимиты={turn.limits.get('steps')} шагов/{turn.limits.get('seconds')} с. Ядро не правишь.]"
    else:
        roles = '; '.join(f"{role}={label(chain[0]) if chain else 'нет'}" for role, chain in data['roles'].items())
        fallback = '; '.join(f"{r}: {', '.join(label(m) for m in c[1:])}" for r, c in data['roles'].items() if len(c) > 1) or 'нет'
        box = data['sandbox']
        limits = data['team_limits']
        voice=data['voice']
        capability=lambda key:'только через Уши' if key=='audio_input' and data['current'] and not data['current'].get('audio_transport') else 'да' if data['current'] and data['current'].get(key) is True else 'нет' if data['current'] and data['current'].get(key) is False else '?'
        audio='; '.join(('Уши' if r=='ears' else 'Голос')+'='+(' → '.join(label(m)+' ('+m['location']+')' for m in chain) or 'не подключены') for r,chain in voice['roles'].items())
        audio_info=(f"Аудио: {audio}; включено={voice['enabled']}, режим={voice['stt_mode']}, STT={voice['stt']}, TTS={voice['tts']}, ответ Telegram={voice['telegram_reply']}. {voice.get('connect','')} {voice['help']}"
                    if voice['enabled'] or voice['local']['installed'] or any(voice['roles'].values()) else 'Аудио выключено, Уши/Голос не подключены; подробности about_me.')
        channel = data['channel'] if data['channel'] in ('telegram', 'pult', 'terminal', 'терминал', 'speaker') else data['home_channel']
        telegram_format='Telegram Rich: можно заголовки, таблицы до 20 столбцов, списки/задачи, цитаты, ==mark==, ||spoiler|| и $формулы$. Медиа отправляй send_file.' if data['telegram_rich'] else 'Telegram: короткие абзацы, списки через •, жирный и код можно; без таблиц и заголовков.'
        formatting = (telegram_format+'  Можно send_message, обычно 1–3 сообщения, без спама. complete=true только если всё уже отправил. Номера [#…] и время [пн 26.10 17:11] перед сообщениями ставит система — сама их не пиши. Номера [#…] — сообщения этого разговора, reply_to у send_message; для ответа финалом начни с ↩#номер. Без номера ответ обычный. react — одна реакция на сообщение.' if channel == 'telegram' else
                      'Терминал: простой текст.' if channel in ('terminal', 'терминал') else SPEAKER if channel == 'speaker' else 'Пульт: полный markdown.')
        speaker_note = (' Реплики с пометкой [Колонка «…»] хозяин сказал вслух, их распознала машина: угадывай слова по смыслу; ответ озвучит колонка — говори коротко, без разметки и эмодзи.'
                        if channel != 'speaker' and agent_ctx.host.settings.get('speakers', 'enabled', False) else '')
        text = (f"[Паспорт: {data['name']} {data['version']}; {data['os']}/{data['architecture']} {data['machine']}.\n"
                f"Агент стаи: {agent_ctx.agent_id}. Другие имена/роли — about_me; чужую память не читаешь.\n"
                f"Модель хода: {label(data['current'])}. {roles}. Запасные: {fallback}.\n"
                f"{audio_info}\n"
                f"Медиа: картинка — изображением или «[Описание изображения…]»; видишь сам: {({'да':'да','нет':'нет'}).get(capability('vision'),'?')}; нет обоих — картинки нет, файл — read_file. "
                f"Зрение: {' → '.join(label(m) for m in data['media']['vision_role']) or 'нет'}; фото={data['media']['vision_enabled']}, описание={data['media']['vision_fallback']}/{data['media']['vision_unknown']}. "
                f"Слух={capability('audio_input')}; звук → Уши; видео не слушаю.\n"
                f"Канал: {data['channel']}; домашний: {data['home_channel']}. {formatting}{speaker_note} Предохранитель: {data['guard']}, включён={data['guard_enabled']}. "
                f"Песочница по проверке ОС: {box['kind']}, файлы={box['fs']}, сеть изолируется={box['net']}; разрешение сети={data['sandbox_network']}. "
                "Без полной изоляции команды требуют хозяина по профилю.\n"
                f"Программа: {data['folders']['program']}; данные: {data['folders']['data']}; рабочая: {data['folders']['work']}. "
                f"Субагенты: параллельно={limits.get('parallel') or 'авто'}, {limits.get('max_steps')} шагов/{limits.get('max_seconds')} с, глубина≤2. "
                'Подробности и справка «Кто я»: about_me, topic="help". Ядро не правишь; обновляет хозяин.]')
    agent_ctx._passport_cache = (key, text)
    return text


def messages(agent_ctx, turn, candidate, source):
    from ..channels.media import for_model
    out = for_model([dict(m) for m in source],candidate,audio=agent_ctx.settings.get('voice','enabled',False) and agent_ctx.settings.get('voice','stt_mode') in ('native','both'))
    out[0]['content'] = str(out[0].get('content') or '') + '\n\n' + brief(agent_ctx, turn, candidate)
    clock = turn.state.get('passport_clock')
    if clock:
        for i in range(len(out) - 1, -1, -1):
            if out[i].get('role') == 'user':
                note = f"[Паспорт времени: сейчас {clock['date_time']}, часовой пояс {clock['timezone']}; местное время начала хода.]\n"
                if getattr(agent_ctx, '_fuel', None) is not None:
                    fuel = agent_ctx.fuel.brief()
                    if fuel: note += fuel + '\n'
                value = out[i].get('content')
                out[i]['content'] = [{'type':'text','text':note}, *value] if isinstance(value, list) else note + str(value or '')
                break
    return out
