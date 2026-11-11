"""Windows API на Linux: ABI, лимиты дерева, раннее присоединение и закрытие."""
import asyncio
import ctypes
import json
from pathlib import Path
from types import SimpleNamespace
import sys
from unittest.mock import AsyncMock,patch
from bender.guard import sandbox,sandbox_child,windows_job as win
from bender.router import cli_common
from bender.tools import shell
from tests.helpers import TempDirCase,async_test


class Function:
    def __init__(self,callback):self.callback=callback
    def __call__(self,*args):return self.callback(*args)


class Kernel:
    def __init__(self,fail=False):
        self.calls=[];self.fail=fail;self.info=None
        self.CreateJobObjectW=Function(lambda unused,name:self.record('create',name,result=41))
        self.SetInformationJobObject=Function(self.configure)
        self.OpenJobObjectW=Function(lambda access,inherit,name:self.record('open',access,inherit,name,result=52))
        self.GetCurrentProcess=Function(lambda:-1)
        self.AssignProcessToJobObject=Function(lambda job,process:self.record('assign',job,process,result=1))
        self.CloseHandle=Function(lambda handle:self.record('close',handle,result=1))
    def record(self,name,*args,result):self.calls.append((name,*args));return result
    def configure(self,handle,kind,value,size):
        self.calls.append(('configure',handle,kind,size));self.info=win.ExtendedLimits.from_buffer_copy(ctypes.string_at(value,size))
        return not self.fail


