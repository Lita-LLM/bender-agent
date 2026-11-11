"""Новость о смене версии. Хозяину — одно уведомление «🔄 Бендер обновился» со списком изменений,
агентам стаи — раздел системного промпта на неделю: служебные события модель не видит, а знать,
что их код сменился, им нужно так же, как хозяину.

Версию сравниваем при каждом запуске с запомненной, поэтому новость приходит после любого способа
обновления: кнопка в пульте, ночная установка, /update, git pull руками, слот подписанного пакета."""
import time

DAYS = 7      # столько дней агенты видят новость в промпте
LIMIT = 15    # строк изменений в сообщении


async def _git(app, *args):
    from .git import command
    root = app.updater.root
    if not (root / '.git').exists(): return None
    code, text = await command(['git', '-c', 'core.hooksPath=/dev/null', '-C', str(root), *args], root, allow_failure=True, timeout=30)
    return text.strip() if code == 0 else None


async def current(app):
    """Что сейчас запущено: коммит git или слот версии."""
    head = await _git(app, 'rev-parse', 'HEAD')
    if head: return 'git:' + head
    from ..core.watchdog import load_boot
    slot = (load_boot(app.dirs.root).get('slots') or {}).get('current')
    return 'slot:' + slot if slot else None


async def changes(app, old, new):
    """Заголовок и строки изменений между версиями."""
    if old.startswith('git:') and new.startswith('git:'):
        a, b = old[4:], new[4:]
        log = await _git(app, 'log', '--format=%s', a + '..' + b)
        if log is not None and log:
            return 'Бендер обновился', log.splitlines()
        back = await _git(app, 'log', '--format=%s', b + '..' + a)
        if back:
            return 'Бендер вернулся на прежнюю версию', ['отменено: ' + s for s in back.splitlines()]
        return 'Версия Бендера сменилась (' + a[:7] + ' → ' + b[:7] + ')', []
    if new.startswith('slot:'):
        record = next((r for r in reversed(app.store.meta_get('updates:history', [])) if r.get('id') == new[5:]), {})
        return 'Бендер обновился до версии ' + str(record.get('version') or new[5:]), []
    return 'Версия Бендера сменилась', []


def history_place() -> str:
    """Где подробная история изменений: в выпуске без дневника разработки — история коммитов."""
    from pathlib import Path
    if (Path(__file__).resolve().parents[2] / 'ЛЕТОПИСЬ.md').exists():
        return 'в ЛЕТОПИСЬ.md в папке программы'
    return 'в истории коммитов программы (git log)'


def text(title, lines):
    shown = lines[:LIMIT]
    out = '🔄 ' + title + '.'
    if shown:
        out += '\nЧто изменилось:\n' + '\n'.join('• ' + s for s in shown)
        if len(lines) > LIMIT: out += f'\n… и ещё {len(lines) - LIMIT}.'
    else:
        out += '\nСписка изменений нет — подробности ' + history_place() + '.'
    return out


async def announce(app):
    """При запуске: версия сменилась — сказать хозяину и запомнить новость для агентов.
    Первый запуск только запоминает версию, чтобы не слать новость о давно стоящей программе."""
    if app.safe: return
    try:
        new = await current(app)
        if not new: return
        old = app.store.meta_get('updates:seen')
        if old == new: return
        app.store.meta_set('updates:seen', new)
        if not old: return
        title, lines = await changes(app, old, new)
        message = text(title, lines)
        app.store.meta_set('updates:news', {'at': time.time(), 'from': old, 'to': new, 'text': message})
        app.journal.write('update', title, actor='system', data={'from': old, 'to': new, 'changes': len(lines)})
        if not wake(app, message):
            app.bus.publish({'type': 'notify', 'kind': 'update', 'text': message})
    except Exception as exc:  # noqa: BLE001 — новость не должна мешать запуску
        app.journal.write('update', 'Не удалось собрать новость об обновлении', actor='system', data={'error': str(exc)[:300]})


