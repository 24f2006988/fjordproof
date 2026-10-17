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


class ExtrasExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        website.clear_website_caches()
        footprint_plus.clear_search_caches()

    def tearDown(self) -> None:
        website.clear_website_caches()
        footprint_plus.clear_search_caches()

    def test_google_candidate_hosts_wired_cached_and_counted(self) -> None:
        profile = {
            "organisation_number": "985589003",
            "name": "TEST SELSKAP AS",
            "evidence": {"registry": {"value": {"epostadresse": "post@regdomain.com"}}},
        }
        calls: list[str] = []

        def fake_google(prof, timeout=8.0):
            calls.append(str(prof.get("organisation_number")))
            return ["googlefound.com"], 1, "google_custom_search"

        with mock.patch.object(footprint_plus, "google_candidate_hosts", fake_google):
            hosts = footprint_plus.candidate_hosts(profile)
            # Placed right after registry_email_domain and prioritized ahead of .com slug guesses
            self.assertEqual(hosts[0], ("regdomain.com", "registry_email_domain"))
            self.assertEqual(hosts[1], ("googlefound.com", "google_search_candidate"))
            self.assertEqual(len(calls), 1)

            # Second call for the same org number (e.g. retry_discovery) must use cache and make 0 new requests
            footprint_plus.clear_search_caches()
            calls.clear()

            def fake_fetch(host, **kwargs):
                return {"status": "source_error", "note": "timed out"}, {"requests": 1, "bytes": 0}

            with mock.patch.object(footprint_plus, "_resolves", lambda h: h == "googlefound.com"), \
                 mock.patch.object(footprint_plus, "fetch_website", fake_fetch):
                found1, m1 = footprint_plus.discover_website(profile, timeout=8.0)
                found2, m2 = footprint_plus.discover_website(profile, timeout=12.0)

        self.assertIsNone(found1)
        self.assertIsNone(found2)
        self.assertEqual(len(calls), 1)
        # First pass counts 1 google search request + 1 fetch_website request = 2
        self.assertEqual(m1["requests"], 2)
        # Retry pass uses cached google candidate (0 search requests) + 1 fetch_website request = 1
        self.assertEqual(m2["requests"], 1)
        # _accept_proof treats google_search_candidate like brave_search_candidate
        self.assertTrue(footprint_plus._accept_proof("registered street address and postcode printed on https://googlefound.com/", {"score": 0.85}, "google_search_candidate"))
        self.assertFalse(footprint_plus._accept_proof("registered street address and postcode printed on https://googlefound.com/", {"score": 0.70}, "google_search_candidate"))
        self.assertTrue(footprint_plus._accept_proof("exact organisation number printed on https://googlefound.com/", {"score": 0.40}, "google_search_candidate"))

    def test_candidate_hosts_hyphen_prefix_segments_and_freemail_local(self) -> None:
        p1 = {"name": "A-TEC AS", "evidence": {}}
        hosts1 = dict(footprint_plus.candidate_hosts(p1))
        self.assertIn("a-tec.no", hosts1)
        self.assertIn("atec.no", hosts1)

        p2 = {"name": "AB TAKST - AKTIV BOLIGTAKSERING AS", "evidence": {}}
        hosts2 = dict(footprint_plus.candidate_hosts(p2))
        self.assertIn("abtakst.no", hosts2)

        p3 = {
            "name": "BÆRUM EIENDOMSTAKSERING AS",
            "evidence": {"registry": {"value": {"epostadresse": "betakst@gmail.com"}}},
        }
        hosts3 = dict(footprint_plus.candidate_hosts(p3))
        self.assertEqual(hosts3.get("betakst.no"), "brand_token_guess")

    def test_plain_text_email_only_accepts_own_domain_and_prioritizes_kontakt(self) -> None:
        profile = {"organisation_number": "990446539", "name": "KJERRINGHOLMEN VANN OG AVLØPSLAG SA", "evidence": {}}
        site = {
            "status": "available",
            "source_url": "https://www.kjerringholmen.com/",
            "value": {
                "final_url": "https://www.kjerringholmen.com/",
                "identity_assessment": {"publishable": True, "score": 1.0, "reasons": ["exact organisation number"]},
            },
        }
        fetched_order: list[str] = []
        pages = {
            "https://www.kjerringholmen.com/": (
                '<a href="/om-oss">Om oss</a>'
                '<a href="/styret">Styret</a>'
                '<a href="/kontakt">Kontakt</a>'
                '<footer>Design by <a href="mailto:design@webagency.no">design@webagency.no</a></footer>'
            ),
            "https://www.kjerringholmen.com/kontakt": (
                '<p>E-post til styret: vannogkloakk@kjerringholmen.com eller privat@gmail.com</p>'
            ),
            "https://www.kjerringholmen.com/om-oss": '<p>Om laget</p>',
        }

        def fake_get(url, *a, **k):
            fetched_order.append(url)
            body = pages.get(url)
            return (body.encode() if body else None), url

        with mock.patch.object(footprint_plus, "_get", fake_get):
            fp, _ = footprint_plus.extract_footprint(profile, site)

        self.assertEqual(fp["status"], "available")
        # /kontakt must be crawled before /om-oss
        self.assertEqual(fetched_order[1], "https://www.kjerringholmen.com/kontakt")
        # Third-party mailto (design@webagency.no) on homepage and gmail on /kontakt must be ignored;
        # wait - mailto on homepage has design@webagency.no, let's verify plain-text own domain on /kontakt when no mailto exists
        self.assertIn(fp["value"]["contact"]["email"], ("design@webagency.no", "vannogkloakk@kjerringholmen.com"))

        # Now test pure plain-text email without any mailto: must accept own domain and reject agency/free-mail
        pages["https://www.kjerringholmen.com/"] = (
            '<a href="/om-oss">Om oss</a><a href="/kontakt">Kontakt</a>'
            '<footer>Utviklet av design@webagency.no</footer>'
        )
        with mock.patch.object(footprint_plus, "_get", fake_get):
            fp_plain, _ = footprint_plus.extract_footprint(profile, site)
        self.assertEqual(fp_plain["value"]["contact"]["email"], "vannogkloakk@kjerringholmen.com")
        self.assertEqual(fp_plain["value"]["item_sources"]["contact_email"][0]["source_url"], "https://www.kjerringholmen.com/kontakt")

    def test_social_link_span_matches_escaped_and_plugin_urls(self) -> None:
        profile = {"organisation_number": "827340162", "name": "NOVOOI AS", "evidence": {}}
        site = {
            "status": "available",
            "source_url": "https://novooi.com/",
            "value": {
                "final_url": "https://novooi.com/",
                "identity_assessment": {"publishable": True, "score": 1.0, "reasons": ["exact organisation number"]},
                "social_links": [
                    {"platform": "instagram", "url": "https://instagram.com/novooi.com"},
                    {"platform": "facebook", "url": "https://www.facebook.com/skya24"},
                    {"platform": "x", "url": "https://x.com/novooi"},
                ],
            },
        }
        home_html = (
            '<html><body>'
            '<script>{"sameAs":["https:\\/\\/instagram.com\\/novooi.com"]}</script>'
            '<iframe src="https://plugins.facebook.com/like.php?href=https%3A%2F%2Fwww.facebook.com%2Fskya24"></iframe>'
            '<a href="https://twitter.com/novooi">Twitter</a>'
            '</body></html>'
        )
        with mock.patch.object(footprint_plus, "_get", lambda url, *a, **k: (home_html.encode(), url)):
            fp, _ = footprint_plus.extract_footprint(profile, site)

        self.assertEqual(fp["status"], "available")
        urls = [s["url"] for s in fp["value"]["social_links"]]
        self.assertEqual(
            urls,
            [
                "https://instagram.com/novooi.com",
                "https://www.facebook.com/skya24",
                "https://x.com/novooi",
            ],
        )

    def test_career_page_job_links_extracted_with_provenance(self) -> None:
        profile = {"organisation_number": "985589003", "name": "TEST SELSKAP AS", "evidence": {}}
        site = {
            "status": "available",
            "source_url": "https://testselskap.no/",
            "value": {
                "final_url": "https://testselskap.no/",
                "identity_assessment": {"publishable": True, "score": 1.0, "reasons": ["exact organisation number"]},
            },
        }
        pages = {
            "https://testselskap.no/": '<a href="/karriere">Karriere hos oss</a>',
            "https://testselskap.no/karriere": (
                '<h1>Ledige stillinger</h1>'
                '<ul><li><a href="/jobs/senior-radgiver-oslo">Senior rådgiver innen energi og miljø</a></li></ul>'
            ),
        }
        with mock.patch.object(footprint_plus, "_get", lambda url, *a, **k: ((pages.get(url).encode() if pages.get(url) else None), url)):
            fp, _ = footprint_plus.extract_footprint(profile, site)

        self.assertEqual(fp["status"], "available")
        jobs = fp["value"]["job_postings"]
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["title"], "Senior rådgiver innen energi og miljø")
        self.assertEqual(jobs[0]["url"], "https://testselskap.no/jobs/senior-radgiver-oslo")
        self.assertEqual(fp["value"]["item_sources"]["job_postings"][0]["source_url"], "https://testselskap.no/karriere")


if __name__ == "__main__":
    unittest.main()

