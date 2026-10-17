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
        self.assertEqual(seen_fetch_kwargs.get("timeout"), 18.0)
        self.assertEqual(seen_fetch_kwargs.get("crawl_secondary"), False)
        self.assertEqual(seen_proof_args[-1], 18.0)
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

    def test_parked_phrase_never_overrides_exact_org_number_and_short_page_guard(self) -> None:
        from norway_company_agent.identity import assess_website_identity

        # Real company page with "hosted by one.com" in footer + exact org number -> publishable
        profile_with_org = {
            "organisation_number": "985589003",
            "name": "TEST SELSKAP AS",
            "evidence": {
                "website": {
                    "status": "available",
                    "source_url": "https://testselskap.no/",
                    "value": {
                        "title": "Test Selskap AS",
                        "main_text_excerpt": "Hosted by one.com. Org.nr 985 589 003.",
                    },
                }
            },
        }
        ass1 = assess_website_identity(profile_with_org)
        self.assertTrue(ass1["publishable"])
        self.assertEqual(ass1["score"], 1.0)

        # Short parked stub (<300 chars) without org number -> rejected as parked
        profile_short_parked = {
            "organisation_number": "985589003",
            "name": "TEST SELSKAP AS",
            "evidence": {
                "website": {
                    "status": "available",
                    "source_url": "https://testselskap.no/",
                    "value": {
                        "title": "testselskap.no",
                        "main_text_excerpt": "testselskap.no is hosted by one.com",
                    },
                }
            },
        }
        ass2 = assess_website_identity(profile_short_parked)
        self.assertFalse(ass2["publishable"])
        self.assertEqual(ass2["score"], 0.1)
        self.assertIn("parked", ass2["reasons"][0])

        # Long real page (>=300 chars) mentioning "hosted by one.com" in footer -> NOT rejected as parked
        long_body = ("Velkommen til Test Selskap AS som leverer rådgivning og konsulenttjenester i hele Norge. " * 5) + " Hosted by one.com"
        profile_long_footer = {
            "organisation_number": "985589003",
            "name": "TEST SELSKAP AS",
            "evidence": {
                "website": {
                    "status": "available",
                    "source_url": "https://testselskap.no/",
                    "value": {
                        "title": "Test Selskap AS",
                        "main_text_excerpt": long_body,
                    },
                }
            },
        }
        ass3 = assess_website_identity(profile_long_footer)
        self.assertNotEqual(ass3["score"], 0.1)

    def test_foreign_labeled_org_number_unconditionally_blocks_address_proof(self) -> None:
        # skya.no prints Org. nr. 918 097 902 MVA (SKYA REGNSKAP AS), not 926320459 (SKYA MARKEDSFØRING AS)
        skya_profile = {
            "organisation_number": "926320459",
            "name": "SKYA MARKEDSFØRING AS",
            "evidence": {
                "registry_live": {
                    "value": {
                        "business_address": {"adresse": ["Storgata 12"], "postnummer": "2000"},
                    }
                }
            },
        }
        skya_site = {
            "status": "available",
            "source_url": "https://skya.no/",
            "value": {"final_url": "https://skya.no/"},
        }
        skya_html = b"<html><body><p>Storgata 12, 2000 Lillestr\xc3\xb8m</p><footer>Skya AS, Org. nr. 918 097 902 MVA</footer></body></html>"
        with mock.patch.object(footprint_plus, "_get", lambda u, *a, **k: (skya_html, u)):
            proof, _ = footprint_plus.hard_identity_proof(skya_profile, skya_site)
        self.assertIsNone(proof)

        # Same address without foreign org number succeeds
        clean_html = b"<html><body><p>Storgata 12, 2000 Lillestr\xc3\xb8m</p><footer>Skya Markedsf\xc3\xb8ring</footer></body></html>"
        with mock.patch.object(footprint_plus, "_get", lambda u, *a, **k: (clean_html, u)):
            proof_ok, _ = footprint_plus.hard_identity_proof(skya_profile, skya_site)
        self.assertIsNotNone(proof_ok)
        self.assertIn("registered street address and postcode", proof_ok)

    def test_registry_listed_site_promoted_only_with_exact_org_number(self) -> None:
        profile = {"organisation_number": "985589003", "name": "BÆRUM RÅDGIVNING AS", "evidence": {}}
        site = {
            "status": "available",
            "source_url": "https://www.beraad.no/",
            "value": {
                "final_url": "https://www.beraad.no/",
                "title": "Forsiden - Bærum Rådgivning",
                "identity_assessment": {"publishable": False, "score": 0.65, "reasons": ["partial legal-name overlap only"]},
                "social_link_assessments": [{"platform": "facebook", "url": "https://facebook.com/beraad", "publishable": True}],
                "social_links": [],
            },
        }
        pages = {
            "https://www.beraad.no/": '<a href="/kontakt">Kontakt</a><a href="https://facebook.com/beraad">FB</a>',
            "https://www.beraad.no/kontakt": 'Kontakt oss på <a href="mailto:post@beraad.no">post@beraad.no</a>. Org.nr 985 589 003',
        }
        with mock.patch.object(footprint_plus, "_get", lambda url, *a, **k: ((pages.get(url).encode() if pages.get(url) else None), url)):
            fp, _ = footprint_plus.extract_footprint(profile, site)

        self.assertTrue(site["value"]["identity_assessment"]["publishable"])
        self.assertEqual(site["value"]["identity_assessment"]["score"], 1.0)
        self.assertEqual(fp["status"], "available")
        self.assertEqual(fp["value"]["contact"]["email"], "post@beraad.no")

        # Address-only proof on an unproven registry-listed site must NOT promote it
        site_addr_only = {
            "status": "available",
            "source_url": "https://www.beraad.no/",
            "value": {
                "final_url": "https://www.beraad.no/",
                "title": "Forsiden",
                "identity_assessment": {"publishable": False, "score": 0.65, "reasons": []},
            },
        }
        with mock.patch.object(
            footprint_plus,
            "hard_identity_proof",
            lambda *a, **k: ("registered street address and postcode printed on https://www.beraad.no/kontakt", {"requests": 1}),
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
        self.assertIn("betakst.no", hosts3)

        # Norwegian å -> aa / ø -> oe variants and compound sector initialism
        p4 = {"name": "ÅSGREINA BYGGASSISTANSE AS", "evidence": {}}
        hosts4 = dict(footprint_plus.candidate_hosts(p4))
        self.assertIn("aasgreinabyggassistanse.no", hosts4)
        self.assertIn("aasgreina.no", hosts4)
        self.assertIn("aasgreinaba.no", hosts4)

    def test_email_regex_rejects_leading_dot_after_at(self) -> None:
        self.assertIsNone(footprint_plus._email_from_text("Kontakt: info@.a-seafood.com", "a-seafood.com"))
        hit = footprint_plus._email_from_text("Kontakt: info@a-seafood.com", "a-seafood.com")
        self.assertIsNotNone(hit)
        self.assertEqual(hit[0], "info@a-seafood.com")

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

    def test_social_link_span_matches_escaped_and_plugin_urls_and_filters_secondary_agency_links(self) -> None:
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
            '<a href="/kontakt">Kontakt</a>'
            '<script>{"sameAs":["https:\\/\\/instagram.com\\/novooi.com"]}</script>'
            '<iframe src="https://plugins.facebook.com/like.php?href=https%3A%2F%2Fwww.facebook.com%2Fskya24"></iframe>'
            '<a href="https://twitter.com/novooi">Twitter</a>'
            '</body></html>'
        )
        contact_html = (
            '<html><body>'
            '<a href="https://linkedin.com/company/novooi">Novooi LinkedIn</a>'
            '<a href="https://facebook.com/randomwebagency">Webbyrå Facebook</a>'
            '</body></html>'
        )
        pages = {
            "https://novooi.com/": home_html,
            "https://novooi.com/kontakt": contact_html,
        }
        with mock.patch.object(footprint_plus, "_get", lambda url, *a, **k: ((pages.get(url, "").encode() if pages.get(url) else None), url)):
            fp, _ = footprint_plus.extract_footprint(profile, site)

        self.assertEqual(fp["status"], "available")
        urls = [s["url"] for s in fp["value"]["social_links"]]
        self.assertEqual(
            urls,
            [
                "https://instagram.com/novooi.com",
                "https://www.facebook.com/skya24",
                "https://x.com/novooi",
                "https://linkedin.com/company/novooi",
            ],
        )
        self.assertNotIn("https://facebook.com/randomwebagency", urls)

    def test_career_page_job_links_and_card_headings_extracted_with_provenance(self) -> None:
        self.assertTrue(footprint_plus._is_career("/ledige-stillinger/"))
        self.assertFalse(footprint_plus._is_career("/stilling/"))  # bare 'stilling' excluded per review

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
            "https://testselskap.no/": '<a href="/ledige-stillinger/">Ledige stillinger</a>',
            "https://testselskap.no/ledige-stillinger/": (
                '<h1>Ledige stillinger</h1>'
                '<div class="card"><h3>Trelastmedarbeider – Byggmakker</h3><a href="/ledige-stillinger/trelastmedarbeider">LES MER</a></div>'
                '<ul><li><a href="/jobs/senior-radgiver-oslo">Senior rådgiver innen energi og miljø</a></li></ul>'
            ),
        }
        with mock.patch.object(footprint_plus, "_get", lambda url, *a, **k: ((pages.get(url).encode() if pages.get(url) else None), url)):
            fp, _ = footprint_plus.extract_footprint(profile, site)

        self.assertEqual(fp["status"], "available")
        jobs = fp["value"]["job_postings"]
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0]["title"], "Trelastmedarbeider – Byggmakker")
        self.assertEqual(jobs[1]["title"], "Senior rådgiver innen energi og miljø")
        self.assertEqual(fp["value"]["item_sources"]["job_postings"][0]["source_url"], "https://testselskap.no/ledige-stillinger/")

    def test_accept_proof_rejects_care_of_and_structural_qualifiers_without_exact_org_number(self) -> None:
        co_profile = {
            "organisation_number": "816559472",
            "name": "TEAMTEC WASTE TO ENERGY AS",
            "evidence": {
                "registry_live": {
                    "value": {
                        "business_address": {"adresse": ["c/o TeamTec AS", "Nyvei 41"], "postnummer": "4900"},
                    }
                }
            },
        }
        holding_profile = {
            "organisation_number": "999888777",
            "name": "NORDISK HOLDING AS",
            "evidence": {
                "registry_live": {
                    "value": {
                        "business_address": {"adresse": ["Storgata 1"], "postnummer": "0155"},
                    }
                }
            },
        }
        normal_profile = {
            "organisation_number": "924585463",
            "name": "RB PROFF PARTNER AS",
            "evidence": {
                "registry_live": {
                    "value": {
                        "business_address": {"adresse": ["Storgata 10"], "postnummer": "0155"},
                    }
                }
            },
        }
        addr_proof = "registered street address and postcode printed on https://teamtec.no/"
        org_proof = "exact organisation number printed on https://teamtec.no/"
        self.assertFalse(footprint_plus._accept_proof(addr_proof, {"score": 0.95}, "brand_token_guess", co_profile))
        self.assertFalse(footprint_plus._accept_proof(addr_proof, {"score": 0.95}, "name_slug_guess", holding_profile))
        self.assertTrue(footprint_plus._accept_proof(org_proof, {"score": 0.65}, "brand_token_guess", co_profile))
        self.assertTrue(footprint_plus._accept_proof(org_proof, {"score": 0.65}, "name_slug_guess", holding_profile))
        self.assertTrue(footprint_plus._accept_proof(addr_proof, {"score": 0.95}, "name_slug_guess", normal_profile))

    def test_news_excludes_tag_category_and_pagination_urls_and_backfills_news_pages(self) -> None:
        for bad_path in (
            "/news/post-tags/bwms",
            "/news/post-tags/ejectors",
            "/nyheter/tag/energi",
            "/nyheter/tags/klima",
            "/blogg/category/tips",
            "/nyheter/kategori/pressemelding",
            "/news/author/admin",
            "/nyheter/page/2",
            "/nyheter/2",
        ):
            self.assertFalse(footprint_plus._is_news(bad_path), f"Expected _is_news({bad_path!r}) to be False")

        self.assertTrue(footprint_plus._is_news("/news/new-contract-signed"))
        self.assertTrue(footprint_plus._is_news("/nyheter/aarsrapport-2025"))

        article_html = (
            b'<html><head><meta property="article:published_time" content="2025-05-10"/></head>'
            b'<body><h1>Real Article</h1></body></html>'
        )
        section_html = (
            b'<html><body>'
            b'<a href="/news/post-tags/bwms">BWMS</a>'
            b'<a href="/news/tag/marine">Marine</a>'
            b'<a href="/news/category/events">Events</a>'
            b'<a href="/news/author/editor">Editor</a>'
            b'<a href="/news/page/2">Page 2</a>'
            b'<a href="/news/real-article">Real Article</a>'
            b'</body></html>'
        )
        pages = {
            "https://example.no/news": section_html,
            "https://example.no/news/real-article": article_html,
            "https://example.no/news/post-tags/bwms": article_html,
        }
        with mock.patch.object(footprint_plus, "_get", lambda u, *a, **k: (pages.get(u), u)):
            items, _, _ = footprint_plus._news_articles(
                ["https://example.no/news"],
                ["https://example.no/news/post-tags/bwms", "https://example.no/news/page/2"],
                "example.no",
            )
        self.assertEqual([i["url"] for i in items], ["https://example.no/news/real-article"])


