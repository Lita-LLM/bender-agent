"""Фиксированное зерно, ограниченные случаи и таймаут: мусор не выходит за границу разборщика."""
import asyncio
from contextlib import contextmanager
import gc
import json
import random
import signal
import sys
import time
import tracemalloc
from pathlib import Path
from bender.core import json5
from bender.core.errors import HumanError
from bender.skills.format import parse as skill_parse
from bender.channels.mail import parse as mail_parse
from bender.net.search import DuckResults
from bender.net.http_server import read_request,BadRequest
from bender.net.sse import parse_sse
from bender.router.textproto import decode
from bender.router.cli_common import stream
from bender.mcp.client import Client
from bender.core.app import App
from tests.helpers import TempDirCase,async_test


def cases(seed=734,count=240):
    rng=random.Random(seed)
    source=['', '{}','[]','null','"x"','{name:"demo",description:"пример"}',
            '---\nname: demo\ndescription: пример\n---\nТекст','From: a@b.c\nSubject: x\n\nтекст',
            '<div class><a href="http://[">текст</a></div>','<tool_call>{"name":"read_file","arguments":{}}</tool_call>']
    for text in source:yield text
    alphabet='{}[]:,-\'"<>/\\\x00\r\n abcdef0123АБВ🤖'
    for _ in range(count):
        text=rng.choice(source)
        for _ in range(rng.randrange(1,12)):
            at=rng.randrange(len(text)+1)
            text=text[:at]+rng.choice(alphabet)+text[at+rng.randrange(2):]
        yield text


@contextmanager
def budget(seconds=.25):
    if hasattr(signal,'setitimer'):
        def expired(*args):raise TimeoutError('Разборщик не завершил случай')
        old=signal.signal(signal.SIGALRM,expired);signal.setitimer(signal.ITIMER_REAL,seconds)
        try:yield
        finally:signal.setitimer(signal.ITIMER_REAL,0);signal.signal(signal.SIGALRM,old)
    else:
        start=time.monotonic();yield
        if time.monotonic()-start>seconds:raise TimeoutError('Разборщик превысил время')


