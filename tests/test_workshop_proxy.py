"""Посредник: DNS/IP, CONNECT, редирект и настоящий namespace MCP-тула."""
import asyncio
import json
from pathlib import Path
import socket
from unittest.mock import patch

from bender.core.app import App
from bender.core.errors import HumanError
from bender.guard.sandbox import detect
from bender.mcp.manager import tool_name
from bender.tools.registry import ToolContext
from bender.workshop.proxy import Proxy, hosts, public_ip
from bender.workshop.tools import approve, definitions, propose
from tests.helpers import TempDirCase, async_test
from tests.test_workshop_tools import CODE, TESTS

PUBLIC = '93.184.216.34'


class ProxyTest(TempDirCase):
    def test_address_classes_and_names(self):
        for value in ('127.0.0.1', '127.8.1.2', '::1', '10.0.0.1', '172.16.1.1', '192.168.1.1',
                      '169.254.169.254', '100.100.100.200', '0.0.0.0', '224.0.0.1', 'fc00::1', 'fe80::1',
                      '::ffff:127.0.0.1', '2002:7f00:0001::', 'not-ip'):
            self.assertFalse(public_ip(value), value)
        self.assertTrue(public_ip(PUBLIC))
        for value in ('localhost', 'metadata.google.internal', 'name.local', '*.example.org', 'example.org:443',
                      'http://example.org', 'example.org.', '127.0.0.1', '::1', 'a@b.example.org', '-bad.example.org'):
            with self.assertRaises(HumanError, msg=value): hosts([value])
        self.assertEqual(hosts(['EXAMPLE.org']), ['example.org'])

    @async_test
    async def test_http_connect_dns_pin_and_rebinding(self):
        app = App(self.tmp/'data')
        app.settings.set('workshop', {'network_proxy': True})
        observed, dialed = [], []
        async def backend(reader, writer):
            raw = await reader.readuntil(b'\r\n\r\n')
            observed.append(raw)
            writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK')
            await writer.drain(); writer.close()
        server = await asyncio.start_server(backend, '127.0.0.1', 0)
        destination = server.sockets[0].getsockname()[1]
        address = PUBLIC
        async def dns(host, *_args, **_kw):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, '', (address, 0))]
        async def dial(ip, port):
            dialed.append((ip,port))
            return await asyncio.open_connection('127.0.0.1', destination)
        proxy = Proxy(app, self.tmp/'proxy.sock', ['example.org'])
        async def request(data):
            reader, writer = await asyncio.open_unix_connection(str(proxy.path))
            writer.write(data);await writer.drain()
            result = await reader.read();writer.close();await writer.wait_closed()
            return result
        try:
            with patch.object(asyncio.get_running_loop(), 'getaddrinfo', side_effect=dns), patch.object(proxy, 'dial', side_effect=dial):
                await proxy.start()
                response = await request(b'GET http://example.org/hello HTTP/1.1\r\nHost: localhost\r\n\r\n')
                self.assertIn(b'200 OK', response)
                self.assertEqual(dialed, [(PUBLIC,80)])
                self.assertIn(b'Host: example.org', observed[0])
                reader, writer = await asyncio.open_unix_connection(str(proxy.path))
                writer.write(b'CONNECT example.org:443 HTTP/1.1\r\nHost: example.org\r\n\r\n');await writer.drain()
                self.assertIn(b'200 Connection Established', await reader.readuntil(b'\r\n\r\n'))
                writer.write(b'GET /tunnel HTTP/1.1\r\nHost: example.org\r\n\r\n');await writer.drain()
                self.assertIn(b'200 OK', await reader.read())
                writer.close();await writer.wait_closed()
                for url in (b'http://localhost/', b'http://127.0.0.1/', b'http://10.0.0.1/', b'http://other.org/'):
                    self.assertIn(b'403 Forbidden', await request(b'GET '+url+b' HTTP/1.1\r\n\r\n'))
                before = len(dialed)
                address = '127.0.0.1'
                self.assertIn(b'403 Forbidden', await request(b'GET http://example.org/ HTTP/1.1\r\n\r\n'))
                self.assertEqual(len(dialed), before)
                # Смесь публичного и частного DNS тоже не допускается.
                with patch.object(asyncio.get_running_loop(), 'getaddrinfo', return_value=[(2,1,6,'',(PUBLIC,0)), (2,1,6,'',('10.1.1.1',0))]):
                    self.assertIn(b'403 Forbidden', await request(b'GET http://example.org/ HTTP/1.1\r\n\r\n'))
                address = PUBLIC
                for raw in (b'POST http://example.org/ HTTP/1.1\r\n\r\n',
                            b'GET http://example.org/ HTTP/1.1\r\nContent-Length: 9\r\n\r\n',
                            b'GET http://example.org/ HTTP/1.1\r\nTransfer-Encoding: chunked\r\n\r\n',
                            b'CONNECT example.org:22 HTTP/1.1\r\n\r\n'):
                    self.assertIn(b'403 Forbidden', await request(raw))
                before = len(dialed)
                app.guard.add_rule('net.read', '*', 'deny', only_clean=False)
                self.assertIn(b'403 Forbidden', await request(b'GET http://example.org/ HTTP/1.1\r\n\r\n'))
                self.assertEqual(len(dialed), before)
            self.assertTrue(app.journal.verify()['ok'])
            self.assertTrue(any(e.get('data',{}).get('address') == PUBLIC for e in app.journal.recent(100)))
        finally:
            proxy.close();server.close();await server.wait_closed();await app.close()

    @async_test
    async def test_real_namespace_tool_redirect_and_disable(self):
        if detect()['kind'] != 'bwrap':
            self.skipTest('Нужен bubblewrap; настоящий прогон также на хосте')
        app = App(self.tmp/'data')
        app.bus.bind_loop(asyncio.get_running_loop())
        app.settings.set('workshop', {'network_proxy': True})
        app.settings.set('guard', {'process_count':1024})
        def accept(event):
            if event.get('type') == 'approval':
                asyncio.get_running_loop().call_soon(app.guard.respond, event['card']['id'], 'once')
        app.bus.listen(accept)
        received = []
        async def backend(reader, writer):
            raw = await reader.readuntil(b'\r\n\r\n');received.append(raw)
            if b' /redirect ' in raw:
                writer.write(b'HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/secret\r\nContent-Length: 0\r\nConnection: close\r\n\r\n')
            else:
                writer.write(b'HTTP/1.1 200 OK\r\nContent-Length: 7\r\nConnection: close\r\n\r\nPUBLIC!')
            await writer.drain();writer.close()
        server = await asyncio.start_server(backend, '127.0.0.1', 0)
        destination = server.sockets[0].getsockname()[1]
        address = PUBLIC
        async def dns(host, *_args, **_kw):
            return [(2,1,6,'',(address,0))]
        dialed = []
        async def dial(_self, ip, port):
            dialed.append(ip)
            return await asyncio.open_connection('127.0.0.1', destination)
        helper = '''
def fetch(arguments):
 import os, socket, urllib.request
 try:
  response=urllib.request.urlopen(arguments.get('url','http://example.org/'),timeout=4).read().decode()
 except Exception as exc:
  response=str(exc)
 direct=False
 try:
  with socket.create_connection(('127.0.0.1', arguments['host_port']),timeout=.2):direct=True
 except OSError:pass
 external=False
 try:
  with socket.create_connection(('93.184.216.34',80),timeout=.2):external=True
 except OSError:pass
 return {'response':response,'direct':direct,'external':external,'proxy':bool(os.environ.get('HTTPS_PROXY'))}

'''
        code = CODE.replace('def main():', helper+'def main():').replace('observe() if params["name"] == "inspect"', 'fetch(params["arguments"]) if params["name"] == "inspect"')
        try:
            record = await propose(app.workshop, ToolContext(app), 'network', 'Домен через посредник', code, TESTS, ['exec','net.read'], ['example.org'])
            self.assertEqual(record['status'],'waiting',record)
            await approve(app.workshop,record['id'],record['hash'],ToolContext(app,actor='owner'))
            record=app.workshop.get(record['id']);sid=record['server']
            raw=app.mcp.get(sid)
            self.assertIs(raw['enabled'],False)
            self.assertEqual(raw['workshop_runtime'],'bwrap-v2')
            self.assertFalse(app.modules.allowed_server({**raw,'workshop_runtime':'bwrap-v1'}))
            with patch.object(asyncio.get_running_loop(),'getaddrinfo',side_effect=dns), patch.object(Proxy,'dial',dial):
                state=await definitions(app.workshop,record['id'],ToolContext(app,actor='owner'))
                await definitions(app.workshop,record['id'],ToolContext(app,actor='owner'),next(s['review'] for s in state['servers'] if s['id']==sid))
                async def call(url):
                    result=await app.tools.run(tool_name(sid,'inspect'),{'url':url,'host_port':destination},ToolContext(app,session_id='proxy-test'))
                    self.assertTrue(result.ok,result.content)
                    return result.content
                result=await call('http://example.org/')
                self.assertIn('PUBLIC!',result)
                self.assertIn('"direct": false',result)
                self.assertIn('"external": false',result)
                self.assertIn('"proxy": true',result)
                for url in ('http://example.org/redirect','http://127.0.0.1/','http://10.0.0.1/'):
                    self.assertIn('403',await call(url))
                count=len(dialed);address='169.254.169.254'
                self.assertIn('403',await call('http://example.org/'))
                self.assertEqual(len(dialed),count)
                app.settings.set('workshop',{'network_proxy':False})
                self.assertFalse(app.mcp.active(raw))
                self.assertFalse(app.tools.enabled(tool_name(sid,'inspect')))
                await asyncio.sleep(0)
                self.assertNotIn(sid,app.mcp.clients)
            self.assertFalse(any(b'/secret ' in item for item in received))
            with self.assertRaises(HumanError):
                app.settings.set('workshop',{'network_proxy':True},by='agent')
        finally:
            server.close();await server.wait_closed();await app.close()
