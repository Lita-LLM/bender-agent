"""Узкий перевод GFM в Rich Markdown по telegram-bot-api-10.3.md, 24.10.2026.

Медиа и кнопки в тексте не загружаются: для них есть отдельные защищённые действия.
Непомещающийся отдельный блок оставляет отправителю прежний HTML-путь.
"""
from __future__ import annotations
import html
import re

LIMIT=32768
MAX_LINES=150


class RichFormatError(ValueError):
    pass


def _inline(text):
    # Код и цели явных ссылок не трогаем. Медиа-синтаксис превращаем в ссылку.
    text=re.sub(r'!\[([^\[\]]*)\]\(([^)\n]+)\)',lambda m:'['+(m[1] or 'Файл')+']('+m[2]+')',text)
    pieces=re.split(r'(`+[^`\n]*`+|\[[^\[\]\n]*\]\([^)\n]*\))',text)
    for n in range(0,len(pieces),2):
        value=pieces[n]
        # Только безопасные теги без атрибутов. tg-thinking добавляет сама инфраструктура черновика.
        value=re.sub(r'<[^<>\n]*>',lambda m:m[0] if re.fullmatch(r'</?(?:u|ins|sub|sup|tg-spoiler|details|summary)>|<details open>',m[0]) else html.escape(m[0]),value)
        dollars=[];position=0
        pattern=re.compile(r'(?<!\\)\$(\$?)([^$\n]+)\$\1')
        while position<len(value):
            match=pattern.search(value,position)
            if not match:break
            formula=match[2].strip()
            valid=formula and formula[-1] not in '+-*/=,' and (match[1] or re.fullmatch(r'[A-Za-z0-9]',formula) or re.fullmatch(r'[\x20-\x7e]+',formula) and re.search(r'[=^_\\{}+*/-]',formula))
            if valid:
                dollars.append((match.start(),match.end()));position=match.end()
            else:position=match.start()+1
        out=[];span=0
        for i,char in enumerate(value):
            while span<len(dollars) and i>=dollars[span][1]:span+=1
            formula=span<len(dollars) and dollars[span][0]<=i<dollars[span][1]
            escaped=i>0 and value[i-1]=='\\'
            if char=='$' and not escaped and not formula:out.append('\\')
            if char=='#' and not escaped:out.append('\\')
            out.append(char)
        pieces[n]=''.join(out)
    return ''.join(pieces)


def normalize(text):
    lines=[];fence=None;depth=0
    for line in str(text or '').splitlines():
        match=re.match(r'^\s{0,3}(`{3,}|~{3,})(.*)$',line)
        if match:
            marker=match[1]
            if fence is None:fence=marker
            elif marker[0]==fence[0] and len(marker)>=len(fence) and not match[2].strip():fence=None
            lines.append(line);continue
        if fence is not None:lines.append(line);continue
        heading=re.match(r'^(#{1,6})(\s+)(.*)',line)
        if heading:line=heading[1]+heading[2]+_inline(heading[3])
        else:line=_inline(line)
        # Не даём обычным вложенным спискам превысить лимит 16 уровней.
        indent=len(line)-len(line.lstrip(' '))
        if indent>28 and re.match(r'\s*(?:[-*+] |\d+\. )',line):line=' '*28+line.lstrip(' ')
        depth+=len(re.findall(r'<details(?: open)?>',line))-line.count('</details>')
        if depth>12:raise RichFormatError('Слишком глубокая вложенность details')
        lines.append(line)
    if fence:lines.append(fence)
    return '\n'.join(lines)


def blocks(text):
    lines=text.splitlines(keepends=True);out=[];i=0
    while i<len(lines):
        start=i;line=lines[i]
        fence=re.match(r'^\s{0,3}(`{3,}|~{3,})',line)
        if fence:
            i+=1
            while i<len(lines):
                close=re.match(r'^\s{0,3}'+re.escape(fence[1][0])+'{'+str(len(fence[1]))+r',}\s*$',lines[i])
                i+=1
                if close:break
        elif i+1<len(lines) and '|' in line and re.fullmatch(r'\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*',lines[i+1].strip()):
            if len(line.strip().strip('|').split('|'))>20:raise RichFormatError('Таблица шире 20 столбцов')
            i+=2
            while i<len(lines) and '|' in lines[i] and lines[i].strip():i+=1
        elif '<details' in line:
            level=line.count('<details')-line.count('</details>');i+=1
            while i<len(lines) and level>0:
                level+=lines[i].count('<details')-lines[i].count('</details>');i+=1
        else:
            i+=1
            while i<len(lines) and lines[i].strip() and not re.match(r'^\s*(?:#{1,6} |`{3,}|~{3,}|<details)',lines[i]):
                if i+1<len(lines) and '|' in lines[i] and re.match(r'^\s*\|?\s*:?-{3,}',lines[i+1]):break
                i+=1
        out.append(''.join(lines[start:i]))
    return out


def parts(text):
    result=[];current='';original='';count=0
    for source in blocks(str(text or '')):
        block=normalize(source)
        if source.endswith('\n'):block+='\n'
        rows=block.count('\n')+1
        if len(block)>LIMIT or rows>MAX_LINES:
            raise RichFormatError('Один блок длиннее лимита Rich; нужен обычный путь')
        if current and (len(current)+len(block)>LIMIT or count+rows>MAX_LINES):
            result.append((original,current.rstrip()));current='';original='';count=0
        current+=block;original+=source;count+=rows
    if current.strip() or not result:result.append((original,current.rstrip()))
    return result


def chunks(text):
    return [formatted for _,formatted in parts(text)]


def draft(status,reasoning,previous,text,*,text_only=False):
    body=normalize(('' if text_only else previous+'\n\n')+text)
    # Теги мыслей — только здесь, вне преобразования ответа модели.
    if not text_only:
        thought=html.escape((status+'\n'+reasoning).strip())
        body='<tg-thinking>'+thought+'</tg-thinking>\n\n'+body
    if len(body)>LIMIT:raise RichFormatError('Черновик длиннее лимита')
    return {'markdown':body,'skip_entity_detection':True}
