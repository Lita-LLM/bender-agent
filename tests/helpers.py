"""Общие помощники тестов."""

from __future__ import annotations

import asyncio
import functools
import os
import tempfile
import unittest
from pathlib import Path

# в тестах никаких прокси из окружения: всё локально
for _k in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
    os.environ.pop(_k, None)
os.environ["BENDER_NO_ANIM"] = "1"

WEB_TOKEN = 'bender-test-owner-token-0123456789abcdef'


def owner_web(app, **kwargs):
    """Обычный защищённый сервер с известной тестовому хозяину кукой."""
    from bender.web.api import create_web
    return create_web(app, token=WEB_TOKEN, **kwargs)


def owner_headers(**extra):
    return {'Cookie':'bender_token='+WEB_TOKEN, **extra}


def run(coro):
    return asyncio.run(coro)


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(asyncio.wait_for(fn(*args, **kwargs), 60))

    return wrapper


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="bender-test-")
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()
