"""Проверка существующих цепочек без запросов к провайдерам и потери неизвестных полей."""


def problem(entry, providers):
    if not isinstance(entry, dict):return 'Кандидат не является объектом'
    pid=entry.get('provider')
    if not isinstance(pid,str) or not isinstance(providers.get(pid),dict):return 'Провайдер больше не подключён'
    if not isinstance(entry.get('model'),str) or not entry['model'].strip():return 'Не указана модель'
    if not isinstance(entry.get('params',{}),dict) or not isinstance(entry.get('enabled',True),bool):return 'Повреждены параметры кандидата'
    return ''


def broken(roles, providers):
    from .config import ROLES
    out=[]
    for role in ROLES:
        entries=roles.get(role,[])
        if not isinstance(entries,list):
            out.append({'role':role,'index':0,'reason':'Цепочка не является списком'})
            continue
        for index,entry in enumerate(entries):
            reason=problem(entry,providers)
            if reason:
                out.append({'role':role,'index':index,'reason':reason,
                            'provider':str(entry.get('provider','')) if isinstance(entry,dict) else '',
                            'model':str(entry.get('model','')) if isinstance(entry,dict) else ''})
    return out


def cleaned(roles, providers):
    from .config import ROLES
    return {role:[entry for entry in entries if not problem(entry,providers)] if isinstance(entries,list) else []
            for role,entries in roles.items() if role in ROLES}
