"""来源样本和本地HTTP服务的回归检查。"""
import hashlib
import json
import unittest
from urllib.request import urlopen
from source_site.source import Source
from source_site.__main__ import running


class SourceTests(unittest.TestCase):
    def test_source_1(self):
        s = Source()
        self.assertEqual(3, len(s.request("GET", "/index")[2]["pages"]))

    def test_source_2(self):
        s = Source()
        self.assertIn(b"2026", s.request("GET", "/page/new")[2])

    def test_source_3(self):
        s = Source()
        self.assertIn("约三十".encode(), s.request("GET", "/page/mixed")[2])

    def test_source_4(self):
        s = Source()
        self.assertEqual(404, s.request("GET", "/page/absent")[0])

    def test_http_source(self):
        with running() as (base, source):
            with urlopen(base + "/index", timeout=3) as response:
                self.assertEqual(200, response.status)
                self.assertTrue(response.read())


if __name__ == "__main__":
    unittest.main()