class WindowsJobTest(TempDirCase):
    def test_abi_total_memory_cpu_count_and_kill_on_last_close(self):
        kernel=Kernel();job=win.Job({'RLIMIT_AS':512*1024*1024,'RLIMIT_CPU':3,'RLIMIT_NPROC':20},api=win.Api(kernel))
        info=kernel.info
        self.assertEqual(info.BasicLimitInformation.LimitFlags,win.KILL_ON_CLOSE|win.JOB_MEMORY|win.JOB_TIME|win.ACTIVE_PROCESS)
        self.assertEqual(info.JobMemoryLimit,512*1024*1024);self.assertEqual(info.ProcessMemoryLimit,0)
        self.assertEqual(info.BasicLimitInformation.PerJobUserTimeLimit,30_000_000)
        self.assertEqual(info.BasicLimitInformation.ActiveProcessLimit,20)
        if ctypes.sizeof(ctypes.c_void_p)==8:self.assertEqual(ctypes.sizeof(info),144)
        self.assertTrue(job.name.startswith('Local\\Bender-'))
        job.close();job.close();self.assertEqual([call for call in kernel.calls if call[0]=='close'],[('close',41)])
        failed=Kernel(fail=True)
        with self.assertRaises(OSError):win.Job(api=win.Api(failed))
        self.assertIn(('close',41),failed.calls)

    def test_child_only_gets_assign_right_and_closes_own_handle(self):
        kernel=Kernel();win.Api(kernel).join('Local\\job')
        self.assertEqual(kernel.calls,[('open',win.ASSIGN_PROCESS,False,'Local\\job'),('assign',52,-1),('close',52)])

    def test_wrapper_joins_before_target_and_failure_never_runs_target(self):
        events=[];config=json.dumps({'limits':{},'landlock':False,'windows_job':'Local\\job'})
        with patch.object(sys,'argv',['sandbox_child.py',config,'target.exe','two words']),patch('runpy.run_path',return_value={'join':lambda name:events.append(('join',name))}),patch('subprocess.call',side_effect=lambda args:events.append(('target',args)) or 7):
            with self.assertRaises(SystemExit) as ended:sandbox_child.main()
        self.assertEqual(ended.exception.code,7);self.assertEqual(events,[('join','Local\\job'),('target',['target.exe','two words'])])
        with patch.object(sys,'argv',['sandbox_child.py',config,'target.exe']),patch('runpy.run_path',return_value={'join':lambda name:(_ for _ in ()).throw(OSError('Не удалось присоединить'))}),patch('subprocess.call') as target:
            with self.assertRaises(OSError):sandbox_child.main()
            target.assert_not_called()
        shell_config=json.dumps({'limits':{},'landlock':False,'windows_job':'Local\\job','windows_shell':True})
        with patch.object(sys,'argv',['sandbox_child.py',shell_config,'echo "две части"']),patch('runpy.run_path',return_value={'join':lambda name:None}),patch('subprocess.call',return_value=0) as target:
            with self.assertRaises(SystemExit):sandbox_child.main()
        target.assert_called_once_with('echo "две части"',shell=True)

    def test_prepared_preserves_raw_argv_and_stop_closes_whole_tree(self):
        kernel=Kernel();job=win.Job(api=win.Api(kernel));argv=['C:\\Program Files\\server.exe','--arg','two words']
        info={'kind':'none','windows_job':True,'limits':{},'write_work':True}
        with patch.object(win,'Job',return_value=job):
            prepared=sandbox.prepare('Не разбирать shlex на Windows',raw_argv=argv,info=info,work_dir=self.tmp,allow_net=False)
        self.assertEqual(prepared.argv[:2],[sys.executable,'-I']);self.assertEqual(prepared.argv[-3:],argv)
        self.assertEqual(json.loads(prepared.argv[3])['windows_job'],job.name)
        events=[];process=SimpleNamespace(kill=lambda:events.append('kill'),pid=12345)
        prepared.bind(process)
        with patch.object(shell.sys,'platform','win32'):shell._kill(process)
        prepared.close();self.assertEqual([call for call in kernel.calls if call[0]=='close'],[('close',41)])
        self.assertEqual(events,['kill'])

    def test_windows_description_and_resource_off_keeps_tree_cleanup(self):
        values={'windows_job':True,'resource_limits':False}
        ctx=SimpleNamespace(settings=SimpleNamespace(get=lambda group,key,default=None:values.get(key,default)))
        with patch.object(sandbox.sys,'platform','win32'),patch.object(sandbox,'detect',return_value={'kind':'none','fs':False,'net':False,'title':'Изоляции нет.'}):
            info=sandbox.for_app(ctx)
        self.assertTrue(info['windows_job']);self.assertEqual(info['limits'],{})
        self.assertIn('Численные лимиты выключены',info['title'])
        values['resource_limits']=True
        with patch.object(sandbox.sys,'platform','win32'),patch.object(sandbox,'detect',return_value={'kind':'none','fs':False,'net':False,'title':'Изоляции нет.'}):info=sandbox.for_app(ctx)
        self.assertNotIn('RLIMIT_FSIZE',info['limits']);self.assertIn('суммарно',info['title'])

    @async_test
    async def test_official_cli_probe_closes_job_on_completion_and_honors_switch(self):
        kernel=Kernel();job=win.Job(api=win.Api(kernel));output=asyncio.StreamReader();output.feed_data(b'--help\n');output.feed_eof()
        process=SimpleNamespace(stdout=output,returncode=0,wait=AsyncMock(return_value=0))
        with patch.object(cli_common.sys,'platform','win32'),patch.object(win,'wrap',return_value=(job,['wrapper','program.exe'])) as wrapper,patch.object(asyncio,'create_subprocess_exec',new=AsyncMock(return_value=process)) as spawn:
            code,text=await cli_common.probe(['program.exe'],protection={'enabled':True,'limits':{}})
        self.assertEqual(code,0);self.assertIn('--help',text);self.assertIs(process._bender_job,job)
        self.assertEqual(spawn.call_args.args,('wrapper','program.exe'));self.assertIn(('close',41),kernel.calls)
        with patch.object(cli_common.sys,'platform','win32'),patch.object(win,'wrap') as wrapper,patch.object(asyncio,'create_subprocess_exec',new=AsyncMock(return_value=SimpleNamespace())):
            await cli_common._spawn(['program.exe'],{'enabled':False})
        wrapper.assert_not_called()
