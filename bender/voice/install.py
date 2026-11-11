"""Установка необязательного голоса в свой venv; модели проверяются до включения.

Репозитории/имена — upstream onnx-asr 0.12.0 и Piper download_voices.py; голоса сообщества — rraaww/ru_piper
со своим словарём произношения mitrokun/espeak-ng-data (без него эти голоса звучат криво).
HF отдаёт commit и LFS SHA256 (для малых файлов git blob SHA1); загрузка
идёт по зафиксированному commit, итоговый манифест хранится рядом с моделями.
"""
from __future__ import annotations
import asyncio
import fnmatch
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import secrets
import shutil
import subprocess
import sys
import urllib.error
import urllib.request
import venv
from ..core.errors import HumanError
from ..core.fsutil import atomic_write
from ..core.pathutil import canonical,within
from ..tools.registry import ToolContext

PACKAGES=('onnx-asr[cpu]==0.12.0','onnxruntime==1.23.2','piper-tts==1.8.0','soundfile==0.13.1')
STOCK=('ruslan','irina')  # Денис и Дмитрий убраны после сравнения на слух; уже установленные продолжают работать
# Голоса сообщества: обучены на исправленном словаре espeak-ng (йотированные фонемы, ударения).
COMMUNITY={'terra':'ru_RU-terra5871-medium.onnx','igm':'ru_RU-igm3804-medium.onnx','sova':'ru_RU-sova200-medium.onnx',
           'pvo':'ru_RU-pvo240-medium.onnx','fr':'ru_RU-fr6800-medium.onnx','mari':'ru_RU-mari-medium_epoch5699.onnx',
           'sushkov':'ru_RU-sushkov_v4-medium.onnx'}
VOICES=tuple(COMMUNITY)+STOCK
DEFAULT='terra'


def chosen(voice):
    """Голос из настроек; убранный из выбора (denis, dmitri) — заменяется голосом по умолчанию."""
    return voice if voice in VOICES else DEFAULT
REPOS={'asr':'istupakov/gigaam-v3-onnx','tts':'rhasspy/piper-voices','tts_community':'rraaww/ru_piper','dictionary':'mitrokun/espeak-ng-data'}
# Словарь берётся с GitHub по зафиксированному commit; хеши сверены вручную при добавлении.
DICTIONARY={'revision':'c786d9bc71f86ae7d69c6d57366df0990682056b','files':{
    'phondata':'74f2f4f74e2e4fa2ff0848c9b183eb0cc105fac45634d2813e61e08a92872d1f',
    'phondata-manifest':'d692a0f9fa209dfd1e82baf5a096046074bdbd7992732d0c8130401dd3dcc22a',
    'phonindex':'5b11175c3e8a0063dbf9ce2fb7f72e8a525b0d53223193b1485e606d9e0e4ad7',
    'phontab':'1bca6ff15224faf125a3545d7c991cca512150ba936eb2f92cecf23951d5f101',
    'ru_dict':'0ce7526053a84edfa3d42e2ce9574f4d250238f5270c2146f643e7bc3612f290'}}


def tts_repo(voice):
    return REPOS['tts_community'] if voice in COMMUNITY else REPOS['tts']


def dictionary_files():
    base='https://raw.githubusercontent.com/'+REPOS['dictionary']+'/'+DICTIONARY['revision']+'/espeak-ng-data/'
    return {'repo':REPOS['dictionary'],'revision':DICTIONARY['revision'],
            'files':[{'name':'espeak-ng-data/'+name,'local':name,'algorithm':'sha256','hash':digest,'url':base+name} for name,digest in DICTIONARY['files'].items()]}


def stock_espeak(python):
    """Папка словарей espeak-ng внутри пакета piper в venv голоса."""
    try:result=subprocess.run([str(python),'-I','-c','import os,piper;print(os.path.join(os.path.dirname(piper.__file__),"espeak-ng-data"))'],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=120)
    except (OSError,subprocess.SubprocessError):raise problem('Не удалось найти словари Piper') from None
    path=Path(result.stdout.decode('utf-8','replace').strip())
    if result.returncode or not (path/'phontab').is_file():raise problem('В пакете Piper нет словарей espeak-ng')
    return path


def problem(title,detail=''):
    return HumanError('local_voice',title,'Проверь сеть, свободное место и Python 3.10+; затем повтори bender voice install.',detail=detail)


def metadata(repo):
    try:
        with urllib.request.urlopen('https://huggingface.co/api/models/'+repo+'?blobs=true',timeout=45) as response:
            raw=response.read(10*1024*1024+1)
        if len(raw)>10*1024*1024:raise ValueError('Слишком большой список файлов')
        value=json.loads(raw)
        if not re.fullmatch('[0-9a-f]{40,64}',value.get('sha','')):raise ValueError('Нет commit моделей')
        return value
    except (OSError,ValueError,TypeError) as error:raise problem('Не удалось получить проверяемый список моделей Hugging Face',type(error).__name__) from None


