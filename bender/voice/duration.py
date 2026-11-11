"""Длительность по структуре файла, без декодирования звука и сторонних пакетов.

Неизвестный/повреждённый заголовок даёт None: метаданные отправителя не доказывают лимит.
"""
import io
import math
import struct
import wave


def ogg(raw):
    offset=0;streams={}
    while offset<len(raw):
        if raw[offset:offset+5]!=b'OggS\0' or offset+27>len(raw):return None
        count=raw[offset+26];start=offset+27+count
        if start>len(raw):return None
        end=start+sum(raw[offset+27:start])
        if end>len(raw):return None
        serial=struct.unpack_from('<I',raw,offset+14)[0];body=raw[start:end]
        stream=streams.setdefault(serial,{'rate':None,'skip':0,'last':None})
        if body.startswith(b'OpusHead') and len(body)>=19:stream.update(rate=48000,skip=struct.unpack_from('<H',body,10)[0])
        elif body.startswith(b'\x01vorbis') and len(body)>=16:stream['rate']=struct.unpack_from('<I',body,12)[0]
        granule=struct.unpack_from('<Q',raw,offset+6)[0]
        if granule!=2**64-1:stream['last']=granule
        offset=end
    if not streams or any(not s['rate'] or s['last'] is None or s['last']<s['skip'] for s in streams.values()):return None
    return sum((s['last']-s['skip'])/s['rate'] for s in streams.values())


def mp3(raw):
    offset=0;total=0;frames=0
    if raw.startswith(b'ID3'):
        if len(raw)<10 or any(v&128 for v in raw[6:10]):return None
        offset=10+sum(v<<(7*(3-i)) for i,v in enumerate(raw[6:10]))
        if raw[5]&16:offset+=10
    while offset+4<=len(raw):
        if raw[offset:offset+3]==b'TAG' and len(raw)-offset==128:break
        header=int.from_bytes(raw[offset:offset+4],'big')
        if header>>21!=0x7ff:return None
        version=(header>>19)&3;layer=(header>>17)&3;bitrate=(header>>12)&15;rate=(header>>10)&3
        if version==1 or layer!=1 or bitrate in (0,15) or rate==3:return None
        rates=(44100,48000,32000);sample_rate=rates[rate]//(1 if version==3 else 2 if version==2 else 4)
        bitrates=(0,32,40,48,56,64,80,96,112,128,160,192,224,256,320) if version==3 else (0,8,16,24,32,40,48,56,64,80,96,112,128,144,160)
        size=(144000 if version==3 else 72000)*bitrates[bitrate]//sample_rate+((header>>9)&1)
        if size<4 or offset+size>len(raw):return None
        side=(17 if (header>>6)&3==3 else 32) if version==3 else (9 if (header>>6)&3==3 else 17)
        tag=offset+4+(0 if header&(1<<16) else 2)+side
        # Xing/Info — служебный первый кадр, не добавляет звуковые отсчёты.
        if frames or raw[tag:tag+4] not in (b'Xing',b'Info'):total+=(1152 if version==3 else 576)/sample_rate
        frames+=1;offset+=size
    if offset<len(raw) and raw[offset:offset+3]!=b'TAG' and any(raw[offset:]):return None
    return total if frames else None


def atoms(raw,start,end):
    while start+8<=end:
        size,kind=struct.unpack_from('!I4s',raw,start);header=8
        if size==1:
            if start+16>end:return
            size=struct.unpack_from('!Q',raw,start+8)[0];header=16
        if not size:size=end-start
        if size<header or start+size>end:return
        yield kind,start+header,start+size
        start+=size


def mp4(raw):
    durations=[]
    for kind,start,end in atoms(raw,0,len(raw)):
        if kind!=b'moov':continue
        movie_rate=None
        for box,bs,be in atoms(raw,start,end):
            if box==b'mvhd' and be-bs>=20:
                at=20 if raw[bs]==1 else 12
                if bs+at+4<=be:movie_rate=struct.unpack_from('!I',raw,bs+at)[0]
        for track,ts,te in atoms(raw,start,end):
            if track!=b'trak':continue
            presentation=None
            if movie_rate:
                for box,bs,be in atoms(raw,ts,te):
                    if box==b'tkhd':
                        at,width=(28,8) if raw[bs]==1 else (20,4)
                        if bs+at+width<=be:
                            count=int.from_bytes(raw[bs+at:bs+at+width],'big')
                            if count and count<2**(width*8)-1:presentation=count/movie_rate
            for media,ms,me in atoms(raw,ts,te):
                if media!=b'mdia':continue
                duration=None;audio=False
                for box,bs,be in atoms(raw,ms,me):
                    if box==b'hdlr' and be-bs>=12:audio=raw[bs+8:bs+12]==b'soun'
                    if box==b'mdhd' and be-bs>=20:
                        version=raw[bs]
                        if version==0:rate,count=struct.unpack_from('!II',raw,bs+12)
                        elif version==1 and be-bs>=32:rate,count=struct.unpack_from('!IQ',raw,bs+20)
                        else:continue
                        if rate and count not in (2**32-1,2**64-1):duration=count/rate
                if audio and duration is not None:durations.append(presentation or duration)
    return max(durations) if durations else None


def seconds(raw,ext=''):
    try:
        if raw.startswith(b'RIFF') and raw[8:12]==b'WAVE':
            with wave.open(io.BytesIO(raw)) as sound:result=sound.getnframes()/sound.getframerate()
        elif raw.startswith(b'OggS'):result=ogg(raw)
        elif raw.startswith(b'fLaC') and len(raw)>=42 and raw[4]&127==0 and int.from_bytes(raw[5:8],'big')==34:
            bits=int.from_bytes(raw[18:26],'big');rate=bits>>44;count=bits&((1<<36)-1)
            result=count/rate if rate and count else None
        elif raw[4:8]==b'ftyp':result=mp4(raw)
        elif ext=='mp3' or raw.startswith(b'ID3') or raw[:1]==b'\xff':result=mp3(raw)
        else:return None
        return result if isinstance(result,(int,float)) and math.isfinite(result) and result>0 else None
    except (wave.Error,EOFError,ValueError,ZeroDivisionError,struct.error,IndexError):return None
