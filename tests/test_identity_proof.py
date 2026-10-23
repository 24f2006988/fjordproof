import unittest
from unittest import mock

from norway_company_agent import footprint_plus
from norway_company_agent.contract import to_contract


BODY = "<article><p>" + " ".join(["Vi har gode nyheter om selskapet og kundene våre i regionen."] * 12) + "</p></article>"


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

    def test_proof_links_rank_terms_first_and_shopify_policies(self):
        html = ('<a href="/cookies">Cookies</a><a href="/personvern">Personvern</a><a href="/om-oss">Om oss</a>'
                '<a href="/side/12">Kontakt</a><a href="/kjopsvilkar">Kjøpsvilkår</a><a href="/blogg">Blogg</a>')
        self.assertEqual(footprint_plus._proof_links("https://example.no", html),
                         ["https://example.no/kjopsvilkar", "https://example.no/side/12", "https://example.no/om-oss",
                          "https://example.no/personvern", "https://example.no/cookies"])
        shop = '<script src="//cdn.shopify.com/s/x.js"></script><a href="/pages/kontakt">Kontakt</a>'
        self.assertEqual(footprint_plus._proof_links("https://shop.no", shop)[:2],
                         ["https://shop.no/policies/terms-of-service", "https://shop.no/policies/contact-information"])

    def test_shopify_terms_page_beyond_four_links_gives_org_proof(self):
        links = "".join(f'<a href="/personvern-{i}">Personvern {i}</a>' for i in range(6))
        pages = {"https://shop.no/": '<script src="https://cdn.shopify.com/a.js"></script>' + links,
                 "https://shop.no/policies/terms-of-service": "Supermercado AS, org.nr. 985 589 003"}

        def fake_get(url, *a, **k):
            body = pages.get(url)
            return (body.encode() if body else None), url
        with mock.patch.object(footprint_plus, "_get", fake_get):
            proof = footprint_plus.hard_identity_proof(_profile(), {"source_url": "https://shop.no/"})[0]
        self.assertIn("exact organisation number printed on https://shop.no/policies/terms-of-service", proof or "")

    def test_other_tld_guesses_come_last_and_need_the_exact_org_number(self):
        profile = {"organisation_number": "931467425", "name": "AQUAPREDICT AS", "evidence": {"registry": {"value": {}}}}
        with mock.patch.object(footprint_plus.discovery_index, "candidates", lambda p: []):
            hosts = footprint_plus.candidate_hosts(profile)
        tld = [h for h, m in hosts if m == "other_tld_guess"]
        self.assertIn("aquapredict.io", tld)
        self.assertTrue(all(m == "other_tld_guess" for _, m in hosts[len(hosts) - len(tld):]))
        self.assertIn("other_tld_guess", footprint_plus.EXACT_ONLY_METHODS)
        # address proof is never enough for an other-ending guess
        self.assertFalse(footprint_plus._accept_proof("registered street address and postcode printed on https://x.io/",
                                                      {"score": 0.95}, "other_tld_guess", profile))

    def test_native_spelling_idn_host_is_a_name_guess(self):
        profile = {"organisation_number": "964348030", "name": "THANSTRØM BIL AS", "evidence": {"registry": {"value": {}}}}
        with mock.patch.object(footprint_plus.discovery_index, "candidates", lambda p: []):
            hosts = footprint_plus.candidate_hosts(profile)
        self.assertIn(("xn--thanstrmbil-mgb.no", "name_slug_guess"), hosts)
        self.assertLess(hosts.index(("xn--thanstrmbil-mgb.no", "name_slug_guess")), 4)

    def test_care_of_the_company_itself_is_not_care_of(self):
        def prof(name, addr):
            return {"name": name, "evidence": {"registry_live": {"value": {"business_address": {"adresse": [addr, "Rådhusgata 3"]}}}}}
        self.assertFalse(footprint_plus._is_care_of(prof("KFREGNSKAP AS", "c/o KFRegnskap AS")))
        self.assertTrue(footprint_plus._is_care_of(prof("CREATEEEVITY AS", "c/o Hauge Business Network")))
        self.assertTrue(footprint_plus._is_care_of(prof("KFREGNSKAP AS", "c/o")))
        self.assertFalse(footprint_plus._is_care_of(prof("KFREGNSKAP AS", "Storgata 1")))

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
        # A sitemap <lastmod> alone is never published as a publication date.
        v = self._footprint({"https://example.no/": "<p>Hei</p>", "https://example.no/sitemap.xml": sitemap})
        self.assertEqual(v["careers_urls"], ["https://example.no/karriere/"])
        self.assertEqual(v["dated_activity"], [])
        # The article page's own publication markup is used instead (title, direct URL, published date).
        article = ('<html><head><meta property="og:type" content="article"><meta property="og:title" content="Ny kunde i Oslo">'
                   '<meta property="article:published_time" content="2026-08-20T08:00:00+02:00"></head><body>' + BODY + '</body></html>')
        v = self._footprint({"https://example.no/": "<p>Hei</p>", "https://example.no/sitemap.xml": sitemap,
                             "https://example.no/nyheter/ny-kunde-i-oslo": article})
        self.assertEqual(v["dated_activity"], [{"title": "Ny kunde i Oslo", "url": "https://example.no/nyheter/ny-kunde-i-oslo",
                                                "date": "2026-08-20", "date_kind": "meta_published_time"}])

    def test_news_section_page_yields_article_items_with_their_own_dates(self):
        home = '<a href="/nyheter">Nyheter</a>'
        section = ('<a href="/nyheter/apning-pa-hamar">Åpning</a><a href="/nyheter/page/2">Neste</a>'
                   '<a href="/nyheter/kategori/drift">Drift</a><a href="https://other.no/nyheter/x">Ekstern</a>')
        art = ('<script type="application/ld+json">{"@context":"https://schema.org","@graph":[{"@type":"NewsArticle",'
               '"headline":"Vi åpner på Hamar","datePublished":"2026-05-02T09:00:00Z","dateModified":"2026-09-01"}]}</script>' + BODY)
        v = self._footprint({"https://example.no/": home, "https://example.no/nyheter": section,
                             "https://example.no/nyheter/apning-pa-hamar": art})
        self.assertEqual(v["dated_activity"], [{"title": "Vi åpner på Hamar", "url": "https://example.no/nyheter/apning-pa-hamar",
                                                "date": "2026-05-02", "date_kind": "jsonld_datePublished"}])
        src = v["item_sources"]["dated_activity"][0]
        self.assertEqual(src["source_url"], "https://example.no/nyheter/apning-pa-hamar")
        self.assertIn("datePublished", src["span"])

    def test_article_without_publication_markup_or_with_future_date_is_skipped(self):
        self.assertIsNone(footprint_plus._article_meta(b"<h1>Om oss</h1><p>Oppdatert 2026-01-01</p>"))
        self.assertIsNone(footprint_plus._article_meta(b'<h1>X</h1><meta property="article:published_time" content="2999-01-01">'))
        self.assertIsNone(footprint_plus._article_meta(b'<meta property="article:modified_time" content="2026-01-01"><h1>X</h1>'))

    def test_wordpress_rest_posts_are_candidates_published_only_from_their_own_article_page(self):
        posts = '[{"date": "2026-08-15T09:00:00", "link": "https://example.no/hei/", "title": {"rendered": "Hei &amp; velkommen"}}]'
        pages = {
            "https://example.no/": '<link rel="https://api.w.org/" href="https://example.no/wp-json/">',
            "https://example.no/wp-json/wp/v2/posts?per_page=5&_fields=date,link,title": posts,
        }
        self.assertEqual(self._footprint(pages)["dated_activity"], [])
        pages["https://example.no/hei/"] = ('<meta property="og:type" content="article"><meta property="og:title" content="Hei &amp; velkommen">'
                                            '<meta property="article:published_time" content="2026-08-15T09:00:00+02:00">' + BODY)
        v = self._footprint(pages)
        self.assertEqual(v["dated_activity"][0]["title"], "Hei & velkommen")
        self.assertEqual(v["dated_activity"][0]["date"], "2026-08-15")
        self.assertEqual(v["item_sources"]["dated_activity"][0]["source_url"], "https://example.no/hei/")

    def test_feed_item_needs_published_date_and_an_article_page(self):
        feed = ('<rss><channel>'
                '<item><title>Portefølje: Firma AS</title><link>https://example.no/firma-as/</link><pubDate>Mon, 02 Jun 2025 10:00:00 +0000</pubDate></item>'
                '<item><title>Ny avtale</title><link>https://example.no/ny-avtale/</link><pubDate>Tue, 03 Jun 2025 10:00:00 +0000</pubDate></item>'
                '</channel></rss>')
        atom = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Bare oppdatert</title><link href="https://example.no/x/"/><updated>2026-01-01T00:00:00Z</updated></entry></feed>'
        self.assertEqual(footprint_plus._parse_feed(atom.encode(), "https://example.no/"), [])
        article = ('<meta property="og:type" content="article">'
                   '<meta property="article:published_time" content="2025-06-03T12:00:00+02:00"><h1>Ny avtale</h1>' + BODY)
        v = self._footprint({
            "https://example.no/": '<link rel="alternate" type="application/rss+xml" href="/feed/">',
            "https://example.no/feed/": feed,
            "https://example.no/firma-as/": "<h1>Firma AS</h1><p>Investert 2025.</p>",  # portfolio page: no article markup
            "https://example.no/ny-avtale/": article,
        })
        self.assertEqual([(i["url"], i["date"]) for i in v["dated_activity"]], [("https://example.no/ny-avtale/", "2025-06-03")])

    def test_article_meta_rejects_ordinary_product_event_and_short_pages(self):
        dated = '<meta property="article:published_time" content="2026-03-01">'
        ok = '<meta property="og:type" content="article">' + dated + "<h1>Nyhet</h1>" + BODY
        self.assertIsNotNone(footprint_plus._article_meta(ok.encode()))
        # article:published_time without og:type=article, or generic Article markup alone, is an ordinary page
        self.assertIsNone(footprint_plus._article_meta((dated + "<h1>Side</h1>" + BODY).encode()))
        generic = '<script type="application/ld+json">{"@type":"Article","headline":"Side","datePublished":"2026-03-01"}</script>'
        self.assertIsNone(footprint_plus._article_meta((generic + BODY).encode()))
        self.assertIsNotNone(footprint_plus._article_meta((generic.replace('"Article"', '"BlogPosting"') + BODY).encode()))
        # product / event markup, product og:type, or too little article text
        product = '<script type="application/ld+json">{"@type":"Product","name":"Stol"}</script>'
        self.assertIsNone(footprint_plus._article_meta((product + ok).encode()))
        self.assertIsNone(footprint_plus._article_meta(ok.replace('content="article"', 'content="product"').encode()))
        self.assertIsNone(footprint_plus._article_meta(('<meta property="og:type" content="article">' + dated + "<h1>Kurs</h1><p>Påmelding her.</p>").encode()))
        # with company names: the article must name the company or speak for it (vi/oss/vår, we/our)
        about = footprint_plus._about_names({"name": "A-SEAFOOD AS"}, "https://a-seafood.com/")
        self.assertEqual(about, [["a", "seafood"]])
        demo = ('<meta property="og:type" content="article">' + dated + "<h1>Salmon</h1><article>"
                + "Salmon and trout are no longer a luxury in many markets. " * 12 + "</article>")
        self.assertIsNone(footprint_plus._article_meta(demo.encode(), about))
        self.assertIsNotNone(footprint_plus._article_meta(demo.replace("in many markets", "for A-Seafood").encode(), about))
        self.assertIsNotNone(footprint_plus._article_meta(ok.encode(), about))  # BODY says 'Vi har ...'
        # a bare <time> element is not a publication date
        self.assertIsNone(footprint_plus._article_meta(("<main><time datetime='2026-03-01'>1. mars</time><h1>Kurs</h1></main>" + BODY).encode()))

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