def model_files(info,kind,voice=DEFAULT):
    if voice not in VOICES:raise problem('Неизвестный русский голос Piper')
    siblings={item['rfilename']:item for item in info.get('siblings',[]) if isinstance(item,dict) and isinstance(item.get('rfilename'),str)}
    quantization=None
    if kind=='asr':
        quantized=[name for name in siblings if fnmatch.fnmatchcase(name,'v3_e2e_ctc?int8.onnx')]
        model=quantized[0] if len(quantized)==1 else 'v3_e2e_ctc.onnx'
        if model!='v3_e2e_ctc.onnx':quantization='int8'
        names=['config.json','v3_e2e_ctc_vocab.txt',model]
        names += [name for name in siblings if name in (model+'.data',model[:-5]+'.onnx_data')]
    elif voice in COMMUNITY:
        model=COMMUNITY[voice]
        names=[model,model+'.json']
    else:
        model='ru/ru_RU/'+voice+'/medium/ru_RU-'+voice+'-medium.onnx'
        names=[model,model+'.json']
    repo=REPOS['asr'] if kind=='asr' else tts_repo(voice)
    files=[]
    for name in names:
        item=siblings.get(name)
        if not item:raise problem('В репозитории не найден файл модели',name)
        lfs=item.get('lfs') or {};sha=lfs.get('sha256') or lfs.get('oid')
        algorithm='sha256' if isinstance(sha,str) and re.fullmatch('[0-9a-f]{64}',sha) else 'git-sha1'
        digest=sha if algorithm=='sha256' else item.get('blobId','')
        if not re.fullmatch('[0-9a-f]{64}' if algorithm=='sha256' else '[0-9a-f]{40}',digest):
            raise problem('У файла модели нет проверяемого хеша',name)
        files.append({'name':name,'local':Path(name).name,'algorithm':algorithm,'hash':digest,
                      'url':'https://huggingface.co/'+repo+'/resolve/'+info['sha']+'/'+name})
    return {'repo':repo,'revision':info['sha'],'files':files,'quantization':quantization,'model':Path(model).name}


def download(item,target):
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    temporary=target.with_name(target.name+'.download')
    def matches(path):
        digest=hashlib.sha256() if item['algorithm']=='sha256' else hashlib.sha1()
        if item['algorithm']=='git-sha1':digest.update(('blob '+str(path.stat().st_size)+'\0').encode())
        with path.open('rb') as source:
            for block in iter(lambda:source.read(1024*1024),b''):digest.update(block)
        return digest.hexdigest()==item['hash']
    try:
        if target.is_file() and matches(target):return False
        with urllib.request.urlopen(item['url'],timeout=120) as response,temporary.open('wb') as output:
            total=0
            while True:
                block=response.read(1024*1024)
                if not block:break
                total+=len(block)
                if total>2*1024**3:raise ValueError('Модель больше 2 ГБ')
                output.write(block)
        if not matches(temporary):raise ValueError('Хеш не совпал')
        os.replace(temporary,target)
        return True
    except (OSError,ValueError) as error:raise problem('Модель не прошла загрузку или проверку хеша',type(error).__name__) from None
    finally:
        if temporary.exists():temporary.unlink()


