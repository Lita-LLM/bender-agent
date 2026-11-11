"""Архив для разработчика из разрешённых технических полей, без исходных текстов."""
from __future__ import annotations

import io
import json
import math
from pathlib import Path
import re
import sqlite3
import zipfile

from .. import __version__
from ..core.errors import HumanError
from ..core.fsutil import atomic_write
from ..core.paths import PROGRAM_DIR
from ..core.pathutil import relative_path
from ..core.secrets import Secrets
from ..core.settings import SCHEMA,Settings
from ..guard.policy import CAPS,Action,Policy
from .doctor import Reader,collect

FILES={'doctor.json':'Состояние проверок без адресов, путей и содержимого данных.',
       'journal.json':'Последние решения: время, вид события и разрешённые технические поля; без текста и идентификаторов.',
       'logs.json':'Последние строки журнала падения: только классы ошибок и кадры программы; исходный текст исключён.',
       'settings.json':'Только известные числовые переключатели и фиксированные варианты; строки, JSON, ключи и адреса исключены.',
       'README.txt':'Версия, состав и пределы отчёта.'}
KINDS={'system','turn','decision','alarm','channel','backup','skill','update','rollback','exam','workshop.proxy','heart','sleep'}
ACTORS={'owner','agent','heart','cron','system','telegram','mail','sleep'}
ERRORS={'ValueError','TypeError','OSError','PermissionError','FileNotFoundError','HumanError','DatabaseError','OperationalError','RuntimeError','MemoryError','TimeoutError','ConnectionError','UnicodeError'}


def preview(limit=100):
    if not isinstance(limit,int) or not 1<=limit<=500:raise HumanError('report_limit','Нужен предел от 1 до 500 записей','Обычно достаточно 100.')
    return {'files':[{'name':name,'description':description} for name,description in FILES.items()],'limit':limit,
            'excluded':'Сейф/ключи/токены, адреса почты и серверов, пути хозяина, имена провайдеров, разговоры, память, произвольный текст логов/настроек/журнала.'}


def finite(value):
    try:return isinstance(value,(int,float)) and not isinstance(value,bool) and math.isfinite(value)
    except OverflowError:return False


def log_rows(root,limit):
    path=Path(root)/'local'/'last_crash.txt'
    try:
        if not path.is_file():return []
        with path.open('rb') as stream:
            stream.seek(0,2);size=stream.tell();stream.seek(max(0,size-65536))
            lines=stream.read(65536).decode('utf-8','replace').splitlines()[-limit:]
    except OSError:return [{'kind':'unavailable'}]
    out=[]
    for line in lines:
        frame=re.search(r'File "[^"\n]*?(bender/[a-zA-Z0-9_/.-]+\.py)", line (\d+)',line.replace('\\','/'))
        if frame:
            try:
                module=str(relative_path(frame[1]))
                if (PROGRAM_DIR/module).is_file():out.append({'kind':'frame','module':module,'line':int(frame[2])});continue
            except HumanError:pass
        kind=line.split(':',1)[0].rsplit('.',1)[-1].strip()
        out.append({'kind':'error','class':kind} if kind in ERRORS else {'kind':'text_omitted'})
    return out


