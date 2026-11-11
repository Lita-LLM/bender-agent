"""Небольшая проверяемая форма elicitation: без вложенных объектов, кода и секретов."""
import math
import re
from datetime import date, datetime
from urllib.parse import urlsplit

from ..core.errors import HumanError


def bad(hint):return HumanError('mcp_form','Не удалось заполнить форму MCP',hint)


def finite(value):return type(value) is int and abs(value)<=2**53-1 or type(value) is float and math.isfinite(value)


def choices(field):
    node=field.get('items',{}) if field.get('type')=='array' else field
    if 'enum' in node:return node['enum']
    options=node.get('oneOf',node.get('anyOf'))
    if isinstance(options,list) and all(isinstance(x,dict) and isinstance(x.get('const'),str) for x in options):return [x['const'] for x in options]
    return None


def schema(value):
    if not isinstance(value,dict) or value.get('type')!='object' or not isinstance(value.get('properties'),dict) or len(value['properties'])>32:raise bad('Нужна простая форма: не более 32 полей.')
    if any(key in value for key in ('$ref','allOf','anyOf','oneOf','if','not','patternProperties')):raise bad('Составная схема формы не поддерживается.')
    required=value.get('required',[])
    if not isinstance(required,list) or any(not isinstance(key,str) or key not in value['properties'] for key in required):raise bad('Неверный список обязательных полей.')
    for name,field in value['properties'].items():
        if not isinstance(name,str) or not name or len(name)>128 or not isinstance(field,dict):raise bad('Неверное имя/описание поля.')
        if any(key in field and (not isinstance(field[key],str) or len(field[key])>1600) for key in ('title','description')):raise bad('Неверный заголовок/описание поля.')
        if re.search(r'password|passwd|secret|token|api.?key|парол|секрет|токен',name,re.I) or field.get('format')=='password':raise bad('Пароли/ключи/токены нельзя запрашивать формой; нужен вход на сайте сервиса.')
        kind=field.get('type')
        if kind not in ('string','integer','number','boolean','array'):raise bad('Только строки, числа, bool и выбор строк из списка.')
        if any(key in field for key in ('$ref','properties','allOf','if','not','pattern')):raise bad('Вложенные/исполняемые ограничения формы не поддерживаются.')
        options=choices(field)
        if ('enum' in field or 'oneOf' in field or kind=='array') and (not isinstance(options,list) or not options or len(options)>128 or any(not isinstance(x,str) or len(x)>4096 for x in options) or len(set(options))!=len(options)):raise bad('Неверные варианты выбора.')
        if kind=='array' and (not isinstance(field.get('items'),dict) or field['items'].get('type','string')!='string'):raise bad('Массив допускается только для выбора строк.')
        for key in ('minLength','maxLength','minItems','maxItems'):
            if key in field and (type(field[key]) is not int or not 0<=field[key]<=16384):raise bad('Неверный предел длины поля.')
        for key in ('minimum','maximum'):
            if key in field and not finite(field[key]):raise bad('Неверный предел числа.')
        for lower,upper in (('minimum','maximum'),('minLength','maxLength'),('minItems','maxItems')):
            if lower in field and upper in field and field[lower]>field[upper]:raise bad('Минимум поля больше максимума.')
        if field.get('format') not in (None,'email','uri','date','date-time'):raise bad('Неподдерживаемый формат строки.')
    return value


def validate(spec,content):
    schema(spec)
    if not isinstance(content,dict) or any(key not in spec['properties'] for key in content):raise bad('Ответ должен содержать только поля этой формы.')
    if any(key not in content for key in spec.get('required',[])):raise bad('Заполни обязательные поля.')
    for name,value in content.items():
        field=spec['properties'][name];kind=field['type'];options=choices(field)
        valid=(isinstance(value,str) if kind=='string' else type(value) is bool if kind=='boolean' else type(value) is int and finite(value) if kind=='integer' else finite(value) if kind=='number' else isinstance(value,list) and all(isinstance(x,str) for x in value))
        if not valid:raise bad('Неверный тип поля '+name+'.')
        if kind in ('string','array'):
            minimum=field.get('minItems' if kind=='array' else 'minLength',0);maximum=min(16384,field.get('maxItems' if kind=='array' else 'maxLength',16384))
            if not minimum<=len(value)<=maximum:raise bad('Неверная длина поля '+name+'.')
        if options is not None and any(item not in options for item in (value if kind=='array' else [value])):raise bad('Выбери предложенный вариант: '+name+'.')
        if kind=='array' and len(set(value))!=len(value):raise bad('Выбор не должен повторяться: '+name+'.')
        if kind in ('integer','number') and not field.get('minimum',-math.inf)<=value<=field.get('maximum',math.inf):raise bad('Число вне разрешённых границ: '+name+'.')
        if kind=='string' and field.get('format'):
            try:
                fmt=field['format']
                if fmt=='date':
                    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}',value):raise ValueError()
                    date.fromisoformat(value)
                elif fmt=='date-time':
                    if not re.fullmatch(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})',value):raise ValueError()
                    datetime.fromisoformat(value.replace('Z','+00:00'))
                elif fmt=='email' and not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',value):raise ValueError()
                elif fmt=='uri' and not urlsplit(value).scheme:raise ValueError()
            except ValueError:raise bad('Проверь формат поля '+name+'.') from None
    return content
