import unittest
from unittest import mock

from norway_company_agent import footprint_plus
from norway_company_agent.contract import to_contract


def _profile():
    return {"organisation_number": "985589003", "name": "TEST AS", "evidence": {"registry": {"value": {}}}}


def _proof(pages):
    def fake_get(url, *a, **k):
        body = pages.get(url)
        return (body.encode() if body else None), url
    with mock.patch.object(footprint_plus, "_get", fake_get):
        return footprint_plus.hard_identity_proof(_profile(), {"source_url": "https://example.no/"})[0]


class HardProofTests(unittest.TestCase):
    def test_org_number_on_linked_privacy_page(self):
        pages = {"https://example.no/": '<a href="/handel-og-personvern">Personvern</a>', "https://example.no/handel-og-personvern": "Org.nr 985 589 003 MVA"}
        self.assertIn("exact organisation number", _proof(pages) or "")

    def test_org_number_inside_longer_digit_string_is_not_proof(self):
        self.assertIsNone(_proof({"https://example.no/": "Ring 47985589003 eller 9855890031"}))

    def test_other_companys_number_is_not_proof(self):
        self.assertIsNone(_proof({"https://example.no/": "Org.nr 912 345 678"}))


    def test_street_address_and_postcode_from_live_registry_is_proof(self):
        profile = {
            "organisation_number": "985589003",
            "name": "TEST AS",
            "evidence": {
                "registry_live": {
                    "value": {
                        "business_address": {
                            "adresse": ["Storgata 15B"],
                            "postnummer": "0182",
                            "poststed": "Oslo",
                        }
                    }
                }
            },
        }
        pages = {"https://example.no/": "Besøk oss i Storgata 15B, 0182 Oslo"}
        with mock.patch.object(footprint_plus, "_get", lambda url, *a, **k: ((pages.get(url).encode() if pages.get(url) else None), url)):
            proof, _ = footprint_plus.hard_identity_proof(profile, {"source_url": "https://example.no/"})
        self.assertIn("registered street address and postcode", proof or "")

    def test_short_street_line_and_postcode_is_not_proof(self):
        profile = {
            "organisation_number": "985589003",
            "name": "TEST AS",
            "evidence": {"registry_live": {"value": {"business_address": {"adresse": ["Vn 1"], "postnummer": "0182"}}}},
        }
        pages = {"https://example.no/": "Annen bedrift, Vn 1, 0182 Oslo"}
        with mock.patch.object(footprint_plus, "_get", lambda url, *a, **k: ((pages.get(url).encode() if pages.get(url) else None), url)):
            proof, _ = footprint_plus.hard_identity_proof(profile, {"source_url": "https://example.no/"})
        self.assertIsNone(proof)

    def test_contact_page_tel_link_is_url_decoded(self):
        profile = {"organisation_number": "985589003", "name": "TEST AS", "evidence": {}}
        pages = {
            "https://example.no/": '<a href="/kontakt">Kontakt</a>',
            "https://example.no/kontakt": '<a href="tel:%20+4792566136">Ring</a><a href="mailto:post@example.no">Mail</a>',
        }
        with mock.patch.object(footprint_plus, "_get", lambda url, *a, **k: ((pages.get(url).encode() if pages.get(url) else None), url)):
            ev, _ = footprint_plus.extract_footprint(profile, {"status": "available", "source_url": "https://example.no/", "value": {"identity_assessment": {"publishable": True}}})
        contact = ev["value"]["contact"]
        self.assertEqual(contact["phone"], "+4792566136")
        self.assertEqual(contact["email"], "post@example.no")

    def _footprint(self, pages):
        profile = {"organisation_number": "985589003", "name": "TEST AS", "evidence": {}}
        site = {"status": "available", "source_url": "https://example.no/", "value": {"identity_assessment": {"publishable": True}}}
        with mock.patch.object(footprint_plus, "_get", lambda url, *a, **k: ((pages.get(url).encode() if pages.get(url) else None), url)):
            return footprint_plus.extract_footprint(profile, site)[0]["value"]

    def test_homepage_tel_link_is_decoded_and_text_phone_is_normalised(self):
        v = self._footprint({"https://example.no/": '<a href="tel:%20+47 92 56 61 36">x</a>'})
        self.assertEqual(v["contact"]["phone"], "+4792566136")
        v = self._footprint({"https://example.no/": "<p>Ring oss: 92 56 61 36</p><p>Org.nr 985 589 003</p>"})
        self.assertEqual(v["contact"]["phone"], "+4792566136")
        v = self._footprint({"https://example.no/": "<p>Org.nr 985 589 003 Faks 12345678</p>"})
        self.assertNotIn("phone", v["contact"])

    def test_sitemap_gives_careers_news_and_dated_posts_from_own_host_only(self):
        sitemap = (
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
            '<url><loc>https://example.no/karriere/</loc></url>'
            '<url><loc>https://example.no/nyheter/ny-kunde-i-oslo</loc><lastmod>2026-09-01T10:00:00+00:00</lastmod></url>'
            '<url><loc>https://other.no/nyheter/skal-ikke-med</loc><lastmod>2026-09-02</lastmod></url></urlset>'
        )
        v = self._footprint({"https://example.no/": "<p>Hei</p>", "https://example.no/sitemap.xml": sitemap})
        self.assertEqual(v["careers_urls"], ["https://example.no/karriere/"])
        self.assertEqual([d["url"] for d in v["dated_activity"]], ["https://example.no/nyheter/ny-kunde-i-oslo"])
        self.assertEqual(v["dated_activity"][0]["date"], "2026-09-01")
        self.assertEqual(v["dated_activity"][0]["date_kind"], "sitemap_lastmod")

    def test_wordpress_rest_posts_used_when_no_feed(self):
        posts = '[{"date": "2026-08-15T09:00:00", "link": "https://example.no/hei/", "title": {"rendered": "Hei &amp; velkommen"}}]'
        v = self._footprint({
            "https://example.no/": '<link rel="https://api.w.org/" href="https://example.no/wp-json/">',
            "https://example.no/wp-json/wp/v2/posts?per_page=5&_fields=date,link,title": posts,
        })
        self.assertEqual(v["dated_activity"][0]["title"], "Hei & velkommen")
        self.assertEqual(v["dated_activity"][0]["date"], "2026-08-15")

    def test_accept_proof_rules(self):
        org = "exact organisation number printed on https://x.no/"
        adr = "registered street address and postcode printed on https://x.no/"
        self.assertTrue(footprint_plus._accept_proof(org, {"score": 0.3}, "registry_email_domain"))
        self.assertTrue(footprint_plus._accept_proof(org, {"score": 0.65}, "brand_token_guess"))
        self.assertFalse(footprint_plus._accept_proof(adr, {"score": 0.95}, "registry_email_domain"))
        self.assertFalse(footprint_plus._accept_proof(adr, {"score": 0.65}, "brand_token_guess"))
        self.assertTrue(footprint_plus._accept_proof(adr, {"score": 0.85}, "name_slug_guess"))
        self.assertFalse(footprint_plus._accept_proof(None, {"score": 1.0}, "name_slug_guess"))

    def _discover(self, method, score, proof):
        profile = {"organisation_number": "985589003", "name": "BONDELIA I BORETTSLAG", "evidence": {}}
        record = {"status": "available", "source_url": "https://www.gobb.no/", "value": {}}
        with mock.patch.object(footprint_plus, "candidate_hosts", lambda p: [("gobb.no", method)]), \
             mock.patch.object(footprint_plus, "_resolves", lambda h: True), \
             mock.patch.object(footprint_plus, "fetch_website", lambda h, **k: (dict(record), {"requests": 1, "bytes": 1})), \
             mock.patch.object(footprint_plus, "apply_website_identity_gate", lambda p, r: {"assessment": {"score": score, "publishable": score >= 0.9, "reasons": []}}), \
             mock.patch.object(footprint_plus, "hard_identity_proof", lambda p, r, *a: (proof, {"requests": 1, "bytes": 0})):
            return footprint_plus.discover_website(profile)[0]

    def test_housing_manager_site_via_registry_email_with_address_only_is_rejected(self):
        adr = "registered street address and postcode printed on https://www.gobb.no/"
        self.assertIsNone(self._discover("registry_email_domain", 0.85, adr))

    def test_registry_email_site_with_exact_org_number_is_accepted(self):
        rec = self._discover("registry_email_domain", 0.85, "exact organisation number printed on https://www.gobb.no/")
        self.assertIsNotNone(rec)
        self.assertEqual(rec["value"]["identity_assessment"]["score"], 1.0)

    def test_guessed_domain_address_only_needs_strong_name_match(self):
        adr = "registered street address and postcode printed on https://www.gobb.no/"
        self.assertIsNone(self._discover("brand_token_guess", 0.65, adr))
        rec = self._discover("name_slug_guess", 0.85, adr)
        self.assertEqual(rec["value"]["identity_assessment"]["score"], 0.9)

    def test_phone_or_leader_alone_is_not_proof(self):
        profile = {
            "organisation_number": "985589003",
            "name": "TEST AS",
            "evidence": {
                "registry_live": {"value": {"phone": "22334455"}},
                "roles": {"value": {"roles": [{"name": "Ola Nordmann", "role_code": "DAGL"}]}},
            },
        }
        pages = {"https://example.no/": "Kontakt daglig leder Ola Nordmann på tlf 22334455"}
        with mock.patch.object(footprint_plus, "_get", lambda url, *a, **k: ((pages.get(url).encode() if pages.get(url) else None), url)):
            proof, _ = footprint_plus.hard_identity_proof(profile, {"source_url": "https://example.no/"})
        self.assertIsNone(proof)


if __name__ == "__main__":
    unittest.main()
