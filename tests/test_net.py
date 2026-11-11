import asyncio
import gzip
import unittest

from bender.net.http_client import HttpClient, NetError, Timeouts, _no_proxy_match
from bender.net.http_server import HttpServer, JSONResponse, Response, Router, StreamResponse, TextResponse, sse
from bender.net.sse import parse_sse
from tests.helpers import async_test


def make_router():
    r = Router()

    @r.route("GET", "/hello")
    async def hello(req):
        return TextResponse("привет " + (req.q("name") or "мир"))

    @r.route("POST", "/echo/{thing}")
    async def echo(req):
        return JSONResponse({"thing": req.params["thing"], "body": req.json(), "ua": req.headers.get("user-agent")})

    @r.route("GET", "/gzip")
    async def gz(req):
        return Response(gzip.compress("сжатый текст".encode()), headers={"Content-Encoding": "gzip"}, content_type="text/plain; charset=utf-8")

    @r.route("GET", "/stream")
    async def stream(req):
        async def gen():
            for i in range(3):
                yield sse({"n": i}, event="tick")
                await asyncio.sleep(0.01)
            yield sse("[DONE]")
        return StreamResponse(gen())

    @r.route("GET", "/slow")
    async def slow(req):
        await asyncio.sleep(2)
        return TextResponse("поздно")

    @r.route("GET", "/files/{path:rest}")
    async def files(req):
        return TextResponse(req.params["path"])

    return r


class NetTest(unittest.TestCase):
    @async_test
    async def test_stop_closes_new_connections_before_header_and_restart(self):
        server=HttpServer(make_router())
        for _ in range(3):
            await server.start('127.0.0.1',0)
            reader,writer=await asyncio.open_connection('127.0.0.1',server.port)
            await server.stop()
            self.assertEqual(await asyncio.wait_for(reader.read(),1),b'')
            self.assertFalse(server._conns)
            self.assertFalse(server._writers)
            writer.close();await writer.wait_closed()

    @async_test
    async def test_roundtrip_keepalive_stream_gzip(self):
        server = HttpServer(make_router())
        await server.start("127.0.0.1", 0)
        base = f"http://127.0.0.1:{server.port}"
        client = HttpClient()
        try:
            r = await client.get(base + "/hello?name=Бендер")
            self.assertEqual(r.status, 200)
            self.assertEqual(await r.text(), "привет Бендер")
            # второй запрос идёт по тому же соединению
            r2 = await client.post(base + "/echo/x%20y", json={"a": [1, "б"]})
            data = await r2.json()
            self.assertEqual(data["thing"], "x y")
            self.assertEqual(data["body"], {"a": [1, "б"]})
            self.assertIn("Bender", data["ua"])
            self.assertEqual(len(client._pool[next(iter(client._pool))]), 1)
            r3 = await client.get(base + "/gzip")
            self.assertEqual(await r3.text(), "сжатый текст")
            r4 = await client.get(base + "/stream")
            events = [e async for e in parse_sse(r4.iter_lines())]
            self.assertEqual([e.event for e in events], ["tick", "tick", "tick", "message"])
            self.assertEqual(events[-1].data, "[DONE]")
            r5 = await client.get(base + "/files/a/b/c.txt")
            self.assertEqual(await r5.text(), "a/b/c.txt")
            r6 = await client.get(base + "/nope")
            self.assertEqual(r6.status, 404)
            await r6.read()
            r7 = await client.request("DELETE", base + "/hello")
            self.assertEqual(r7.status, 405)
            await r7.read()
        finally:
            await client.close()
            await server.stop()

    @async_test
    async def test_timeout_and_refused(self):
        server = HttpServer(make_router())
        await server.start("127.0.0.1", 0)
        client = HttpClient(timeouts=Timeouts(connect=2, read=0.3))
        try:
            with self.assertRaises(NetError) as cm:
                await client.get(f"http://127.0.0.1:{server.port}/slow")
            self.assertEqual(cm.exception.kind, "timeout")
        finally:
            await client.close()
            await server.stop()
        with self.assertRaises(NetError) as cm:
            await HttpClient(timeouts=Timeouts(connect=2)).get(f"http://127.0.0.1:{server.port}/hello")
        self.assertEqual(cm.exception.kind, "connect")

    @async_test
    async def test_reset_while_connecting_is_retried(self):
        """Сброс при соединении (как у api.telegram.org в плохой сети) — разовый: запрос ещё не ушёл, повторяем."""
        from unittest.mock import patch
        server = HttpServer(make_router())
        await server.start("127.0.0.1", 0)
        real = asyncio.open_connection
        calls = []

        async def flaky(*args, **kw):
            calls.append(1)
            if len(calls) == 1:
                raise ConnectionResetError(104, "Connection reset by peer")
            return await real(*args, **kw)

        client = HttpClient(timeouts=Timeouts(connect=2))
        try:
            with patch("bender.net.http_client.CONNECT_RETRIES", (0, 0)), patch("asyncio.open_connection", flaky):
                async with await client.get(f"http://127.0.0.1:{server.port}/hello") as resp:
                    self.assertEqual(resp.status, 200)
                self.assertEqual(len(calls), 2)
                calls.clear()

                async def always(*args, **kw):
                    calls.append(1)
                    raise ConnectionResetError(104, "Connection reset by peer")
                with patch("asyncio.open_connection", always), self.assertRaises(NetError) as cm:
                    await HttpClient(timeouts=Timeouts(connect=2)).get(f"http://127.0.0.1:{server.port}/hello")
                self.assertEqual((cm.exception.kind, len(calls)), ("connect", 3))
                self.assertIn("Connection reset by peer", str(cm.exception))
        finally:
            await client.close()
            await server.stop()

    @async_test
    async def test_bad_request(self):
        server = HttpServer(make_router())
        await server.start("127.0.0.1", 0)
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
            writer.write(b"GARBAGE\r\n\r\n")
            await writer.drain()
            data = await reader.read()
            self.assertTrue(data.startswith(b"HTTP/1.1 400"))
            writer.close()
        finally:
            await server.stop()

    def test_no_proxy(self):
        rules = "localhost,127.0.0.0/8,.example.com,*.svc.local,api.anthropic.com"
        self.assertTrue(_no_proxy_match("localhost", rules))
        self.assertTrue(_no_proxy_match("127.0.0.5", rules))
        self.assertTrue(_no_proxy_match("a.example.com", rules))
        self.assertTrue(_no_proxy_match("x.svc.local", rules))
        self.assertTrue(_no_proxy_match("api.anthropic.com", rules))
        self.assertFalse(_no_proxy_match("openrouter.ai", rules))
        self.assertFalse(_no_proxy_match("10.0.0.1", rules))


if __name__ == "__main__":
    unittest.main()
