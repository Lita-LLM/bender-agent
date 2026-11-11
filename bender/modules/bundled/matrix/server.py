"""Отдельный MCP-транспорт Matrix. Код пакета никогда не импортируется в ядро."""
import base64
import json
import re
import sys
import uuid
sys.stdin.reconfigure(encoding='utf-8')
sys.stdout.reconfigure(encoding='utf-8')
from urllib.error import HTTPError
from urllib.parse import quote,urlencode,urlsplit
from urllib.request import Request,HTTPRedirectHandler,build_opener

MAX_FILE=20*1024*1024
MAX_FRAME=32*1024*1024
CONFIG={}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kw):return None


HTTP=build_opener(NoRedirect())


def token_url(value):
    if not isinstance(value,str) or any(ord(c)<33 for c in value) or '\\' in value:raise ValueError('Неверный адрес сервера Matrix.')
    parts=urlsplit(value);_=parts.port
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or (parts.scheme!='https' and not (parts.scheme=='http' and parts.hostname in ('127.0.0.1','localhost','::1'))):raise ValueError('Нужен HTTPS-сервер Matrix; HTTP допустим только на localhost.')
    return value.rstrip('/')


def segment(value):
    if not isinstance(value,str) or not value or len(value)>4096 or any(ord(c)<32 for c in value):raise ValueError('Неверный идентификатор Matrix.')
    return quote(value,safe='')


def request(method,path,body=None,*,raw=False,missing=False,mime='application/json'):
    if not CONFIG:raise ValueError('Подключи Matrix через экран Связи; транспорт не настроен.')
    data=body if isinstance(body,bytes) else json.dumps(body,ensure_ascii=False).encode() if body is not None else None
    req=Request(CONFIG['homeserver']+path,data=data,method=method,headers={'Authorization':'Bearer '+CONFIG['token'],'Content-Type':mime,'Accept':'*/*' if raw else 'application/json'})
    try:
        with HTTP.open(req,timeout=75) as response:
            limit=MAX_FILE if raw else 2*1024*1024
            payload=response.read(limit+1)
            if len(payload)>limit:raise ValueError('Ответ Matrix слишком велик.')
            if raw:return {'data':base64.b64encode(payload).decode(),'mime':response.headers.get_content_type()}
            result=json.loads(payload)
            if not isinstance(result,dict):raise ValueError('Matrix вернул неверный JSON.')
            return result
    except HTTPError as error:
        if missing and error.code==404:return None
        raise ValueError('Matrix ответил HTTP '+str(error.code)+'. Проверь доступ/права; редиректы и повторы не выполняются.') from None


def room_path():return '/_matrix/client/v3/rooms/'+segment(CONFIG['room'])


def status():
    who=request('GET','/_matrix/client/v3/account/whoami').get('user_id')
    encryption=request('GET',room_path()+'/state/m.room.encryption/',missing=True)
    members=request('GET',room_path()+'/joined_members').get('joined',{})
    if not isinstance(who,str) or not isinstance(members,dict) or len(members)>100:raise ValueError('Не удалось проверить участников комнаты.')
    return {'bot':who,'encrypted':encryption is not None,'members':list(members)}


def require_room():
    info=status();owner=CONFIG.get('owner')
    if info['encrypted']:raise ValueError('Комната шифрована. Этот модуль не поддерживает E2EE; выбери отдельную комнату без шифрования.')
    if not owner or set(info['members'])!={info['bot'],owner}:raise ValueError('В комнате должны быть только бот и привязанный хозяин. Отправка остановлена.')


def send_event(kind,content):
    require_room()
    return request('PUT',room_path()+'/send/'+segment(kind)+'/'+uuid.uuid4().hex,content)


def mxc(value):
    parts=urlsplit(value)
    if parts.scheme!='mxc' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or not re.fullmatch(r'/[A-Za-z0-9_-]+',parts.path):raise ValueError('Неверный URI вложения Matrix.')
    _=parts.port
    if len(parts.netloc)>256 or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789.-:[]' for c in parts.netloc):raise ValueError('Неверный сервер в URI Matrix.')
    return parts.netloc,parts.path[1:]


