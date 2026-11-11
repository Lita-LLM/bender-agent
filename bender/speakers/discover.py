"""Поиск колонок в домашней сети по mDNS (_esphomelib._tcp): устройства ESPHome, в том числе Pulsar.

Только стандартная библиотека: один запрос в группу 224.0.0.251, ответы читаются пару секунд.
Превью: ничего не находит, если сеть режет multicast (гостевой Wi-Fi, часть роутеров) — тогда адрес вводится вручную."""
from __future__ import annotations

import socket
import struct
import time

SERVICE = '_esphomelib._tcp.local'
GROUP = ('224.0.0.251', 5353)


def encode(name: str) -> bytes:
    return b''.join(bytes([len(p)]) + p.encode() for p in name.strip('.').split('.')) + b'\0'


def query(name: str = SERVICE) -> bytes:
    # QU-бит (0x8000) просит ответить сразу нам, а не в группу
    return struct.pack('>HHHHHH', 0, 0, 1, 0, 0, 0) + encode(name) + struct.pack('>HH', 12, 0x8001)


def read_name(data: bytes, pos: int, depth: int = 0):
    parts = []
    jumped_end = None
    while True:
        if pos >= len(data) or depth > 12:
            raise ValueError('обрыв имени')
        length = data[pos]
        if length == 0:
            pos += 1
            break
        if length & 0xC0 == 0xC0:
            if jumped_end is None:
                jumped_end = pos + 2
            pos = ((length & 0x3F) << 8) | data[pos + 1]
            depth += 1
            continue
        parts.append(data[pos + 1:pos + 1 + length].decode('utf-8', 'replace'))
        pos += 1 + length
    return '.'.join(parts), (jumped_end if jumped_end is not None else pos)


def parse(data: bytes) -> dict:
    """Из пакета mDNS — {'ptr': [имена экземпляров], 'srv': {имя: (порт, хост)}, 'a': {хост: ip}}."""
    out = {'ptr': [], 'srv': {}, 'a': {}}
    try:
        _, _, questions, answers, authority, extra = struct.unpack('>HHHHHH', data[:12])
        pos = 12
        for _ in range(questions):
            _, pos = read_name(data, pos)
            pos += 4
        for _ in range(answers + authority + extra):
            name, pos = read_name(data, pos)
            kind, _, _, size = struct.unpack('>HHIH', data[pos:pos + 10])
            pos += 10
            body = data[pos:pos + size]
            if kind == 12:
                out['ptr'].append(read_name(data, pos)[0])
            elif kind == 33 and size >= 6:
                port = struct.unpack('>H', body[4:6])[0]
                out['srv'][name] = (port, read_name(data, pos + 6)[0])
            elif kind == 1 and size == 4:
                out['a'][name] = socket.inet_ntoa(body)
            pos += size
    except (ValueError, struct.error, IndexError):
        pass
    return out


def find(seconds: float = 2.0) -> list[dict]:
    """Колонки, ответившие за seconds: [{'name', 'host', 'port'}]."""
    ptr, srv, addr = set(), {}, {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 255)
        sock.settimeout(0.3)
        sock.sendto(query(), GROUP)
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                data, _ = sock.recvfrom(9000)
            except socket.timeout:
                continue
            except OSError:
                break
            got = parse(data)
            ptr.update(got['ptr'])
            srv.update(got['srv'])
            addr.update(got['a'])
    except OSError:
        return []
    finally:
        sock.close()
    found = []
    for instance in sorted(ptr):
        if not instance.endswith(SERVICE) or instance not in srv:
            continue
        port, target = srv[instance]
        host = addr.get(target)
        if host:
            found.append({'name': instance[:-len(SERVICE) - 1], 'host': host, 'port': port})
    return found