class Phase2RecallAndPrecisionTests(unittest.TestCase):
    def setUp(self) -> None:
        website.clear_website_caches()

    def tearDown(self) -> None:
        website.clear_website_caches()

    def test_website_title_and_description_fallbacks(self) -> None:
        # Case A: og:title fallback when <title> is empty
        html_og = b'<html><head><meta property="og:title" content="OG Company Title"/><meta name="description" content="Company meta description"/></head><body><p>Velkommen til oss.</p></body></html>'
        with mock.patch.object(website, "assert_public_url", lambda u: None), \
             mock.patch.object(website.SAFE_OPENER, "open", lambda req, timeout=None: _FakeResponse(html_og, req.full_url)):
            rec, _ = website.fetch_website("https://example-og.no/", timeout=5.0, crawl_secondary=False)
            val = rec["value"]
            self.assertEqual(val["title"], "OG Company Title")
            self.assertEqual(val["description"], "Company meta description")

        # Case B: h1 and body text do NOT populate title or description (prevents identity gate loosening)
        html_h1 = b'<html><head></head><body><h1>H1 Main Heading</h1><p>Velkommen til fris\xc3\xb8rsalongen v\xc3\xa5r i Oslo sentrum.</p></body></html>'
        with mock.patch.object(website, "assert_public_url", lambda u: None), \
             mock.patch.object(website.SAFE_OPENER, "open", lambda req, timeout=None: _FakeResponse(html_h1, req.full_url)):
            rec, _ = website.fetch_website("https://example-h1.no/", timeout=5.0, crawl_secondary=False)
            val = rec["value"]
            self.assertEqual(val["title"], "")
            self.assertEqual(val["description"], "")

        # Case C: case-insensitive meta description tag
        html_case = b'<html><head><title>Test</title><meta name="Description" content="Mixed case meta description"/></head><body><p>Tekst</p></body></html>'
        with mock.patch.object(website, "assert_public_url", lambda u: None), \
             mock.patch.object(website.SAFE_OPENER, "open", lambda req, timeout=None: _FakeResponse(html_case, req.full_url)):
            rec, _ = website.fetch_website("https://example-case.no/", timeout=5.0, crawl_secondary=False)
            self.assertEqual(rec["value"]["description"], "Mixed case meta description")

        # Case D: JSON-LD Organization description fallback
        html_jsonld = (
            b'<html><head><title>Test</title>'
            b'<script type="application/ld+json">{"@context": "https://schema.org", "@type": "Organization", "name": "Test AS", "description": "JSON-LD org description"}</script>'
            b'</head><body><p>Tekst</p></body></html>'
        )
        with mock.patch.object(website, "assert_public_url", lambda u: None), \
             mock.patch.object(website.SAFE_OPENER, "open", lambda req, timeout=None: _FakeResponse(html_jsonld, req.full_url)):
            rec, _ = website.fetch_website("https://example-jsonld.no/", timeout=5.0, crawl_secondary=False)
            self.assertEqual(rec["value"]["description"], "JSON-LD org description")

    def test_assess_social_identity_norwegian_digraphs_and_prefix(self) -> None:
        from norway_company_agent import identity

        # NygaardHagebruk matches NYGÅRD HAGEBRUK AS
        prof_nygard = {"name": "NYGÅRD HAGEBRUK AS"}
        link_nygard = {"platform": "facebook", "url": "https://facebook.com/NygaardHagebruk"}
        ass_nygard = identity.assess_social_identity(prof_nygard, link_nygard)
        self.assertTrue(ass_nygard["publishable"])
        self.assertGreaterEqual(ass_nygard["identity_score"], 0.9)
        self.assertIn("nygard", ass_nygard["matched_tokens"])
        self.assertIn("hagebruk", ass_nygard["matched_tokens"])

        # aasgreinabygg matches ÅSGREINA BYGGASSISTANSE AS
        prof_aasgreina = {"name": "ÅSGREINA BYGGASSISTANSE AS"}
        link_aasgreina = {"platform": "instagram", "url": "https://instagram.com/aasgreinabygg"}
        ass_aasgreina = identity.assess_social_identity(prof_aasgreina, link_aasgreina)
        self.assertTrue(ass_aasgreina["publishable"])
        self.assertGreaterEqual(ass_aasgreina["identity_score"], 0.9)
        self.assertIn("asgreina", ass_aasgreina["matched_tokens"])
        self.assertIn("byggassistanse", ass_aasgreina["matched_tokens"])

    def test_phone_from_text_phase2_patterns_and_rejections(self) -> None:
        # Parentheses around +47
        p1 = footprint_plus._phone_from_text("Ring oss gjerne på (+47) 973 22 321 i åpningstiden.")
        self.assertIsNotNone(p1)
        self.assertEqual(p1[0], "+4797322321")

        p2 = footprint_plus._phone_from_text("Tel: (+47) 932 30 063")
        self.assertIsNotNone(p2)
        self.assertEqual(p2[0], "+4793230063")

        # kontakt oss på label
        p3 = footprint_plus._phone_from_text("Kontakt oss på 958 78 169 for en uforpliktende prat.")
        self.assertIsNotNone(p3)
        self.assertEqual(p3[0], "+4795878169")

        # Leading word boundary rejections
        p_bad1 = footprint_plus._phone_from_text("Dette er en automobil 22334455 som kjører forbi.")
        self.assertIsNone(p_bad1)

        p_bad2 = footprint_plus._phone_from_text("Her er hotel 22334455 i Bergen sentrum.")
        self.assertIsNone(p_bad2)

        # Agency credit rejection
        p_credit = footprint_plus._phone_from_text("Nettside levert av Webbyrå AS +47 99887766")
        self.assertIsNone(p_credit)

    def test_email_from_text_domain_stem_freemail_and_rejections(self) -> None:
        # Accepted: labeled domain-stem freemail
        e1 = footprint_plus._email_from_text(
            "For timebestilling: E-post: mntannlegeklinikk@gmail.com eller ring oss.",
            "mntannlegeklinikk.no",
        )
        self.assertIsNotNone(e1)
        self.assertEqual(e1[0], "mntannlegeklinikk@gmail.com")
        self.assertIn("E-post: mntannlegeklinikk@gmail.com", e1[1])

        # Rejected: unlabeled freemail
        e_bad1 = footprint_plus._email_from_text(
            "kontakt oss eller send til random@gmail.com",
            "mntannlegeklinikk.no",
        )
        self.assertIsNone(e_bad1)

        # Rejected: missing explicit email label even if stem matches
        e_bad2 = footprint_plus._email_from_text(
            "send til mntannlegeklinikk@gmail.com snarest",
            "mntannlegeklinikk.no",
        )
        self.assertIsNone(e_bad2)

        # Rejected: stem length < 6
        e_bad3 = footprint_plus._email_from_text(
            "E-post: foo@gmail.com",
            "foo.no",
        )
        self.assertIsNone(e_bad3)

    def test_career_segments_and_job_posting_priority_sorting(self) -> None:
        # stilling-ledig recognized as career path
        self.assertTrue(footprint_plus._is_career("/stilling-ledig/"))
        self.assertTrue(footprint_plus._is_career("/stillinger-ledige/"))
        self.assertFalse(footprint_plus._is_career("/innstilling/"))

        # Sorting prioritizes path segment match over anchor text only
        careers = [
            "https://www.example.no/a-samarbeide-med-oss",
            "https://www.example.no/ledig-stilling/",
        ]
        careers.sort(key=lambda u: 0 if any(s in footprint_plus.CAREER_SEGMENTS for s in footprint_plus._segments(footprint_plus.urllib.parse.urlparse(u).path)) else 1)
        self.assertEqual(careers[0], "https://www.example.no/ledig-stilling/")


if __name__ == "__main__":
    unittest.main()