def invoke(name,args):
    if name=='status':return status()
    if name=='join':return request('POST','/_matrix/client/v3/join/'+segment(CONFIG['room']),{})
    if name=='sync':
        query={'timeout':60000,'set_presence':'offline','filter':json.dumps({'room':{'rooms':[CONFIG['room']],'timeline':{'limit':100},'state':{'types':['m.room.member','m.room.encryption']}}})}
        if args.get('since'):query['since']=args['since']
        return request('GET','/_matrix/client/v3/sync?'+urlencode(query))
    if name=='send':
        text=args['text']
        if not isinstance(text,str) or not text or len(text)>30000:raise ValueError('Сообщение Matrix — от 1 до 30000 знаков.')
        content={'msgtype':'m.text','body':text}
        if args.get('reply'):content['m.relates_to']={'m.in_reply_to':{'event_id':args['reply']}}
        return send_event('m.room.message',content)
    if name=='react':
        key=args['key']
        if not isinstance(key,str) or not key or len(key)>64:raise ValueError('Реакция Matrix — непустая строка до 64 знаков.')
        return send_event('m.reaction',{'m.relates_to':{'rel_type':'m.annotation','event_id':args['event_id'],'key':key}})
    if name=='download':
        require_room();server,media=mxc(args['uri'])
        return request('GET','/_matrix/client/v1/media/download/'+segment(server)+'/'+segment(media),raw=True)
    if name=='upload':
        require_room();data=base64.b64decode(args['data'],validate=True)
        if len(data)>MAX_FILE:raise ValueError('Вложение Matrix больше 20 МБ.')
        mime=args.get('mime','application/octet-stream')
        if not isinstance(mime,str) or len(mime)>128 or any(ord(c)<32 for c in mime):raise ValueError('Неверный тип вложения.')
        result=request('POST','/_matrix/media/v3/upload?'+urlencode({'filename':args['name']}),data,mime=mime)
        uri=result['content_uri'];mxc(uri)
        kind={'photo':'m.image','audio':'m.audio','voice':'m.audio','video':'m.video'}.get(args.get('kind'),'m.file')
        return send_event('m.room.message',{'msgtype':kind,'body':args.get('caption') or args['name'],'filename':args['name'],'url':uri,'info':{'mimetype':mime,'size':len(data)}})
    raise ValueError('Неизвестная операция транспорта Matrix.')


FIELDS={'status':{},'join':{},'sync':{'since':'string'},'send':{'text':'string','reply':'string'},'react':{'event_id':'string','key':'string'},'download':{'uri':'string'},'upload':{'data':'string','name':'string','mime':'string','kind':'string','caption':'string'}}
TOOLS=[{'name':name,'description':'Транспорт Matrix: '+name+'. Настройка/привязка только в Связях.',
        'inputSchema':{'type':'object','properties':{key:{'type':value} for key,value in fields.items()},'additionalProperties':False}} for name,fields in FIELDS.items()]


def main():
    while True:
        line=sys.stdin.buffer.readline(MAX_FRAME+1)
        if not line:return
        if len(line)>MAX_FRAME:return
        message=json.loads(line)
        if 'id' not in message:continue
        try:
            method=message['method'];params=message.get('params',{})
            if method=='server/discover':result={'supportedVersions':['2026-07-28'],'serverInfo':{'name':'matrix','version':'1.0.0'},'capabilities':{'tools':{}}}
            elif method=='tools/list':result={'tools':TOOLS}
            elif method=='channel/configure':
                CONFIG.clear();CONFIG.update(homeserver=token_url(params['homeserver']),token=params['token'],room=params['room'],owner=params.get('owner'))
                if not isinstance(CONFIG['token'],str) or not CONFIG['token'] or any(ord(c)<33 for c in CONFIG['token']):raise ValueError('Неверный токен Matrix.')
                segment(CONFIG['room']);result={}
            elif method=='tools/call':result={'content':[],'structuredContent':invoke(params['name'],params.get('arguments',{}))}
            else:raise ValueError('Метод не поддержан транспортом Matrix.')
            reply={'jsonrpc':'2.0','id':message['id'],'result':result}
        except Exception as error:
            # HTTP/SSL/декодеры могут включать данные запроса в ошибку: не копируем их.
            message_text=str(error) if type(error) is ValueError else 'Ошибка транспорта Matrix; проверь сервер, сертификат и настройки.'
            reply={'jsonrpc':'2.0','id':message['id'],'error':{'code':-32000,'message':message_text[:1000]}}
        sys.stdout.write(json.dumps(reply,ensure_ascii=False)+'\n');sys.stdout.flush()


if __name__=='__main__':main()