class NameDomainProof(unittest.TestCase):
    """Site without the org number: accepted only when the domain is the whole unique legal name, written on the page as the
    company's name (with its AS suffix, or in title/footer next to the registered place), on a page with real content."""
    FILLER = " Vi selger kvalitetsvarer og har lang erfaring. " * 8

    def profile(self, name="SUPERMERCADO AS", address=None):
        return {"organisation_number": "916361750", "name": name,
                "evidence": {"registry_live": {"value": {"business_address": address or {"adresse": ["Storgata 1"], "postnummer": "2640", "poststed": "VINSTRA"}}}}}

    def record(self, url, text, title=""):
        return {"source_url": url, "value": {"final_url": url, "title": title, "main_text_excerpt": text + self.FILLER, "pages": []}}

    def proof(self, profile, record, method="name_slug_guess", unique=True):
        with mock.patch.object(footprint_plus, "_name_is_unique", return_value=(unique, 1)):
            return footprint_plus.name_domain_proof(profile, record, method)[0]

    def test_accepted(self):
        sup = self.profile()
        self.assertIsNotNone(self.proof(sup, self.record("https://supermercado.no/", "Velkommen til Supermercado AS, Norge.")))
        # Title name plus the registered place (a small town) also counts.
        self.assertIsNotNone(self.proof(sup, self.record("https://supermercado.no/", "Butikken ligger i Vinstra.", title="Supermercado")))
        # A thin script-built page counts only when it names the legal entity itself.
        wix = {"source_url": "https://www.wildgear.no/", "value": {"final_url": "https://www.wildgear.no/", "title": "New Page | Wild Gear AS",
                                                                  "main_text_excerpt": "Menu", "pages": []}}
        self.assertIsNotNone(self.proof(self.profile("WILD GEAR AS"), wix))

    def test_ordinary_words_are_not_the_company_name(self):
        # pentsmil.no is CC VEST TANNLEGESENTER AS (Oslo); PENT SMIL AS is a different practice in Vinstra.
        page = self.record("https://www.pentsmil.no/", "For de fleste av oss er det viktig med et pent smil. Norge.", title="Pentsmil-no5")
        self.assertIsNone(self.proof(self.profile("PENT SMIL AS"), page))

    def test_rejections(self):
        sup = self.profile()
        page = "Velkommen til Supermercado AS, Norge."
        self.assertIsNone(self.proof(sup, self.record("https://supermercado.no/", page), unique=False))  # another entity shares the name
        self.assertIsNone(self.proof(sup, self.record("https://supermercado.no/", "Velkommen! Norge.")))  # name only in the address bar
        self.assertIsNone(self.proof(sup, self.record("https://super-shop.no/", page)))  # domain is not the name
        self.assertIsNone(self.proof(sup, self.record("https://supermercado.no/", page + " Org.nr 912 345 678")))  # foreign org number
        self.assertIsNone(self.proof(sup, self.record("https://supermercado.no/", page + " Lanseres snart")))  # placeholder
        self.assertIsNone(self.proof(sup, self.record("https://supermercado.no/", page), method="registry_email_domain"))
        oslo = self.profile(address={"adresse": ["Gata 2"], "postnummer": "0855", "poststed": "OSLO"})
        self.assertIsNone(self.proof(oslo, self.record("https://supermercado.no/", "Vi holder til i Oslo.", title="Supermercado")))  # big city
        self.assertIsNone(self.proof(self.profile("FJELLGLØD HOLDING AS"), self.record("https://fjellglodholding.no/", "Fjellglød Holding AS")))
        self.assertIsNone(self.proof(self.profile("MAXI AS"), self.record("https://maxi.no/", "Maxi AS")))  # too short to be distinctive
        care_of = self.profile(address={"adresse": ["c/o Regnskap AS", "Gata 2"], "postnummer": "2640", "poststed": "VINSTRA"})
        self.assertIsNone(self.proof(care_of, self.record("https://supermercado.no/", page)))
        with mock.patch.dict("os.environ", {"SIGNALPOST_NAME_DOMAIN_PROOF": "off"}):
            self.assertIsNone(self.proof(sup, self.record("https://supermercado.no/", page)))


