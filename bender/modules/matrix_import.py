"""Предложения Matrix из уже сохранённого архива OpenClaw, без прав/секретов/автовхода."""
from .matrix_transport import homeserver
from ..core.errors import HumanError


def proposals(ctx):
    rows=ctx.store.meta_get('import:openclaw:channels',[]) or [];out=[]
    for row in rows if isinstance(rows,list) else []:
        matrix=row.get('matrix') if isinstance(row,dict) else None
        if not isinstance(matrix,dict):continue
        accounts=matrix.get('accounts')
        entries=[('общий',matrix)]
        if isinstance(accounts,dict):entries.extend((name,{**matrix,**value}) for name,value in accounts.items() if isinstance(name,str) and isinstance(value,dict))
        for name,config in entries[:32]:
            try:base=homeserver(config.get('homeserver'))
            except HumanError:continue
            rooms=[]
            for key in ('rooms','groups'):
                if isinstance(config.get(key),dict):rooms.extend(room for room in config[key] if isinstance(room,str) and room.startswith('!') and len(room)<=4096 and not any(ord(c)<33 for c in room))
            candidate={'account':name[:100],'homeserver':base,'rooms':list(dict.fromkeys(rooms))[:100],'encrypted':config.get('encryption') is True}
            if candidate not in out:out.append(candidate)
            if len(out)>=32:return out
    return out
