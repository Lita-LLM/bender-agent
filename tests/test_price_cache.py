"""Охота 16.11: стоимость Anthropic с кэшем промпта не обнуляется."""
import unittest

from bender.router.core import Switchboard
from bender.router.types import Usage


class PriceCacheTest(unittest.TestCase):
    prices = {"m": {"in": 3.0, "out": 15.0, "cache_read": 0.3, "cache_write": 3.75}}

    def test_anthropic_input_is_uncached_part(self):
        u = Usage(input=12, output=500, cache_read=120_000, cache_write=3_000)
        cost = Switchboard.price({"kind": "anthropic", "prices": self.prices}, "m", u)
        expected = (12 * 3.0 + 500 * 15.0 + 120_000 * 0.3 + 3_000 * 3.75) / 1e6
        self.assertAlmostEqual(cost, expected)
        self.assertGreater(cost, 0.05)

    def test_openai_cached_tokens_are_part_of_prompt(self):
        u = Usage(input=10_000, output=100, cache_read=8_000)
        cost = Switchboard.price({"kind": "openai", "prices": self.prices}, "m", u)
        self.assertAlmostEqual(cost, (2_000 * 3.0 + 100 * 15.0 + 8_000 * 0.3) / 1e6)


if __name__ == "__main__":
    unittest.main()
