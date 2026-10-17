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


class FooterAndRegistryRecheckTests(unittest.TestCase):
    def setUp(self) -> None:
        website.clear_website_caches()

    def tearDown(self) -> None:
        website.clear_website_caches()

    def test_footer_identity_text_excerpt_and_body_fallback(self) -> None:
        from norway_company_agent.identity import apply_website_identity_gate

        html = (
            b"<html><head><title>Velkommen</title></head>"
            b"<body><div>Kort forside for selskapet.</div>"
            b"<footer>Nordisk Takst og Raadgivning AS | Org.nr. 985 589 003</footer></body></html>"
        )

        def fake_open(req, timeout=None):
            if req.full_url.endswith("/robots.txt"):
                return _FakeResponse(b"User-agent: *\nAllow: /\n", req.full_url, "text/plain")
            return _FakeResponse(html, "https://nordisktakst.no/")

        profile = {"organisation_number": "985589003", "name": "NORDISK TAKST OG RÅDGIVNING AS", "evidence": {}}
        with mock.patch.object(website, "assert_public_url", lambda u: None), \
             mock.patch.object(website.SAFE_OPENER, "open", fake_open):
            rec, _ = website.fetch_website("https://nordisktakst.no/", crawl_secondary=False)

        self.assertIn("985 589 003", rec["value"]["identity_text_excerpt"])
        self.assertTrue(len(rec["value"]["main_text_excerpt"]) > 20)
        gated = apply_website_identity_gate(profile, rec)
        self.assertTrue(gated["assessment"]["publishable"])
        self.assertEqual(gated["assessment"]["score"], 1.0)

    def test_standalone_org_number_check_rejects_concatenated_digits(self) -> None:
        from norway_company_agent.identity import assess_website_identity

        profile = {
            "organisation_number": "985589003",
            "name": "ANDRE SELSKAP AS",
            "evidence": {
                "website": {
                    "status": "available",
                    "source_url": "https://example.no/",
                    "value": {
                        "title": "Velkommen",
                        "main_text_excerpt": "Postboks 9855, 8900 Brønnøysund, avdeling 3",
                    },
                }
            },
        }
        assessment = assess_website_identity(profile)
        self.assertFalse(assessment["publishable"])
        self.assertNotEqual(assessment["score"], 1.0)

    def test_registry_listed_site_promoted_only_with_exact_org_number(self) -> None:
        profile = {"organisation_number": "985589003", "name": "BÆRUM EIENDOMSTAKSERING AS", "evidence": {}}
        site = {
            "status": "available",
            "source_url": "https://www.betakst.no/",
            "value": {
                "final_url": "https://www.betakst.no/",
                "title": "Forsiden - Bærum Eiendomstaksering",
                "identity_assessment": {"publishable": False, "score": 0.65, "reasons": ["partial legal-name overlap only"]},
                "social_link_assessments": [{"platform": "facebook", "url": "https://facebook.com/betakst", "publishable": True}],
                "social_links": [],
            },
        }
        pages = {
            "https://www.betakst.no/": '<a href="/kontakt">Kontakt</a><a href="https://facebook.com/betakst">FB</a>',
            "https://www.betakst.no/kontakt": 'Kontakt oss på <a href="mailto:post@betakst.no">post@betakst.no</a>. Org.nr 985 589 003',
        }
        with mock.patch.object(footprint_plus, "_get", lambda url, *a, **k: ((pages.get(url).encode() if pages.get(url) else None), url)):
            fp, _ = footprint_plus.extract_footprint(profile, site)

        self.assertTrue(site["value"]["identity_assessment"]["publishable"])
        self.assertEqual(site["value"]["identity_assessment"]["score"], 1.0)
        self.assertEqual(fp["status"], "available")
        self.assertEqual(fp["value"]["contact"]["email"], "post@betakst.no")

        # Address-only proof on an unproven registry-listed site must NOT promote it
        site_addr_only = {
            "status": "available",
            "source_url": "https://www.betakst.no/",
            "value": {
                "final_url": "https://www.betakst.no/",
                "title": "Forsiden",
                "identity_assessment": {"publishable": False, "score": 0.65, "reasons": []},
            },
        }
        with mock.patch.object(
            footprint_plus,
            "hard_identity_proof",
            lambda *a, **k: ("registered street address and postcode printed on https://www.betakst.no/kontakt", {"requests": 1}),
        ):
            fp2, _ = footprint_plus.extract_footprint(profile, site_addr_only)
        self.assertFalse(site_addr_only["value"]["identity_assessment"]["publishable"])
        self.assertEqual(fp2["status"], "not_found")


if __name__ == "__main__":
    unittest.main()
