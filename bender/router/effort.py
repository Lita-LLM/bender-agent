"""Усилия модели: выбор хозяина и отдельный перевод для каждого API, без сети."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from ..core.errors import HumanError

LEVELS = {'default':'Как у провайдера', 'off':'Без размышлений', 'minimal':'Минимальные',
          'low':'Низкие', 'medium':'Средние', 'high':'Высокие', 'xhigh':'Очень высокие',
          'max':'Максимальные', 'adaptive':'Модель решает сама'}
MODES = {'auto':'Определить по провайдеру и модели', 'openai':'reasoning_effort (OpenAI-совместимый)',
         'openrouter':'reasoning (OpenRouter)', 'anthropic':'Adaptive + effort (Claude)',
         'budget':'Бюджет размышлений (старый Claude)', 'deepseek':'Thinking + effort (DeepSeek V4)',
         'thinking':'Thinking on/off (DeepSeek V3)', 'qwen':'Шаблон чата: enable_thinking',
         'none':'Не передавать усилия'}
_ORDER = ['off','minimal','low','medium','high','xhigh','max']
_BUDGETS = {'minimal':1024,'low':2048,'medium':8000,'high':24000}


def normalize(level):
    aliases = {'none':'off','auto':'default','reset':'default','x-high':'xhigh','maximum':'max'}
    value = aliases.get(str(level).strip().lower(), str(level).strip().lower())
    if value not in LEVELS:
        raise HumanError('effort','Неизвестный уровень усилий','Используй /effort '+', '.join(LEVELS)+'.')
    return value


def validate(params):
    if 'effort' in params and (not isinstance(params['effort'],str) or params['effort'] not in LEVELS):
        raise HumanError('effort','Неизвестный уровень усилий модели','Выбери уровень в «Модели» → роль.')
    if 'effort_mode' in params and (not isinstance(params['effort_mode'],str) or params['effort_mode'] not in MODES):
        raise HumanError('effort_format','Неизвестный формат усилий','Выбери формат в «Модели» → роль → Усилия модели.')


def _row(provider, model):
    return next((m for m in provider.get('models',[]) if isinstance(m,dict) and m.get('id')==model), {})


# Старые модели, которым сам Claude Code усилий не передаёт (так в его коде, версия 2.1.319).
_CLI_NO_EFFORT = re.compile(r'claude-(?:3-|opus-4-[01](?:-|$)|sonnet-4-[05](?:-|$)|haiku-4-5(?:-|$)|(?:opus|sonnet)-4(?:-20\d{6})?$)')


def _cli_name(model):
    """Имя модели Claude Code — к виду Anthropic: без приставок Bedrock/Vertex, версий и окна [1m]."""
    name = (model or '').strip().lower() or 'default'
    name = re.sub(r'\[1m\]$','',name)  # opus[1m] — та же модель с окном 1M
    name = re.sub(r'^(?:[a-z]+\.)?anthropic\.','',name.rsplit('/',1)[-1])  # us.anthropic.…, anthropic.…
    name = re.sub(r'(?:-v\d+(?::\d+)?|@\d{8})$','',name)  # …-v1:0, …@20250929
    # Алиасы Claude Code у Anthropic: default — Opus 5.5 на всех тарифах; best — Fable, если доступна, иначе Opus;
    # opusplan в -p (без режима плана) работает на Sonnet.
    return {'default':'claude-opus-5-5','best':'claude-fable-5-1','opusplan':'claude-sonnet-5-5'}.get(name,name)


def _cli_elsewhere():
    """Bedrock, Vertex, Foundry, свой адрес или переназначенные алиасы: таблица Anthropic может не совпасть."""
    import os
    names=('CLAUDE_CODE_USE_BEDROCK','CLAUDE_CODE_USE_VERTEX','CLAUDE_CODE_USE_FOUNDRY','ANTHROPIC_BASE_URL','ANTHROPIC_MODEL','ANTHROPIC_DEFAULT_MODEL')
    return any(os.environ.get(n) for n in names) or any(k.startswith('ANTHROPIC_DEFAULT_') and v for k,v in os.environ.items())


def _claude(model, row, cli=False):
    """Зафиксированные семейства; опубликованные возможности имеют приоритет над именем."""
    if cli:model = _cli_name(model)
    name = model.rsplit('/',1)[-1].lower().replace('.', '-').replace('_','-')
    name = {'opus':'claude-opus-5-5','sonnet':'claude-sonnet-5-5',
            'fable':'claude-fable-5-1','haiku':'claude-haiku-5-5'}.get(name,name)
    modern = bool(re.match(r'claude-(?:(?:opus|sonnet|haiku)-5(?:-|$)|(?:fable|mythos)-(?:5|preview)(?:-|$)|opus-4-[678](?:-|$)|sonnet-4-6(?:-|$))',name))
    xhigh = modern and not re.match(r'claude-(?:opus-4-6|sonnet-4-6|mythos-preview)(?:-|$)',name)
    mandatory = bool(re.match(r'claude-(?:opus-5-5|fable-5|mythos-(?:5|preview))(?:-|$)',name))
    between = bool(re.match(r'claude-sonnet-5-5(?:-|$)',name))
    caps = row.get('reasoning_capabilities') or {}
    modern = caps.get('adaptive',modern)
    mandatory = not caps.get('disabled',not mandatory) and not between
    levels = ['low','medium','high']
    if modern:
        if caps.get('xhigh',xhigh):levels.append('xhigh')
        if caps.get('max',True):levels.append('max')
        if not mandatory:levels.insert(0,'off')
        levels.append('adaptive')
        return 'anthropic',levels,between
    if re.match(r'claude-(?:3-7|(?:opus|sonnet|haiku)-4)(?:-|$)',name) or caps.get('budget'):
        return 'budget',['off','minimal','low','medium','high'],False
    return 'none',[],False


def _openai(model, responses):
    name = re.sub(r'-\d{4}-\d{2}-\d{2}$','',model.rsplit('/',1)[-1].lower())
    basic = ['low','medium','high']
    if name in ('gpt-6-astra','gpt-6.1-sol'):return basic+['xhigh']+(['max'] if responses else [])
    if name in ('gpt-6-sol','gpt-6-luna') or re.match(r'gpt-5\.6(?:-|$)',name):
        return ['off']+basic+['xhigh']+(['max'] if responses else [])
    if name=='gpt-5.1-codex-mini':return ['medium']
    if name=='gpt-5.1-codex-max':return ['off','medium','high','xhigh']
    if re.match(r'gpt-5(?:\.\d+)?-codex(?:-|$)',name):return basic+['xhigh']
    if name=='gpt-5-pro':return ['high']
    if re.match(r'gpt-5\.[2-9](?:\.\d+)?-pro(?:-|$)',name):return ['medium','high','xhigh']
    if re.match(r'gpt-5\.[2-9](?:\.\d+)?(?:-|$)',name):return ['off']+basic+['xhigh']
    if re.match(r'gpt-5\.1(?:-|$)',name):return ['off']+basic
    if re.match(r'gpt-5(?:-|$)',name):return ['minimal']+basic
    if re.match(r'o[134](?:-|$)',name):return basic
    return []


def profile(provider, model, mode='auto'):
    automatic=mode=='auto'
    row = _row(provider,model)
    kind = provider.get('kind','openai')
    host = (urlsplit(provider.get('base_url','')).hostname or '').lower()
    preset = provider.get('preset')
    between = False
    unsupported = False
    levels = []
    if mode=='auto':
        if kind in ('anthropic','claude_code'):
            mode,levels,between = _claude(model,row,cli=kind=='claude_code')
            if kind=='claude_code':
                # Флаг не передаётся только тем, кому его не передаёт и сам Claude Code; Opus 4.5 — low…high.
                # Незнакомым и будущим моделям — прежние low/medium/high: лишнее Claude Code понизит сам.
                name = _cli_name(model)
                unsupported = bool(_CLI_NO_EFFORT.match(name))
                mode='cli';between=False
                if re.match(r'claude-opus-4-5(?:-|$)',name):levels=['low','medium','high']
                levels=[] if unsupported else [x for x in levels if x not in ('off','minimal','adaptive')]
        elif kind=='chatgpt':mode='openai';levels=_openai(model,True)
        elif host=='openrouter.ai' or preset=='openrouter':
            mode='openrouter';levels=['off','minimal','low','medium','high','xhigh','adaptive']
        elif host=='api.deepseek.com' or preset=='deepseek':
            mode='deepseek' if re.match(r'deepseek-v4(?:-|$)',model.lower()) else 'thinking'
        elif preset=='ollama':mode='openai';levels=['off','low','medium','high','max']
        elif _openai(model,False):mode='openai';levels=_openai(model,False)
        elif preset=='gemini' or host=='generativelanguage.googleapis.com':
            mode='openai';levels=['minimal','low','medium','high']
            if 'pro' not in model.lower():levels.insert(0,'off')
        elif row.get('reasoning') is True or row.get('reasoning_efforts'):mode='openai'
        else:mode='none'
    if mode=='openai' and not levels:
        levels=['low','medium','high'] if automatic else ['off','minimal','low','medium','high','xhigh','max']
    elif mode=='cli' and not levels and not unsupported:levels=['low','medium','high']
    elif mode=='anthropic' and not levels:levels=['off','low','medium','high','adaptive']
    elif mode=='budget':levels=['off','minimal','low','medium','high']
    elif mode=='deepseek':levels=['off','high','max']
    elif mode in ('thinking','qwen'):levels=['off','high']
    elif mode=='openrouter' and not levels:levels=['off','minimal','low','medium','high','xhigh','adaptive']
    declared = row.get('reasoning_efforts')
    if isinstance(declared,list):levels=[x for x in _ORDER+['adaptive'] if x in declared]
    if automatic and row.get('reasoning') is False and kind not in ('anthropic','claude_code'):
        mode='none';levels=[]
    if mode=='none':levels=[]
    note = {'none':'Формат не подтверждён: параметр не передаётся. Для посредника выбери формат явно.',
            'budget':'Старый API: уровень переводится в бюджет токенов, max_tokens может вырасти.',
            'cli':('Официальный Claude Code: --effort, если установленная версия поддерживает этот флаг.' if not unsupported else 'У этой модели Claude Code усилий нет: флаг не передаётся.')
                  +(' Claude Code настроен на другой источник моделей (переменные окружения) — уровень уточнит он сам.' if kind=='claude_code' and _cli_elsewhere() else ''),
            'thinking':'API умеет только включать и выключать размышления.',
            'qwen':'Шаблон чата умеет только включать и выключать размышления.'}.get(mode,'')
    if not automatic and mode=='openai':note='Явный формат: выбранный уровень передаётся напрямую. Допустимые уровни проверь у своего посредника.'
    if automatic and preset=='ollama':note='Ollama переводит reasoning_effort по метаданным модели; уровни могут совпадать или возвращаться к её умолчанию. Поддержку проверь в /api/show.'
    if between:note+=' off оставляет служебные размышления между тулами (between_tools); полного disabled у этой модели нет.'
    return {'mode':mode,'levels':['default']+levels,'between_tools':between,'note':note}


def describe(provider, model, params=None, default='default'):
    params=params or {}
    requested=params.get('effort',params.get('reasoning_effort',default))
    try:requested=normalize(requested)
    except HumanError:
        return {'mode':'raw','levels':['default'],'between_tools':False,'requested':'прямой параметр API',
                'effective':'прямой параметр API','note':'Прежний нестандартный параметр сохранён без перевода.'}
    info=profile(provider,model,params.get('effort_mode','auto'))
    supported=info['levels'][1:]
    effective=requested
    note=info['note']
    if requested!='default' and requested not in supported:
        if requested in ('off','adaptive'):
            effective='default'
            note+=' Размышления не выключены: остаётся выбор провайдера.' if requested=='off' else ' Остаётся выбор провайдера.'
        else:
            ranked=[x for x in _ORDER[1:] if x in supported]
            lower=[x for x in ranked if _ORDER.index(x)<=_ORDER.index(requested)]
            effective=lower[-1] if lower else ranked[0] if ranked else 'default'
            note+=f' Запрошено {requested}, доступно {effective}.'
    return {**info,'requested':requested,'effective':effective,'note':note.strip()}


def project(provider, model, params):
    """Служебные effort/effort_mode никогда не уходят в API; неизвестное сохраняется."""
    out=dict(params or {})
    selected=out.pop('effort',None)
    mode=out.pop('effort_mode','auto')
    # Старое имя остаётся рабочим. У Claude оно теперь использует нативный effort.
    if selected is None and provider.get('kind')=='anthropic' and out.get('reasoning_effort'):
        selected=out.pop('reasoning_effort')
        if profile(provider,model,mode)['mode']=='none':mode='budget'
    if selected in (None,'default'):return out
    info=describe(provider,model,{'effort':selected,'effort_mode':mode})
    mode,level=info['mode'],info['effective']
    out.pop('reasoning_effort',None)
    if level=='default':return out
    if mode=='openai':out['reasoning_effort']='none' if level=='off' else level
    elif mode=='openrouter':
        reasoning=dict(out.get('reasoning') or {})
        for key in ('effort','enabled','max_tokens'):reasoning.pop(key,None)
        reasoning.update({'enabled':False} if level=='off' else {'enabled':True} if level=='adaptive' else {'effort':level})
        out['reasoning']=reasoning
    elif mode in ('anthropic','budget'):
        output=dict(out.get('output_config') or {});output.pop('effort',None)
        if level=='off':out['thinking']={'type':'between_tools' if info['between_tools'] else 'disabled'}
        elif mode=='anthropic':
            out['thinking']={'type':'adaptive'}
            if level!='adaptive':output['effort']=level
        else:
            budget=_BUDGETS[level]
            out['thinking']={'type':'enabled','budget_tokens':budget}
            out['max_tokens']=max(int(out.get('max_tokens') or 16000),budget+4096)
        if output:out['output_config']=output
        else:out.pop('output_config',None)
        for key in ('temperature','top_p','top_k'):out.pop(key,None)
    elif mode in ('deepseek','thinking'):
        out['thinking']={'type':'disabled' if level=='off' else 'enabled'}
        if mode=='deepseek' and level!='off':out['reasoning_effort']=level
    elif mode=='qwen':out['chat_template_kwargs']={**(out.get('chat_template_kwargs') or {}),'enable_thinking':level!='off'}
    return out


def configured(params, settings):
    """Кандидат/явный вызов важнее умолчания; выключатель не меняет старые поля API."""
    params=dict(params or {})
    if not settings.get('enabled',True):
        params.pop('effort',None);params.pop('effort_mode',None)
    elif 'effort' not in params and 'reasoning_effort' not in params and settings.get('effort','default')!='default':
        params['effort']=settings['effort']
    return params


def snapshot(ctx):
    settings=ctx.settings.group('reasoning')
    rows=[]
    for role in ctx.models.roles():
        if role in ('ears','voice','embed'):continue
        for candidate in ctx.router.chain(role,fallback_main=False):
            rows.append({'role':role,'provider':candidate.provider['id'],'model':candidate.model,
                         **describe(candidate.provider,candidate.model,configured(candidate.params,settings))})
    return {'enabled':settings['enabled'],'effort':settings['effort'],'models':rows}


async def command(ctx, sid, arg=''):
    """Команда хозяина меняет умолчание только этого агента; кандидаты имеют приоритет."""
    if arg:
        from ..tools.registry import ToolContext
        level=normalize(arg)
        await ToolContext(ctx,actor='owner',session_id=sid).check('settings','reasoning','изменить усилия моделей этого агента')
        ctx.settings.set('reasoning',{'enabled':True,'effort':level},by='owner')
    state=snapshot(ctx)
    text='Усилия этого агента: '+(state['effort'] if state['enabled'] else 'настройка выключена')+'.'
    for row in state['models']:
        text+=f"\n{row['role']} · {row['model']}: {row['requested']} → {row['effective']} ({row['mode']})."
        if row['note']:text+=' '+row['note']
    return text+'\n/effort '+', '.join(LEVELS)+'. Отдельный выбор модели в ролях важнее этого умолчания.'