def build(root,*,limit=100):
    plan=preview(limit);root=Path(root);doctor=collect(root);reader=None
    checks=[]
    numbers={'bytes','wal_bytes','port','candidates','broken','agents','last','last_verify','free_bytes','total_bytes','starts','pid','rss_mb','threads','fds','wchar','write_bytes'}
    switches={'limits','in_use','enabled','configured','verified','safe','watched','pending','has_previous','available','running'}
    enums={'os':{'linux','win32','darwin','cygwin','android'},'kind':{'bwrap','none','unshare','job','rlimit'},'state':{'off','connecting','checking','ok','error','не проверялся'}}
    for row in doctor['rows']:
        record={key:row[key] for key in ('id','level','title','action')};details={}
        for key,value in row.get('details',{}).items():
            if key in numbers and finite(value) or key in switches and isinstance(value,bool):details[key]=value
            elif key in enums and isinstance(value,str) and value in enums[key]:details[key]=value
            elif key=='python' and isinstance(value,str) and re.fullmatch(r'\d+\.\d+\.\d+(?:[a-z]\d+)?',value):details[key]=value
        record['details']=details;checks.append(record)
    payload={'doctor.json':{'format':1,'version':__version__,'ok':doctor['ok'],'rows':checks},
             'journal.json':[],'logs.json':log_rows(root,limit),'settings.json':{}}
    try:
        if (root/'bender.db').exists():
            reader=Reader(root/'bender.db')
            rows=reader.q('SELECT ts,kind,actor,data FROM journal ORDER BY seq DESC LIMIT ?',(limit,))
            for row in rows:
                try:data=json.loads(row['data'])
                except (ValueError,TypeError):data={}
                data=data if isinstance(data,dict) else {}
                record={'kind':row['kind'] if row['kind'] in KINDS else 'other','actor':row['actor'] if row['actor'] in ACTORS else 'other'}
                if finite(row['ts']):record['ts']=row['ts']
                if isinstance(data.get('capability'),str) and data.get('capability') in CAPS:record['capability']=data['capability']
                if data.get('verdict') in ('allow','deny','ask'):record['verdict']=data['verdict']
                for key in ('ok','tainted','tool_failed'):
                    if isinstance(data.get(key),bool):record[key]=data[key]
                for key in ('cost','seconds','count'):
                    if finite(data.get(key)):record[key]=data[key]
                payload['journal.json'].append(record)
            try:
                settings=Settings(reader)
                for name,group in SCHEMA.items():
                    if not group.fields:continue
                    stored=settings.group(name);clean={}
                    for field in group.fields:
                        value=stored.get(field.key)
                        if field.type=='bool' and isinstance(value,bool):clean[field.key]=value
                        elif field.type in ('int','float') and finite(value):clean[field.key]=value
                        elif field.type=='choice' and isinstance(value,str) and value in (field.choices or {}):clean[field.key]=value
                    if clean:payload['settings.json'][name]=clean
            except (HumanError,ValueError,TypeError):pass
    except (sqlite3.Error,OSError):pass
    finally:
        if reader:reader.close()
    known=[]
    try:
        secrets=Secrets(root/'secrets.json')
        known=[secrets.get(name) for name in secrets.names() if secrets.get(name) and len(secrets.get(name))>=4]
    except HumanError:pass
    def redact(value):
        if isinstance(value,str):
            for secret in known:value=value.replace(secret,'[скрыто]')
            return re.sub(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}','[почта скрыта]',value)
        if isinstance(value,list):return [redact(item) for item in value]
        if isinstance(value,dict):return {key:redact(item) for key,item in value.items()}
        return value
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',zipfile.ZIP_DEFLATED) as archive:
        for name,value in payload.items():archive.writestr(name,json.dumps(redact(value),ensure_ascii=False,allow_nan=False,indent=2))
        archive.writestr('README.txt','Бендер '+__version__+'\n'+json.dumps(plan,ensure_ascii=False,indent=2)+'\nИсходные строки исключены целиком; отчёт может не содержать причину сбоя. Не прикладывай к нему data/ или secrets.json.\n')
    return buffer.getvalue(),plan


def save(root,path,raw):
    path=Path(path).expanduser().resolve();reader=None;guard={'profile':'careful','enabled':True};rules=[]
    try:
        if (Path(root)/'bender.db').exists():
            reader=Reader(Path(root)/'bender.db');settings=Settings(reader)
            guard=settings.group('guard');rules=settings.group('guard_rules').get('rules',[])
    except (OSError,sqlite3.Error,HumanError):pass
    finally:
        if reader:reader.close()
    policy=Policy(work_dir=path.parent,data_dir=root,get_settings=lambda:guard,get_rules=lambda:rules)
    decision=policy.decide(Action('fs.write',str(path),actor='owner',summary='сохранить просмотренный технический отчёт'))
    if not decision.allowed:raise HumanError('report_denied','Отчёт нельзя сохранить в эту папку',decision.reason)
    try:atomic_write(path,raw)
    except OSError as exc:raise HumanError('report_write','Не удалось сохранить отчёт','Проверь папку назначения, права и свободное место.',detail=str(exc)) from None
    return path
