"""Заголовки MCP 2026: имена/URI и статические примитивные параметры тулов."""
import base64
import re

from ..core.errors import HumanError


def invalid(hint):return HumanError('mcp_header','Некорректные заголовки MCP',hint)


def header_value(value):
    """Sentinel тоже кодируется, чтобы сервер отличал его от буквальной строки."""
    if not isinstance(value,str):raise invalid('Имя или URI должны быть строкой.')
    if value!=value.strip() or any(not (32<=ord(c)<=126 or c=='\t') for c in value) or (value.startswith('=?base64?') and value.endswith('?=')):
        return '=?base64?'+base64.b64encode(value.encode('utf-8')).decode('ascii')+'?='
    return value


def annotations(schema):
    """Только properties-цепочки; items/$ref/условия/композиции не дают пути извлечения."""
    found=[];names=set()
    def walk(node,path):
        if not isinstance(node,dict):return
        if 'x-mcp-header' in node:
            name=node['x-mcp-header'];kind=node.get('type')
            if path is None or not path or kind not in ('string','integer','boolean'):raise invalid('x-mcp-header разрешён только строкам, целым и bool по статическому пути properties.')
            if not isinstance(name,str) or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+",name) or name.lower() in names:raise invalid('Имя x-mcp-header должно быть HTTP-токеном без повторов регистра.')
            names.add(name.lower());found.append((path,name,kind))
        for key,value in node.items():
            if key in ('properties','patternProperties','$defs','definitions','dependentSchemas','dependencies') and isinstance(value,dict):
                for prop,child in value.items():walk(child,path+(prop,) if key=='properties' and path is not None else None)
            elif key in ('items','prefixItems','allOf','anyOf','oneOf'):
                for child in value if isinstance(value,list) else [value]:walk(child,None)
            elif key in ('additionalProperties','additionalItems','contains','propertyNames','not','if','then','else','unevaluatedProperties','unevaluatedItems','contentSchema'):walk(value,None)
    walk(schema,())
    return found


def parameter_headers(schema,arguments):
    result={};missing=object()
    for path,name,kind in annotations(schema):
        value=arguments
        for part in path:
            value=value.get(part,missing) if isinstance(value,dict) else missing
            if value is missing:break
        if value is missing:continue
        expected={'string':str,'integer':int,'boolean':bool}[kind]
        if type(value) is not expected or (kind=='integer' and abs(value)>2**53-1):raise invalid('Неверный тип/небезопасное целое для параметра '+'.'.join(path)+'.')
        text=('true' if value else 'false') if kind=='boolean' else str(value)
        result['Mcp-Param-'+name]=header_value(text)
    return result
