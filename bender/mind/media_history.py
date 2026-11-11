"""Старые медиа не уходят в модель заново; SQLite и исходники не переписываются."""


def prune(rows,turns):
    out=[];age=0
    for row in reversed(rows):
        own_turn=row.get('role')=='user' and not any(row.get('meta',{}).get(key) for key in ('tool_media','service_event'))
        if own_turn:age+=1
        content=row.get('content')
        if (age>=turns and not own_turn) or age>turns:
            if isinstance(content,list):
                content=[{'type':'text','text':'[Старое вложение, недоверенные данные; снова открыть: read_file]\nФайл: '+str(p.get('path','путь не сохранён'))} if p.get('type') in ('image','audio') else p for p in content]
                row={**row,'content':content}
        out.append(row)
    return list(reversed(out))