PROMPT = ('[Событие: хозяин установил обновление Бендера — программы, в которой ты живёшь; ты уже работаешь на новой версии. '
          'Напиши хозяину коротко, своими словами: обновление установлено и что в нём было — по списку ниже, главное для него, без хэшей. '
          'Больше ничего не делай, если об этом не просили. Список составлен кодом из названий коммитов.]\n')


def active(app):
    """Агент и разговор, выбранные у хозяина в личке Telegram, — с ним хозяин сейчас и говорит.
    Telegram не привязан — главный агент и его главный разговор."""
    try:
        tg = app.telegram
        owner = tg.owner() or {}
        chat = owner.get('chat_id') or owner.get('user_id')
        if chat:
            from ..channels.telegram_topics import Address
            ctx, sid = tg.selected(Address(chat))
            return ctx, sid or ctx.main_session()
    except Exception:  # noqa: BLE001 — выбор недоступен: расскажет главный
        pass
    return app, app.main_session()


def wake(app, message):
    """Разбудить агента(ов) новостью: он сам пишет хозяину. False — некого будить, нужен простой notify."""
    mode = app.settings.get('updates', 'news', 'active')
    if mode == 'notice': return False
    try:
        if mode == 'all':
            targets = [(ctx, ctx.main_session()) for ctx in (app.agents.get(r['id']) for r in app.agents.public() if not r.get('archived'))]
        elif mode == 'main':
            targets = [(app, app.main_session())]
        else:
            targets = [active(app)]
    except Exception:  # noqa: BLE001 — стая недоступна: хватит уведомления
        return False
    news = app.store.meta_get('updates:news')
    if isinstance(news, dict):
        news['told'] = [ctx.agent_id for ctx, _ in targets]
        app.store.meta_set('updates:news', news)
    import asyncio
    for ctx, sid in targets:
        task = asyncio.ensure_future(_tell(app, ctx, sid, message))
        tells = app.__dict__.setdefault('_update_tells', set())  # держим ссылку, чтобы задачу не собрал сборщик мусора
        tells.add(task); task.add_done_callback(tells.discard)
    return bool(targets)


async def _tell(app, ctx, sid, message):
    import asyncio
    try:
        for _ in range(3600):  # в чужой ход не вклиниваемся; дольше часа не ждём
            turn = ctx.mind.turns.get(sid)
            if not (turn and turn.task and not turn.task.done()): break
            await asyncio.wait({turn.task}, timeout=1)
        result = await ctx.mind.say(sid, PROMPT + message.removeprefix('🔄 '), channel='update', actor='schedule', unattended=True)
        error = (result or {}).get('error')
        if error and error.get('code') != 'stopped': raise RuntimeError(str(error.get('title') or error.get('code')))
    except asyncio.CancelledError: raise
    except Exception as exc:  # noqa: BLE001 — агент не смог рассказать: хозяин всё равно узнаёт
        app.journal.write('update', 'Агент не рассказал о новой версии: ' + str(getattr(exc, 'title', '') or exc)[:200], actor='system', data={'agent': ctx.agent_id})
        app.bus.publish({'type': 'notify', 'kind': 'update', 'agent_id': ctx.agent_id, 'text': message})


def awareness(agent_ctx):
    """Раздел системного промпта: какой новый код у агента. Меняется раз в обновление — кэш не страдает."""
    host = getattr(agent_ctx, 'host', agent_ctx)
    news = host.store.meta_get('updates:news')
    if not isinstance(news, dict) or time.time() - float(news.get('at') or 0) > DAYS * 86400: return ''
    when = time.strftime('%d.%m.%Y %H:%M', time.localtime(news['at']))
    body = str(news.get('text') or '').removeprefix('🔄 ')
    told = news.get('told') or []
    me = getattr(agent_ctx, 'agent_id', 'main')
    who = ('Ты сам рассказал хозяину об этом.' if me in told else
           'Хозяину об этом уже сообщили; если он спросит тебя — ты в курсе.' if told else
           'Хозяину об этом пришло уведомление.')
    return ('# Обновление программы\n\n'
            f'{when} хозяин установил новую версию Бендера — программы, в которой ты живёшь. ' + who + '\n\n'
            + body + '\n\nЕсли хозяин спросит, что нового, — расскажи; полная история изменений — ' + history_place() + '.')
