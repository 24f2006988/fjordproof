import gzip
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from norway_company_agent import discovery_index, footprint_plus  # noqa: E402

ORG = "923795545"
PROFILE = {"organisation_number": ORG, "name": "FLEXISTORE NORGE AS", "evidence": {}}


class DiscoveryIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        path = Path(self.tmp.name) / "idx.json.gz"
        with gzip.open(path, "wt", encoding="utf-8") as fh:
            json.dump({"pairs": {ORG: [["flexistore.no", "https://flexistore.no/kontakt"]]}}, fh)
        self.env = mock.patch.dict(os.environ, {"SIGNALPOST_DISCOVERY_INDEX": str(path)})
        self.env.start()
        os.environ.pop("SIGNALPOST_DISCOVERY_CACHE", None)
        discovery_index.reset()
        footprint_plus.clear_search_caches()

    def tearDown(self) -> None:
        self.env.stop()
        discovery_index.reset()
        self.tmp.cleanup()

    def test_lookup_and_off_switch(self) -> None:
        self.assertEqual(discovery_index.candidates(PROFILE), [("flexistore.no", "https://flexistore.no/kontakt")])
        self.assertEqual(discovery_index.candidates({"organisation_number": "810034882"}), [])
        with mock.patch.dict(os.environ, {"SIGNALPOST_DISCOVERY_CACHE": "off"}):
            self.assertEqual(discovery_index.candidates(PROFILE), [])
            self.assertNotIn("discovery_index", dict(footprint_plus.candidate_hosts(PROFILE)).values())

    def test_missing_index_file_is_empty_not_an_error(self) -> None:
        with mock.patch.dict(os.environ, {"SIGNALPOST_DISCOVERY_INDEX": str(Path(self.tmp.name) / "none.gz")}):
            discovery_index.reset()
            self.assertEqual(discovery_index.candidates(PROFILE), [])

    def test_index_host_is_first_candidate(self) -> None:
        # First in line; flexistore.no is also a plain name guess for this company, so it is judged by that rule.
        self.assertEqual(footprint_plus.candidate_hosts(PROFILE)[0], ("flexistore.no", "name_slug_guess"))
        with mock.patch.object(discovery_index, "candidates", return_value=[("other-brand.no", "https://other-brand.no/x")]):
            self.assertEqual(footprint_plus.candidate_hosts(PROFILE)[0], ("other-brand.no", "discovery_index"))

    def _discover(self, proof: str | None, publishable: bool = True):
        seen: dict = {}

        def fake_proof(prof, rec, *args, **kwargs):
            seen.update(kwargs)
            return proof, {"requests": 1}

        record = {"status": "available", "source_url": "https://flexistore.no/", "value": {"pages": []}}
        with mock.patch.object(footprint_plus, "candidate_hosts", lambda p: [("flexistore.no", "discovery_index")]), \
             mock.patch.object(footprint_plus, "_resolves", lambda h: True), \
             mock.patch.object(footprint_plus, "fetch_website", lambda h, **k: (dict(record), {"requests": 1, "bytes": 1})), \
             mock.patch.object(footprint_plus, "apply_website_identity_gate",
                               lambda p, r: {"assessment": {"score": 0.1, "publishable": publishable, "reasons": []}}), \
             mock.patch.object(footprint_plus, "hard_identity_proof", fake_proof):
            found, _ = footprint_plus.discover_website(PROFILE)
        return found, seen

    def test_exact_org_number_live_is_required(self) -> None:
        found, seen = self._discover("exact organisation number printed on https://flexistore.no/kontakt")
        self.assertIsNotNone(found)
        self.assertEqual(seen.get("extra_urls"), ["https://flexistore.no/kontakt"])
        self.assertIn("Common Crawl discovery index", found["note"])
        # address proof or a name-based gate pass is not enough for a crawl-cache candidate
        self.assertIsNone(self._discover("registered street address and postcode printed on https://flexistore.no/")[0])
        self.assertIsNone(self._discover(None, publishable=True)[0])

    def test_hint_page_checked_and_foreign_hint_ignored(self) -> None:
        fetched: list[str] = []

        def fake_get(url, timeout):
            fetched.append(url)
            if url == "https://flexistore.no/kontakt":
                return f"<p>Org nr: {ORG[:3]} {ORG[3:6]} {ORG[6:]}</p>".encode(), url
            return b"<p>Velkommen</p>", url

        site = {"source_url": "https://flexistore.no/", "value": {"pages": []}}
        with mock.patch.object(footprint_plus, "_get", fake_get):
            proof, _ = footprint_plus.hard_identity_proof(PROFILE, site, 2, extra_urls=["https://flexistore.no/kontakt"])
            self.assertEqual(proof, "exact organisation number printed on https://flexistore.no/kontakt")
            fetched.clear()
            footprint_plus.hard_identity_proof(PROFILE, site, 2, extra_urls=["https://evil.example/kontakt"])
            self.assertNotIn("https://evil.example/kontakt", fetched)


class BuildIndexTests(unittest.TestCase):
    def test_guards(self) -> None:
        import build_discovery_index as b

        universe = [{"organisation_number": n, "name": name} for n, name in
                    [("923795545", "FLEXISTORE NORGE AS"), ("982057892", "99X INCREO AS"),
                     ("916914261", "TRØNDELAG TEATER AS"), ("810034882", "SANDNES ELEKTRISKE AS")]]

        def page(dom, orgs, title="", url=None):
            return {"dom": dom, "url": url or f"https://{dom}/", "kind": "home", "title": title,
                    "orgs": {n: {"labelled": lab, "ctx": ctx} for n, lab, ctx in orgs}}

        pages = [
            page("flexistore.no", [("923795545", True, "Org nr: 923 795 545")]),
            # agency org number on a client's privacy page: dropped, the client's own number kept
            page("trondelag-teater.no", [("916914261", True, "Org.nr 916 914 261"), ("982057892", True, "Org. nr NO 982 057 892")],
                 title="Trøndelag Teater - Personvern"),
            # credit line and unlabelled numbers are never pairs
            page("kunde.no", [("810034882", True, "Nettside levert av Sandnes Elektriske AS org.nr 810034882")]),
            page("annen.no", [("810034882", False, "tlf 810034882")]),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            scan, uni = Path(tmp) / "scan.jsonl", Path(tmp) / "u.jsonl"
            scan.write_text("\n".join(json.dumps(p) for p in pages) + "\n{broken\n", encoding="utf-8")
            uni.write_text("\n".join(json.dumps(u) for u in universe) + "\n", encoding="utf-8")
            pairs, stats = b.build(scan, uni)
        self.assertEqual(sorted(pairs), ["916914261", "923795545"])
        self.assertEqual(stats["broken_lines"], 1)
        self.assertEqual(stats["dropped_second_org_without_name"], 1)
        self.assertEqual(stats["dropped_credit_context"], 1)


if __name__ == "__main__":
    unittest.main()
