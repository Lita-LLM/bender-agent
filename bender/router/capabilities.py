"""Три состояния возможностей: каталог, явный выбор хозяина или датированный отказ API."""
import re

KEYS=('vision','audio_input')


def description(provider,model,key):
    if provider.get('kind')=='claude_code' and key=='vision':
        return {'value':True,'source':'client','at':None,'reason':'Картинки уходят в Claude Code изображениями (stream-json).'}
    if provider.get('kind') in ('claude_code','codex_cli','antigravity'):
        return {'value':False,'source':'client','at':None,'reason':'Текстовый CLI-протокол Бендера не передаёт исходные медиа; используй роль Зрение/Уши.'}
    records=provider.get('model_capabilities')
    stored=records.get(model,{}) if isinstance(records,dict) else {}
    if not isinstance(stored,dict):stored={}
    record=stored.get(key)
    if isinstance(record,dict) and 'value' in record and (record['value'] is None or isinstance(record['value'],bool)):
        return dict(record)
    item=next((m for m in provider.get('models',[]) if isinstance(m,dict) and m.get('id')==model),{})
    value=item.get(key)
    if not isinstance(value,bool):value=None
    if key=='vision' and value is None and provider.get('kind')=='chatgpt':
        from .chatgpt import MODELS
        if any(m['id']==model for m in MODELS):value=True
    return {'value':value,'source':'catalog','at':provider.get('models_at'),'reason':''}


def value(candidate,key):return description(candidate.provider,candidate.model,key)['value']


def rejected(error,messages):
    """Только явное отсутствие медиа-возможности, не битый файл или общий HTTP 400."""
    if error.http_status not in (None,400,415,422) or error.klass not in ('format','server'):return set()
    present={p.get('type') for m in messages or [] if isinstance(m.get('content'),list) for p in m['content'] if isinstance(p,dict)}
    text=(error.message[:4096]+' '+(error.raw or '')[:4096]).lower().replace('_',' ')
    out=set()
    array_rejected=bool(re.search(r'(?:expected|must be).{0,20}(?:a )?string.{0,80}(?:array|list)',text))
    for kind,key,words in [('image','vision',r'(?:images?|vision|image url|input image)'),('audio','audio_input',r'(?:audio|input audio|sound)')]:
        if kind not in present:continue
        patterns=[r'(?:does not|doesn.t|cannot|can.t|not) support.{0,70}'+words,
                  words+r'.{0,70}(?:not supported|unsupported|not allowed|only supported by certain models)',
                  r'(?:unsupported|not allowed)(?!\s+(?:parameter|field)).{0,70}'+words,
                  r'(?:invalid|unexpected|unknown) (?:content )?(?:value|type).{0,30}'+(r'(?:input image|image url)' if kind=='image' else r'input audio')+r'.{0,100}(?:supported|expected|allowed)']
        if array_rejected or any(re.search(pattern,text) for pattern in patterns) or re.search(r'(?:model|endpoint).{0,40}only supports? text',text):out.add(key)
    return out
