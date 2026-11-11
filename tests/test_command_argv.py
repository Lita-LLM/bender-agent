"""Настоящий запуск аргументов: пути Windows, метасимволы и проверка Предохранителем."""
import json
import os
import sys
from unittest.mock import patch

from bender.core.errors import HumanError
from bender.tools.registry import ToolContext
from bender.skills.manager import scan
from tests.helpers import TempDirCase, async_test
from tests.test_mind import MindEnv


class CommandArgvTest(TempDirCase):
    @async_test
    async def test_literal_arguments_and_guard_with_and_without_job(self):
        async with MindEnv(self.tmp) as env:
            app=env.app
            app.settings.set('guard',{'profile':'off'})
            args=[str(app.dirs.work/'папка с пробелом'/'repo'), 'a&b|c>file', '%PATH%', '$HOME', "a'b", 'a"b', 'trailing\\']
            ctx=ToolContext(app,actor='owner')
            for enabled in ([True,False] if os.name=='nt' else [True]):
                app.settings.set('guard',{'windows_job':enabled})
                argv=[sys.executable,'-c','import sys,json; print(json.dumps(sys.argv[1:]))',*args]
                result=await app.tools.run('run',{'command':'ignored','argv':argv},ctx)
                self.assertTrue(result.ok,result.content)
                actual=json.loads(result.content.split('\n',1)[1])
                self.assertEqual(actual,args)
            calls=[]
            async def deny(cap,target,*args,**kwargs):
                calls.append((cap,target))
                raise HumanError('test_denial','Запуск запрещён тестом')
            with patch.object(ctx,'check',deny):
                result=await app.tools.run('run',{'command':'echo harmless','argv':[sys.executable,'-c','print("actual-command")']},ctx)
            self.assertFalse(result.ok)
            self.assertIn('actual-command',calls[0][1])

    def test_package_scan_accepts_regular_files_but_rejects_hardlinks(self):
        root=self.tmp/'пакет с пробелом';root.mkdir()
        source=root/'SKILL.md';source.write_text('тест',encoding='utf-8')
        paths,_=scan(root,10,10000)
        self.assertIn('SKILL.md',paths)
        os.link(source,root/'copy.md')
        with self.assertRaises(HumanError):scan(root,10,10000)
