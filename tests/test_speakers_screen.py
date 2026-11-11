"""Экран колонок (превью): поиск по mDNS и API без правки JSON."""
import json
import socket
import struct
from unittest import mock

from bender.net.http_server import HttpServer
from bender.speakers import discover
from tests.helpers import TempDirCase, async_test, owner_headers, owner_web
from tests.test_mind import MindEnv


def record(name, kind, body):
    return discover.encode(name) + struct.pack('>HHIH', kind, 1, 120, len(body)) + body


class DiscoverTest(TempDirCase):
    def test_parse_pointer_service_and_address_with_compression(self):
        instance = 'kitchen._esphomelib._tcp.local'
        packet = (struct.pack('>HHHHHH', 0, 0x8400, 0, 2, 0, 1) + record(discover.SERVICE, 12, discover.encode(instance))
                  + record(instance, 33, struct.pack('>HHH', 0, 0, 6053) + discover.encode('kitchen.local'))
                  + record('kitchen.local', 1, socket.inet_aton('192.0.2.40')))
        got = discover.parse(packet)
        self.assertEqual(got['ptr'], [instance])
        self.assertEqual(got['srv'][instance], (6053, 'kitchen.local'))
        self.assertEqual(got['a']['kitchen.local'], '192.0.2.40')
        self.assertEqual(discover.parse(b'\x00\x01garbage'), {'ptr': [], 'srv': {}, 'a': {}})  # мусор не роняет

    @async_test
    async def test_api_lists_discovers_and_marks_known(self):
        async with MindEnv(self.tmp) as env:
            app = env.app
            app.settings.set('speakers', {'stations': [{'name': 'Кухня', 'host': '192.0.2.40'}]}, by='owner')
            web = HttpServer(owner_web(app, allowed_hosts={'127.0.0.1', 'localhost'}))
            await web.start('127.0.0.1', 0)
            url = f'http://127.0.0.1:{web.port}'
            try:
                async def call(method, path, body=None):
                    response = await app.http.request(method, url + path, json=body, headers=owner_headers(**{'X-Bender': '1'}))
                    async with response:
                        return response.status, json.loads(await response.read())
                status, state = await call('GET', '/api/speakers')
                self.assertEqual(status, 200)
                self.assertEqual(state['stations'][0]['name'], 'Кухня')
                rows = [{'name': 'kitchen', 'host': '192.0.2.40', 'port': 6053}, {'name': 'hall', 'host': '192.0.2.41', 'port': 6053}]
                with mock.patch('bender.speakers.discover.find', return_value=rows):
                    status, found = await call('POST', '/api/speakers/discover', {})
                self.assertEqual(status, 200, found)
                self.assertEqual([row['known'] for row in found['found']], [True, False])
            finally:
                await web.stop()
