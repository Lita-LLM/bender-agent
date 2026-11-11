"""OAuth MCP: проверка обнаруженных адресов, PKCE и привязки к издателю токена."""
import base64
import hashlib
import re
import secrets
from urllib.parse import urlsplit, urlunsplit
from urllib.request import parse_http_list

from ..core.errors import HumanError


def failed(hint):return HumanError('mcp_oauth','Не удалось войти в MCP-сервис',hint)


def secure_url(value,*,issuer=False):
    """Не нормализует issuer: при ответе требуется буквальное совпадение по RFC9207."""
    if not isinstance(value,str) or not value or len(value)>8192 or any(ord(char)<33 for char in value) or '\\' in value:raise failed('Нужен корректный адрес HTTPS.')
    try:
        parts=urlsplit(value);_=parts.port
        if parts.scheme!='https' or not parts.hostname or parts.username or parts.password or parts.fragment or parts.port==0 or (issuer and parts.query):raise ValueError()
    except ValueError:raise failed('Сервис входа должен использовать HTTPS без логина/пароля, фрагмента и неоднозначных адресов.') from None
    return value


def origin(value):
    parts=urlsplit(value)
    return parts.scheme.lower(),parts.hostname,parts.port or (443 if parts.scheme=='https' else 80)


def metadata_urls(value,*,resource=False):
    """RFC9728 для ресурса, RFC8414 и OIDC для издателя; порядок имеет значение."""
    secure_url(value,issuer=not resource)
    parts=urlsplit(value);base=urlunsplit((parts.scheme,parts.netloc,'','',''));path=parts.path.rstrip('/')
    if resource:return list(dict.fromkeys([base+'/.well-known/oauth-protected-resource'+path,base+'/.well-known/oauth-protected-resource']))
    suffixes=['/.well-known/oauth-authorization-server'+path,'/.well-known/openid-configuration'+path]
    if path:suffixes.append(path+'/.well-known/openid-configuration')
    return [base+suffix for suffix in suffixes]


def challenge(value):
    """Разбирает Bearer среди HTTP challenges, включая запятые внутри кавычек."""
    if not isinstance(value,str) or len(value)>16384:return {}
    scheme=None;found=[];current=None
    for part in parse_http_list(value):
        match=re.match(r'^([A-Za-z][A-Za-z0-9_-]*)\s+(.*)$',part)
        if match:
            scheme=match[1].lower();part=match[2]
            current={} if scheme=='bearer' else None
            if current is not None:found.append(current)
        if scheme!='bearer' or current is None:continue
        key,sep,data=part.partition('=')
        if not sep:continue
        key=key.strip().lower();data=data.strip()
        if data.startswith('"'):
            if not data.endswith('"'):raise failed('Неверные кавычки в WWW-Authenticate.')
            data=re.sub(r'\\(.)',r'\1',data[1:-1])
        if key in current:raise failed('Повтор параметра в WWW-Authenticate: '+key+'.')
        current[key]=data
    if len(found)>1:raise failed('Сервер прислал несколько Bearer challenges; требуется однозначный вход.')
    return found[0] if found else {}


def scopes(value):
    if not isinstance(value,str) or len(value)>4096 or any(not (char==' ' or 33<=ord(char)<=126 and char not in ('"','\\')) for char in value):raise failed('Неверный список прав OAuth.')
    values=value.split()
    if len(values)>128:raise failed('Сервис запросил больше 128 прав OAuth.')
    return ' '.join(dict.fromkeys(values))


def resource_metadata(data,server_url):
    if not isinstance(data,dict):raise failed('Вместо метаданных ресурса пришёл другой ответ.')
    resource=secure_url(data.get('resource'))
    if origin(resource)!=origin(server_url):raise failed('Метаданные относятся к другому узлу MCP; токен ему не передаётся.')
    issuers=data.get('authorization_servers')
    if not isinstance(issuers,list) or not 1<=len(issuers)<=16 or any(not isinstance(value,str) for value in issuers):raise failed('В метаданных нет списка серверов входа.')
    for value in issuers:secure_url(value,issuer=True)
    supported=data.get('scopes_supported',[])
    if not isinstance(supported,list) or len(supported)>128 or any(not isinstance(value,str) or not value or scopes(value)!=value or ' ' in value for value in supported):raise failed('Неверные scopes_supported ресурса.')
    return {'resource':resource,'issuers':list(dict.fromkeys(issuers)),'scopes_supported':supported}


def authorization_metadata(data,issuer):
    secure_url(issuer,issuer=True)
    if not isinstance(data,dict) or data.get('issuer')!=issuer:raise failed('Издатель в метаданных не совпадает с выбранным адресом; вход остановлен.')
    methods=data.get('code_challenge_methods_supported')
    if not isinstance(methods,list) or any(not isinstance(value,str) for value in methods) or 'S256' not in methods:raise failed('Сервер не подтвердил PKCE S256; вход без этой защиты запрещён.')
    result={'issuer':issuer}
    for key in ('authorization_endpoint','token_endpoint','registration_endpoint'):
        if key=='registration_endpoint' and key not in data:continue
        value=secure_url(data.get(key))
        if origin(value)!=origin(issuer):raise failed('Адрес '+key+' находится на другом узле издателя; такой вход пока не поддерживается.')
        result[key]=value
    advertised=data.get('authorization_response_iss_parameter_supported',False)
    if not isinstance(advertised,bool):raise failed('Неверное объявление проверки iss.')
    result['iss_required']=advertised
    result['client_metadata_supported']=data.get('client_id_metadata_document_supported') is True
    methods=data.get('token_endpoint_auth_methods_supported',['client_secret_basic'])
    if not isinstance(methods,list) or any(not isinstance(value,str) for value in methods):raise failed('Неверные способы проверки клиента.')
    result['auth_methods']=methods
    return result


def pkce():
    verifier=secrets.token_urlsafe(48)
    challenge_value=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode('ascii')).digest()).decode('ascii').rstrip('=')
    return verifier,challenge_value


def validate_issuer(expected,received,required):
    """Проверяется до error/code; регистр, слеш и кодирование не исправляются."""
    if received is None and not required:return
    if not isinstance(received,str) or received!=expected:raise failed('Издатель ответа OAuth не совпал с ожидаемым; код не отправлен.')