class FuzzTest(TempDirCase):
    def test_json5_yaml_text_protocol(self):
        for text in cases():
            for parse,allowed in ((json5.loads,(json5.JSON5Error,)),(skill_parse,(HumanError,)),(decode,(HumanError,))):
                with budget():
                    try:parse(text)
                    except allowed:pass
        for text in ('['*2000+'0'+']'*2000, '"\\uD800\\uGGGG"', '"\\xZZ"', '"\\x"'):
            with budget(),self.assertRaises(json5.JSON5Error):json5.loads(text)
        with budget(),self.assertRaises(HumanError):skill_parse('---\nname: demo\ndescription: x\na: '+('9'*5000)+'\n---\nтекст')
        with budget():decode('<tool_call>'+('['*2000+'0'+']'*2000)+'</tool_call>')

    def test_mail_and_duck_html(self):
        for text in cases(735):
            with budget():
                try:
                    mail_parse(text.encode('utf-8'))
                    mail_parse(b'Content-Type: text/html; charset=utf-8\r\n\r\n'+text.encode('utf-8'))
                except HumanError:pass
            with budget():
                parser=DuckResults()
                try:
                    parser.feed(text);parser.close()
                    if parser.item:parser.finish()
                except HumanError:pass
        letter=b'Content-Type: text/plain; charset=missing-charset\r\n\r\nhello'
        self.assertIn('hello',mail_parse(letter)['body'])

    @async_test
    async def test_http_requests_and_sse(self):
        seed=[b'GET / HTTP/1.1\r\n\r\n',b'POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n-1\r\n',
              b'POST / HTTP/1.1\r\nContent-Length: 5\r\n\r\nx',b'GET http://[ HTTP/1.1\r\n\r\n',
              b'POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n1\r\nxZZ0\r\n\r\n',
              b'POST / HTTP/1.1\r\nContent-Length: 1\r\nContent-Length: 9\r\n\r\nx',
              b'POST / HTTP/1.1\r\nTransfer-Encoding: chunked\r\nContent-Length: 1\r\n\r\n0\r\n\r\n']
        rng=random.Random(736)
        for number in range(250):
            raw=rng.choice(seed)
            if number>=len(seed):
                at=rng.randrange(len(raw));raw=raw[:at]+bytes([rng.randrange(256)])+raw[at+1:]
            else:raw=seed[number]
            reader=asyncio.StreamReader(limit=16384);reader.feed_data(raw);reader.feed_eof()
            try:await asyncio.wait_for(read_request(reader,None,max_body=1024),.1)
            except (BadRequest,ConnectionResetError):pass
        for raw in seed[1:]:
            reader=asyncio.StreamReader();reader.feed_data(raw);reader.feed_eof()
            with self.assertRaises((BadRequest,ConnectionResetError)):await asyncio.wait_for(read_request(reader,None,max_body=1024),.1)
        async def lines(text):
            for line in text.splitlines():yield line
        for text in cases(737):
            try:await asyncio.wait_for(collect_sse(lines(text)),.1)
            except ValueError:pass
        async def endless_event():
            for _ in range(5000):yield 'data: '
        with self.assertRaises(ValueError):await asyncio.wait_for(collect_sse(endless_event()),.2)

    def test_module_manifests(self):
        app=App(self.tmp/'data');root=app.dirs.work/'package';root.mkdir()
        try:
            for text in cases(738):
                (root/'bender-module.json').write_text(text)
                try:
                    with budget():app.modules.package_at(root,full=True)
                except HumanError:pass
            (root/'bender-module.json').write_text('['*2000+'0'+']'*2000)
            with budget(),self.assertRaises(HumanError):app.modules.package_at(root,full=True)
        finally:app.store.close()

    @async_test
    async def test_mcp_messages_and_cli_jsonl(self):
        class Transport:
            value=None
            async def exchange(self,msg,progress):return {'jsonrpc':'2.0','id':msg['id'],'result':self.value}
            async def close(self):pass
        transport=Transport();client=Client(transport,timeout=.1)
        for text in cases(739):
            try:transport.value=json.loads(text)
            except ValueError:transport.value=text
            try:await asyncio.wait_for(client.request('tools/list'),.1)
            except HumanError:pass
        payload='\n'.join(cases(740)).encode()
        path=self.tmp/'cli.py';path.write_text('import sys\nsys.stdout.buffer.write('+repr(payload)+')\n')
        events=[]
        await stream([sys.executable,str(path)],prompt='',cwd=self.tmp,env={},timeout=2,on_event=events.append)
        self.assertTrue(all(isinstance(event,dict) for event in events))
        path.write_text('print('+repr('['*2000+'0'+']'*2000)+')\n')
        await stream([sys.executable,str(path)],prompt='',cwd=self.tmp,env={},timeout=2,on_event=events.append)

    def test_repeated_garbage_does_not_accumulate(self):
        corpus=list(cases(741,80))
        def batch():
            for text in corpus:
                try:json5.loads(text)
                except ValueError:pass
                try:skill_parse(text)
                except HumanError:pass
                decode(text)
        batch();gc.collect();tracemalloc.start()
        try:
            before=tracemalloc.get_traced_memory()[0]
            for _ in range(15):batch()
            gc.collect();after=tracemalloc.get_traced_memory()[0]
            self.assertLess(after-before,512*1024)
        finally:tracemalloc.stop()


async def collect_sse(lines):return [event async for event in parse_sse(lines)]


class CliEventFuzzTest(TempDirCase):
    @async_test
    async def test_malformed_nested_cli_events_fall_back(self):
        from tests.test_mind import MindEnv
        for event in ({'type':'stream_event','event':['bad']},{'type':'result','result':'ok','usage':['bad']},{'type':'stream_event','event':{'type':'content_block_delta','delta':{'type':'text_delta','text':['bad']}}}):
            async with MindEnv(self.tmp) as env:
                path=self.tmp/'fuzz_cli.py'
                path.write_text('import sys,json\nif "--help" in sys.argv:print("--tools --strict-mcp-config --output-format")\nelse:print('+repr(json.dumps(event))+')\n')
                provider=env.app.models.add_provider(preset='claude_code',extra={'command':[sys.executable,str(path)]})
                fallback=env.app.models.roles()['main'][0]['provider']
                env.app.models.set_role('main',[{'provider':provider['id'],'model':'default'},{'provider':fallback,'model':'echo'}])
                result=await env.app.router.call([{'role':'user','content':'проверка'}])
                self.assertIn('Эхо',result.text)
                self.assertTrue(result.notice)