class CrawlIndexOnlyRaisesPriority(unittest.TestCase):
    def test_index_host_that_is_also_a_name_guess_keeps_the_name_guess_rule(self):
        profile = {"organisation_number": "924585463", "name": "RB PROFF PARTNER AS", "evidence": {}}
        with mock.patch.object(footprint_plus.discovery_index, "candidates", return_value=[("rbproffpartner.no", "https://rbproffpartner.no/kontakt")]):
            hosts = footprint_plus.candidate_hosts(profile)
        self.assertEqual(hosts[0], ("rbproffpartner.no", "name_slug_guess"))


class BusinessSportsClubTests(unittest.TestCase):
    def _score(self, name, form):
        from norway_company_agent.identity import assess_website_identity
        site = {"source_url": "https://example.no/", "value": {"final_url": "https://example.no/", "title": name.title(),
                                                                "main_text_excerpt": "Velkommen " * 30}}
        return assess_website_identity({"organisation_number": "964348030", "name": name, "legal_form": form,
                                        "evidence": {"website": site}})["reasons"]

    def test_bil_in_a_company_name_means_car_not_sports_club(self):
        self.assertNotIn("business sports-club entity points to the operating company's site without club evidence",
                         self._score("THANSTRØM BIL AS", "AS"))
        self.assertIn("business sports-club entity points to the operating company's site without club evidence",
                      self._score("EQUINOR BIL", "FLI"))
