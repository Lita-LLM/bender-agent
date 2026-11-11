"""Windows Job Objects: общий бюджет дерева и завершение при закрытии хозяина.

API/структуры — Microsoft Learn JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
JOBOBJECT_BASIC_LIMIT_INFORMATION, AssignProcessToJobObject. Это контроль
ресурсов, не изоляция чтения файлов или сети. ctypes загружается только здесь.
"""
from __future__ import annotations
import ctypes
import secrets

DWORD=ctypes.c_uint32
SIZE=ctypes.c_size_t
HANDLE=ctypes.c_void_p
BOOL=ctypes.c_int32
KILL_ON_CLOSE=0x2000
JOB_MEMORY=0x200
JOB_TIME=0x4
ACTIVE_PROCESS=0x8
EXTENDED_INFORMATION=9
ASSIGN_PROCESS=0x1


class BasicLimits(ctypes.Structure):
    _fields_=[('PerProcessUserTimeLimit',ctypes.c_int64),('PerJobUserTimeLimit',ctypes.c_int64),('LimitFlags',DWORD),
              ('MinimumWorkingSetSize',SIZE),('MaximumWorkingSetSize',SIZE),('ActiveProcessLimit',DWORD),
              ('Affinity',SIZE),('PriorityClass',DWORD),('SchedulingClass',DWORD)]


class IoCounters(ctypes.Structure):
    _fields_=[(name,ctypes.c_uint64) for name in ('ReadOperationCount','WriteOperationCount','OtherOperationCount','ReadTransferCount','WriteTransferCount','OtherTransferCount')]


class ExtendedLimits(ctypes.Structure):
    _fields_=[('BasicLimitInformation',BasicLimits),('IoInfo',IoCounters),('ProcessMemoryLimit',SIZE),('JobMemoryLimit',SIZE),('PeakProcessMemoryUsed',SIZE),('PeakJobMemoryUsed',SIZE)]


class Api:
    def __init__(self,dll=None):
        if dll is None:
            if not hasattr(ctypes,'WinDLL'):raise OSError('Job Objects доступны только Windows')
            dll=ctypes.WinDLL('kernel32',use_last_error=True)
        self.dll=dll
        signatures={
            'CreateJobObjectW':([HANDLE,ctypes.c_wchar_p],HANDLE),
            'SetInformationJobObject':([HANDLE,ctypes.c_int32,HANDLE,DWORD],BOOL),
            'OpenJobObjectW':([DWORD,BOOL,ctypes.c_wchar_p],HANDLE),
            'AssignProcessToJobObject':([HANDLE,HANDLE],BOOL),
            'GetCurrentProcess':([],HANDLE),
            'CloseHandle':([HANDLE],BOOL),
        }
        for name,(args,result) in signatures.items():
            function=getattr(dll,name);function.argtypes=args;function.restype=result

    @staticmethod
    def check(value,what):
        if not value:
            number=ctypes.get_last_error() if hasattr(ctypes,'get_last_error') else 0
            raise OSError(number,'Windows не выполнил '+what)
        return value

    def create(self,name):
        if hasattr(ctypes,'set_last_error'):ctypes.set_last_error(0)
        handle=self.check(self.dll.CreateJobObjectW(None,name),'CreateJobObject')
        if hasattr(ctypes,'get_last_error') and ctypes.get_last_error()==183:
            self.dll.CloseHandle(handle);raise OSError('Имя Job Object уже занято')
        return handle

    def configure(self,handle,info):
        self.check(self.dll.SetInformationJobObject(handle,EXTENDED_INFORMATION,ctypes.byref(info),ctypes.sizeof(info)),'SetInformationJobObject')

    def join(self,name):
        # Дочерняя обёртка получает лишь право присоединить себя, не менять лимиты.
        handle=self.check(self.dll.OpenJobObjectW(ASSIGN_PROCESS,False,name),'OpenJobObject')
        try:self.check(self.dll.AssignProcessToJobObject(handle,self.dll.GetCurrentProcess()),'AssignProcessToJobObject')
        finally:self.dll.CloseHandle(handle)

    def close(self,handle):self.dll.CloseHandle(handle)


class Job:
    def __init__(self,limits=None,*,api=None):
        self.api=api or Api();self.handle=None;self.name='Local\\Bender-'+secrets.token_hex(16)
        values=limits or {};info=ExtendedLimits();info.BasicLimitInformation.LimitFlags=KILL_ON_CLOSE
        if values.get('RLIMIT_AS',0)>0:
            info.JobMemoryLimit=int(values['RLIMIT_AS']);info.BasicLimitInformation.LimitFlags|=JOB_MEMORY
        if values.get('RLIMIT_CPU',0)>0:
            info.BasicLimitInformation.PerJobUserTimeLimit=int(values['RLIMIT_CPU'])*10_000_000
            info.BasicLimitInformation.LimitFlags|=JOB_TIME
        if values.get('RLIMIT_NPROC',0)>0:
            info.BasicLimitInformation.ActiveProcessLimit=int(values['RLIMIT_NPROC'])
            info.BasicLimitInformation.LimitFlags|=ACTIVE_PROCESS
        try:
            self.handle=self.api.create(self.name);self.api.configure(self.handle,info)
        except BaseException:self.close();raise

    def close(self):
        handle,self.handle=self.handle,None
        if handle is not None:self.api.close(handle)


def join(name):Api().join(name)


def parameters(agent_ctx):
    enabled=agent_ctx.settings.get('guard','windows_job',True)
    limits={}
    if agent_ctx.settings.get('guard','resource_limits',True):
        limits={'RLIMIT_CPU':agent_ctx.settings.get('guard','cpu_seconds',120),
                'RLIMIT_AS':agent_ctx.settings.get('guard','memory_mb',0)*1024*1024,
                'RLIMIT_NPROC':agent_ctx.settings.get('guard','process_count',128)}
    return {'enabled':enabled,'limits':limits}


def wrap(argv,limits=None):
    import json
    import sys
    from pathlib import Path
    job=Job(limits)
    config={'limits':{},'landlock':False,'write':[],'windows_job':job.name}
    return job,[sys.executable,'-I',str(Path(__file__).with_name('sandbox_child.py')),json.dumps(config,separators=(',',':')),*argv]
