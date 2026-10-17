from __future__ import annotations

import hashlib
import unittest
from unittest import mock

from bs4 import BeautifulSoup

from norway_company_agent import footprint_plus, website


class _FakeResponse:
    def __init__(self, body: bytes, url: str, content_type: str = "text/html; charset=utf-8") -> None:
        self._body = body
        self._url = url
        self.headers = {"content-type": content_type}

    def read(self, limit: int | None = None) -> bytes:
        return self._body if limit is None else self._body[:limit]

    def geturl(self) -> str:
        return self._url

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *args: object) -> bool:
        return False


class SlowSiteAndCachingTests(unittest.TestCase):
    def setUp(self) -> None:
        website.clear_website_caches()

    def tearDown(self) -> None:
        website.clear_website_caches()

    def test_robots_txt_cached_per_host(self) -> None:
        calls: list[str] = []

        def fake_open(req, timeout=None):
            calls.append(req.full_url)
            return _FakeResponse(b"User-agent: *\nAllow: /\n", req.full_url, "text/plain")

        with mock.patch.object(website, "assert_public_url", lambda u: None), \
             mock.patch.object(website.SAFE_OPENER, "open", fake_open):
            allowed1, req1 = website._robots_check("https://example.no/", 5.0)
            allowed2, req2 = website._robots_check("https://example.no/kontakt", 5.0)
            allowed3, req3 = website._robots_check("https://example.no/om-oss", 5.0)

        self.assertTrue(allowed1)
        self.assertTrue(allowed2)
        self.assertTrue(allowed3)
        self.assertEqual(req1, 1)
        self.assertEqual(req2, 0)
        self.assertEqual(req3, 0)
        self.assertEqual(calls, ["https://example.no/robots.txt"])

    def test_page_cache_prevents_redundant_fetches_and_preserves_sha256(self) -> None:
        calls: list[str] = []
        home_bytes = b"<html><head><title>Test AS</title></head><body><p>Velkommen til Test AS, Org.nr 985 589 003</p></body></html>"
        expected_sha = hashlib.sha256(home_bytes).hexdigest()

        def fake_open(req, timeout=None):
            calls.append(req.full_url)
            if req.full_url.endswith("/robots.txt"):
                return _FakeResponse(b"User-agent: *\nAllow: /\n", req.full_url, "text/plain")
            return _FakeResponse(home_bytes, "https://example.no/")

        with mock.patch.object(website, "assert_public_url", lambda u: None), \
             mock.patch.object(footprint_plus, "assert_public_url", lambda u: None), \
             mock.patch.object(website.SAFE_OPENER, "open", fake_open):
            rec, m = website.fetch_website("https://example.no/", timeout=8.0, crawl_secondary=False)
            self.assertEqual(rec["status"], "available")
            self.assertEqual(rec["content_sha256"], expected_sha)
            self.assertEqual(m["requests"], 2)  # 1 robots.txt + 1 homepage

            # Subsequent _get for homepage and robots.txt must hit cache with 0 network requests
            raw, final = footprint_plus._get("https://example.no/", timeout=8.0)
            self.assertEqual(raw, home_bytes)
            self.assertEqual(final, "https://example.no/")
            self.assertEqual(getattr(footprint_plus._NET, "last_requests", -1), 0)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), expected_sha)

            r_raw, _ = footprint_plus._get("https://example.no/robots.txt", timeout=5.0)
            self.assertEqual(r_raw, b"User-agent: *\nAllow: /\n")
            self.assertEqual(getattr(footprint_plus._NET, "last_requests", -1), 0)

        self.assertEqual(calls, ["https://example.no/robots.txt", "https://example.no/"])

    def test_priority_links_deduplicates_trailing_slashes(self) -> None:
        html = (
            '<a href="/about">About</a>'
            '<a href="/about/">About Us</a>'
            '<a href="/kontakt/">Kontakt</a>'
            '<a href="/kontakt">Kontakt oss</a>'
        )
        soup = BeautifulSoup(html, "lxml")
        links = website._priority_links("https://example.no/", soup)
        self.assertEqual(links, ["https://example.no/about", "https://example.no/kontakt/"])

    def test_discover_website_passes_timeout_and_crawl_secondary_false(self) -> None:
        profile = {"organisation_number": "985589003", "name": "TEST SELSKAP AS", "evidence": {}}
        seen_fetch_kwargs: dict[str, object] = {}
        seen_proof_args: tuple[object, ...] = ()

        def fake_fetch(host, **kwargs):
            seen_fetch_kwargs.update(kwargs)
            return {
                "status": "available",
                "source_url": "https://testselskap.no/",
                "value": {"title": "Test Selskap", "pages": []},
            }, {"requests": 2, "bytes": 100}

        def fake_proof(prof, rec, *args):
            nonlocal seen_proof_args
            seen_proof_args = args
            return "exact organisation number printed on https://testselskap.no/kontakt", {"requests": 1}

        with mock.patch.object(footprint_plus, "candidate_hosts", lambda p: [("testselskap.no", "name_slug_guess")]), \
             mock.patch.object(footprint_plus, "_resolves", lambda h: True), \
             mock.patch.object(footprint_plus, "fetch_website", fake_fetch), \
             mock.patch.object(footprint_plus, "apply_website_identity_gate", lambda p, r: {"assessment": {"score": 0.65, "publishable": False, "reasons": []}}), \
             mock.patch.object(footprint_plus, "hard_identity_proof", fake_proof):
            found, metrics = footprint_plus.discover_website(profile, timeout=12.0)

        self.assertIsNotNone(found)
        self.assertEqual(seen_fetch_kwargs.get("timeout"), 12.0)
        self.assertEqual(seen_fetch_kwargs.get("crawl_secondary"), False)
        self.assertEqual(seen_proof_args[-1], 12.0)
        self.assertEqual(metrics["requests"], 3)


if __name__ == "__main__":
    unittest.main()
