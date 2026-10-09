import unittest

from norway_company_agent.batch import terminal_envelope
from norway_company_agent.contract import to_contract
from norway_company_agent.footprint_plus import _is_career, _is_news, _norm_phone, _phone_from_text

SHA_HOME = "a" * 64
SHA_CONTACT = "b" * 64


class FootprintHeuristics(unittest.TestCase):
    def test_careers_needs_whole_word(self):
        self.assertTrue(_is_career("/karriere/"))
        self.assertTrue(_is_career("/om-oss/ledige-stillinger"))
        self.assertTrue(_is_career("/", "Jobs"))
        self.assertFalse(_is_career("/tjenester/faa-jobben-gjort"))
        self.assertFalse(_is_career("/", "Få jobben gjort"))

    def test_news_excludes_product_categories(self):
        self.assertTrue(_is_news("/nyheter/ny-butikk"))
        self.assertTrue(_is_news("/", "Blogg"))
        self.assertTrue(_is_news("/post/skatt-pa-krypto"))
        self.assertFalse(_is_news("/produkter/nyheter"))
        self.assertFalse(_is_news("/tapet/fibertapet/nyheter"))
        self.assertTrue(_is_news("/no/nyheter/ny-butikk"))
        self.assertFalse(_is_news("/brands/newsprint"))

    def test_phone_is_canonical_or_dropped(self):
        self.assertEqual(_norm_phone("%20+4792566136"), "+4792566136")
        self.assertEqual(_norm_phone("92 56 61 36"), "+4792566136")
        self.assertEqual(_norm_phone("0047 92566136"), "+4792566136")
        self.assertIsNone(_norm_phone("038321014"))
        self.assertEqual(_phone_from_text("Ring oss: 92 56 61 36 i dag")[0], "+4792566136")


class PerItemEvidence(unittest.TestCase):
    def make(self, footprint_status="available"):
        site = {"status": "available", "source_url": "https://eksempel.no/", "source_class": "company_owned_site",
                "retrieved_at": "2026-01-01T00:00:02Z", "content_sha256": SHA_HOME,
                "value": {"title": "Eksempel", "identity_assessment": {"publishable": True, "score": 1.0}}}
        fp = {"status": footprint_status, "source_url": "https://eksempel.no/", "source_class": "company_owned_site",
              "retrieved_at": "2026-01-01T00:00:03Z", "content_sha256": SHA_HOME,
              "value": {"careers_urls": ["https://eksempel.no/karriere"], "news_pages": [], "dated_activity": [],
                        "social_links": [], "job_postings": [],
                        "contact": {"phone": "+4792566136"},
                        "item_sources": {
                            "careers_urls": [{"item": "https://eksempel.no/karriere", "source_url": "https://eksempel.no/",
                                              "sha256": SHA_HOME, "span": '<a href="/karriere">Karriere</a>'}],
                            "contact_phone": [{"item": "+4792566136", "source_url": "https://eksempel.no/kontakt",
                                               "sha256": SHA_CONTACT, "span": "Tlf 92 56 61 36"}]}}}
        profile = {"organisation_number": "123456789", "name": "Eksempel AS",
                   "evidence": {"registry": {"status": "available", "source_url": "https://data.brreg.no/x", "source_class": "official_registry",
                                             "retrieved_at": "2026-01-01T00:00:00Z", "content_sha256": "c" * 64, "value": {"navn": "Eksempel AS"}},
                                "website": site, "external_footprint": fp,
                                "group": {"status": "not_found", "source_url": "https://data.brreg.no/g", "source_class": "official_registry",
                                          "retrieved_at": "2026-01-01T00:00:01Z", "value": None}},
                   "run_metrics": {"requests": 1, "latencies_ms": [1], "plus_requests": 0}}
        env = terminal_envelope(profile, run_id="t", modules=["registry", "website", "external_footprint", "group"],
                                started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:00:04Z")
        return to_contract(env)

    def test_claims_cite_the_page_that_contains_the_item(self):
        env = self.make()
        evidence = {e["id"]: e for e in env["evidence"]}
        claims = {c["field"]: c for c in env["claims"]}
        phone = evidence[claims["site_phone"]["evidence_ids"][0]]
        self.assertEqual(phone["source_url"], "https://eksempel.no/kontakt")
        self.assertEqual(phone["content_sha256"], SHA_CONTACT)
        self.assertIn("92 56 61 36", phone["claim_span"])
        careers = evidence[claims["careers_urls"]["evidence_ids"][0]]
        self.assertEqual(careers["source_url"], "https://eksempel.no/")
        self.assertIn("/karriere", careers["claim_span"])

    def test_no_invented_hash_for_empty_sources(self):
        env = self.make()
        for e in env["evidence"]:
            self.assertNotEqual(e["content_sha256"], "74234e98afe7498fb5daf1f36ac2d78acc339464f950703b8c019892f982b90b")  # sha256("null")
        self.assertNotIn("ev-group", {e["id"] for e in env["evidence"]})
        ids = {e["id"] for e in env["evidence"]}
        for c in env["claims"]:
            self.assertTrue(set(c["evidence_ids"]) <= ids)


if __name__ == "__main__":
    unittest.main()