def command(argv,timeout=900):
    try:result=subprocess.run([str(value) for value in argv],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
    except (OSError,subprocess.SubprocessError):raise problem('Не удалось запустить подготовку голосового процесса') from None
    if result.returncode:raise problem('Не удалось подготовить отдельный голосовой процесс','Код завершения '+str(result.returncode))


def unit(folder,port,threads):
    def quote(value):return '"'+str(value).replace('\\','\\\\').replace('"','\\"').replace('%','%%')+'"'
    executable=Path(folder)/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    script=Path(__file__).with_name('local_server.py').resolve()
    return ('[Unit]\nDescription=Локальный голос Бендера\nAfter=network.target\n\n[Service]\nType=simple\n'
            +'ExecStart='+' '.join(quote(value) for value in (executable,script,'--folder',folder,'--port',port,'--threads',threads))+'\n'
            +'Restart=on-failure\nRestartSec=60\nNoNewPrivileges=true\nPrivateTmp=true\nUMask=0077\n\n[Install]\nWantedBy=default.target\n')


def installed_voices(folder):
    """Уже поставленные голоса, у которых модель на месте; старый манифест знал один голос."""
    try:old=json.loads((Path(folder)/'installed.json').read_text())
    except (OSError,ValueError):return {}
    if not isinstance(old,dict):return {}
    voices=old.get('voices') if isinstance(old.get('voices'),dict) else {old.get('voice'):{'tts_file':old.get('tts_file'),'espeak_dir':old.get('espeak_dir')}}
    return {name:dict(entry) for name,entry in voices.items() if isinstance(name,str) and isinstance(entry,dict) and isinstance(entry.get('tts_file'),str)
            and Path(entry['tts_file']).name==entry['tts_file'] and (Path(folder)/'models'/'tts'/entry['tts_file']).is_file()}


def previous_default(folder,voices):
    try:name=json.loads((Path(folder)/'installed.json').read_text()).get('voice')
    except (OSError,ValueError,AttributeError):return None
    return name if name in voices else None


async def install(app,*,agent=None,voice=DEFAULT,service=True):
    """Без agent голос становится голосом по умолчанию; с agent — личным голосом этого агента, остальные голоса остаются."""
    default=not agent
    if platform.machine().lower() not in ('x86_64','amd64','aarch64','arm64'):
        raise problem('Для этой архитектуры нет проверенных колёс голоса','Используй 64-битную ОС на Pi 4/5 (aarch64) или облачную цепочку; armv7 не обещается.')
    if voice not in VOICES:raise problem('Неизвестный голос Piper')
    root=app.host;ctx=root.agents.get(root.agents.resolve(agent)) if agent else root;folder=canonical(root.dirs.local/'voice')
    if not within(folder,root.dirs.local) or not within(folder,root.dirs.root):raise problem('Папка локального голоса стала ссылкой наружу')
    await ToolContext(ctx,actor='owner').check('voice.install',str(folder),'Установить GigaAM v3 и Piper '+voice+' в отдельный процесс'+(' (голос сообщества rraaww/ru_piper и словарь mitrokun/espeak-ng-data)' if voice in COMMUNITY else ''),detail={'packages':list(PACKAGES),'repositories':REPOS,'systemd':service})
    asr=await asyncio.to_thread(metadata,REPOS['asr']);tts=await asyncio.to_thread(metadata,tts_repo(voice))
    manifests={'asr':model_files(asr,'asr',voice),'tts':model_files(tts,'tts',voice)}
    if voice in COMMUNITY:manifests['espeak']=dictionary_files()
    def prepare():
        reused=downloaded=0
        folder.mkdir(parents=True,exist_ok=True)
        if not within(folder/'venv',folder) or not within(folder/'models',folder):raise problem('Подпапка голосового пакета стала ссылкой наружу')
        venv.EnvBuilder(with_pip=True).create(folder/'venv')
        python=folder/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        command([python,'-m','pip','install','--disable-pip-version-check',*PACKAGES])
        command([python,'-c',"import onnx_asr,piper,soundfile as sf; assert sf.check_format('OGG','OPUS')"])
        for kind,manifest in manifests.items():
            for item in manifest['files']:
                target=folder/'models'/kind/item['local']
                if not within(target,folder):raise problem('Путь модели вышел из пакета')
                if download(item,target):downloaded+=1
                else:reused+=1
        espeak=None
        if 'espeak' in manifests:
            # Полный набор словарей Piper, поверх — исправленный русский; стандартный набор в venv не трогается.
            espeak=folder/'models'/'tts'/'espeak-ng-data'
            if not within(espeak,folder):raise problem('Путь словаря вышел из пакета')
            if espeak.exists():shutil.rmtree(espeak)
            shutil.copytree(stock_espeak(python),espeak)
            for item in manifests['espeak']['files']:shutil.copyfile(folder/'models'/'espeak'/item['local'],espeak/item['local'])
        voices=installed_voices(folder)
        voices[voice]={'tts_file':manifests['tts']['model'],'espeak_dir':espeak.name if espeak else None,'repo':manifests['tts']['repo'],'revision':manifests['tts']['revision']}
        if espeak:  # словарь общий: голоса сообщества, поставленные раньше, тоже его получают
            for entry in voices.values():
                if entry['tts_file'] in COMMUNITY.values():entry['espeak_dir']=espeak.name
        main=voice if default else previous_default(folder,voices) or voice
        state={'packages':list(PACKAGES),'models':manifests,'voices':voices,'voice':main,'tts_file':voices[main]['tts_file'],
               'quantization':manifests['asr']['quantization'],'espeak_dir':voices[main]['espeak_dir']}
        atomic_write(folder/'installed.json',json.dumps(state,ensure_ascii=False,indent=2).encode())
        token_path=folder/'token'
        if not within(token_path,folder):raise problem('Ключ голосового процесса стал ссылкой наружу')
        if not token_path.exists():atomic_write(token_path,secrets.token_urlsafe(32).encode())
        token_path.chmod(0o600)
        return token_path.read_text().strip(),reused,downloaded,main
    token,reused,downloaded,main=await asyncio.to_thread(prepare)
    config=root.settings.group('voice_local');port=config['port']
    provider=next((p for p in root.models.providers().values() if p.get('preset')=='local_voice'),None)
    if provider is None:provider=root.models.add_provider(preset='local_voice',base_url='http://127.0.0.1:'+str(port)+'/v1',api_key=token,extra={'models':[{'id':'gigaam-v3-e2e-ctc','audio_role':'ears'},{'id':'piper-ru','audio_role':'voice'}]})
    else:provider=root.models.update_provider(provider['id'],{'base_url':'http://127.0.0.1:'+str(port)+'/v1','api_key':token})
    root.settings.set('voice_local',{'enabled':True,'voice':main},by='owner')
    if not default and voice!=main:ctx.settings.set('voice',{'piper':voice},by='owner')  # личный голос, если он не совпал с общим
    from .connect import connect
    connections=await connect(ToolContext(ctx,actor='owner'),agent or 'all',only_empty=not bool(agent))
    service_path=folder/'bender-voice.service'
    atomic_write(service_path,unit(folder,port,config['threads']).encode())
    service_enabled=False
    service_note='Автозапуск не включён; запусти bender voice serve или bender service install --voice.'
    if service and sys.platform.startswith('linux'):
        try:
            from ..core.service import change
            result=await change(root,'install',voice=True);service_enabled=True;service_note=result['message']
        except HumanError as error:service_note=error.title+'. '+error.hint+' '+service_note
    restart=' Если голосовая служба уже работала, перезапусти её: systemctl --user restart bender-voice.service; при voice serve — останови и запусти снова. Перезапуск ядра не нужен.'
    return {'installed':True,'agent':ctx.agent_id,'provider':provider['id'],'voice':voice,'voices':sorted(installed_voices(folder)),'quantization':manifests['asr']['quantization'],'service':str(service_path),'service_enabled':service_enabled,
            'connected':connections['connected'],'reused_files':reused,'downloaded_files':downloaded,'note':'Пакет настроен. '+connections['note']+' Проверка скорости: bender voice bench. '+service_note+restart}


async def add_voice(app,voice):
    """Докачать голос в уже установленный пакет: только модель (и словарь для голосов сообщества), без pip и без смены голоса по умолчанию."""
    if voice not in VOICES:raise problem('Неизвестный голос Piper')
    root=app.host;folder=canonical(root.dirs.local/'voice')
    if not within(folder,root.dirs.local) or not within(folder,root.dirs.root):raise problem('Папка локального голоса стала ссылкой наружу')
    if not (folder/'installed.json').is_file():raise HumanError('local_voice','Локальный голос ещё не установлен','Выполни bender voice install.')
    if voice in installed_voices(folder):return {'voice':voice,'downloaded':False}
    await ToolContext(root,actor='owner').check('voice.install',str(folder),'Скачать голос Piper '+voice+(' (голос сообщества rraaww/ru_piper и словарь mitrokun/espeak-ng-data)' if voice in COMMUNITY else ''),detail={'repositories':REPOS})
    tts=await asyncio.to_thread(metadata,tts_repo(voice))
    manifests={'tts':model_files(tts,'tts',voice)}
    if voice in COMMUNITY:manifests['espeak']=dictionary_files()
    def prepare():
        python=folder/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
        for kind,manifest in manifests.items():
            for item in manifest['files']:
                target=folder/'models'/kind/item['local']
                if not within(target,folder):raise problem('Путь модели вышел из пакета')
                download(item,target)
        espeak=folder/'models'/'tts'/'espeak-ng-data'
        if not within(espeak,folder):raise problem('Путь словаря вышел из пакета')
        if 'espeak' in manifests and not (espeak/'phontab').is_file():
            if espeak.exists():shutil.rmtree(espeak)
            shutil.copytree(stock_espeak(python),espeak)
            for item in manifests['espeak']['files']:shutil.copyfile(folder/'models'/'espeak'/item['local'],espeak/item['local'])
        state=json.loads((folder/'installed.json').read_text())
        voices=installed_voices(folder)
        voices[voice]={'tts_file':manifests['tts']['model'],'espeak_dir':espeak.name if 'espeak' in manifests else None,'repo':manifests['tts']['repo'],'revision':manifests['tts']['revision']}
        main=state.get('voice') if state.get('voice') in voices else voice
        state.update(voices=voices,voice=main,tts_file=voices[main]['tts_file'],espeak_dir=voices[main]['espeak_dir'])
        atomic_write(folder/'installed.json',json.dumps(state,ensure_ascii=False,indent=2).encode())
    await asyncio.to_thread(prepare)
    return {'voice':voice,'downloaded':True}
