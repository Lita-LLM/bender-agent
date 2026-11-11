"""Внутри namespace: TCP-вход для HTTP_PROXY пересылает байты в Unix-сокет.

Это транспорт, не граница доверия: прямую сеть закрывает ОС, допуск — в Бендере.
"""
import os
import selectors
import socket
import socketserver
import subprocess
import sys
import threading


class Relay(socketserver.BaseRequestHandler):
    def handle(self):
        with socket.socket(socket.AF_UNIX) as remote:
            remote.connect('/proxy.sock')
            with selectors.DefaultSelector() as selector:
                selector.register(self.request, selectors.EVENT_READ, remote)
                selector.register(remote, selectors.EVENT_READ, self.request)
                while True:
                    events = selector.select(60)
                    if not events:
                        return
                    for key, _ in events:
                        data = key.fileobj.recv(65536)
                        if not data:
                            return
                        key.data.sendall(data)


class Bridge(socketserver.ThreadingTCPServer):
    daemon_threads = True
    request_queue_size = 8

    def handle_error(self, request, client_address):
        pass


with Bridge(('127.0.0.1', 0), Relay) as server:
    port = server.server_address[1]
    proxy = 'http://127.0.0.1:'+str(port)
    environment = dict(os.environ)
    for name in ('HTTP_PROXY','HTTPS_PROXY','http_proxy','https_proxy'):
        environment[name] = proxy
    environment['NO_PROXY'] = environment['no_proxy'] = ''
    threading.Thread(target=server.serve_forever, kwargs={'poll_interval':60}, daemon=True).start()
    # Родительская группа процесса гасит также дочерний код и мост.
    raise SystemExit(subprocess.call([sys.executable, *sys.argv[1:]], env=environment))
