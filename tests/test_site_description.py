import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent import site_description  # noqa: E402
from norway_company_agent.batch import terminal_envelope  # noqa: E402
from norway_company_agent.contract import to_contract  # noqa: E402
from norway_company_agent.evidence import evidence  # noqa: E402

PAGE = ("Velkommen til Eksempel AS.   Vi leverer regnskapstjenester og lønn til små bedrifter i Oslo.\n"
        "Kontakt oss på post@eksempel.no. Vi har hjulpet kunder med bokføring siden 1999.")


def profile(publishable=True):
    return {
        "organisation_number": "123456789", "name": "Eksempel AS", "legal_form": "AS", "bankrupt": False, "liquidating": False,
        "evidence": {
            "registry_live": evidence("registry_live", "available", "official_registry_live", "https://data.brreg.no/enhetsregisteret/api/enheter/123456789", value={"a": 1}),
            "website": evidence("website", "available", "discovered_company_website", "https://eksempel.no/", content_sha256="ab" * 32,
                                value={"final_url": "https://eksempel.no/", "main_text_excerpt": PAGE,
                                       "identity_assessment": {"publishable": publishable, "score": 1.0}}),
        },
        "run_metrics": {"requests": 1, "latencies_ms": [1]},
    }


class SiteDescriptionTest(unittest.TestCase):
    def test_off_without_key_makes_no_request(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": ""}):
            called = []
            rec, used, cost = site_description.describe(profile(), caller=lambda *a: called.append(a))
        self.assertEqual((rec, used, cost, called), (None, 0, 0.0, []))

    def test_only_verbatim_sentences_survive(self):
        got = site_description.verbatim_sentences([
            "Vi leverer regnskapstjenester og lønn til små bedrifter i Oslo.",            # verbatim
            "Vi leverer regnskapstjenester og lønn til 500 bedrifter i Oslo.",            # changed number
            "We deliver accounting services to small businesses in Oslo.",                # translated
            "Vi har   hjulpet kunder med bokføring siden 1999.",                          # whitespace only differs
            "Vi leverer regnskapstjenester og lønn til små bedrifter i Oslo.",            # duplicate
        ], PAGE)
        self.assertEqual(got, ["Vi leverer regnskapstjenester og lønn til små bedrifter i Oslo.",
                               "Vi har hjulpet kunder med bokføring siden 1999."])

    def test_unverified_site_is_never_sent(self):
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k"}):
            rec, used, _ = site_description.describe(profile(publishable=False), caller=lambda *a: self.fail("called"))
        self.assertEqual((rec, used), (None, 0))

    def test_claim_quotes_page_and_costs_are_reported(self):
        fake = lambda prompt, timeout: (["Vi leverer regnskapstjenester og lønn til små bedrifter i Oslo.", "Invented sentence about awards won."],
                                        {"input": 2000, "output": 50})
        with mock.patch.dict(os.environ, {"GEMINI_API_KEY": "k"}):
            rec, used, cost = site_description.describe(profile(), caller=fake)
        self.assertEqual(used, 1)
        self.assertAlmostEqual(cost, (2000 * 0.10 + 50 * 0.40) / 1e6)
        self.assertEqual(rec["value"]["sentences"], ["Vi leverer regnskapstjenester og lønn til små bedrifter i Oslo."])
        pr = profile()
        pr["evidence"]["site_description"] = rec
        env = to_contract(terminal_envelope(pr, run_id="t", modules=["registry_live", "website", "site_description"],
                                            started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:00:01Z"))
        claim = next(c for c in env["claims"] if c["field"] == "company_description")
        self.assertEqual(claim["availability"], "available")
        spans = {e["id"]: e["claim_span"] for e in env["evidence"]}
        self.assertTrue(all(spans[i] in " ".join(PAGE.split()) for i in claim["evidence_ids"]))
        self.assertEqual(claim["raw_value"], rec["value"]["sentences"])
        self.assertGreater(env["operations"]["third_party_cost_usd"], 0)
        brief = next(s for s in env["synthesis"]["sections"] if s["label"] == "Company brief")
        self.assertIn("Its verified website says", brief["text"])

    def test_keyless_description_quotes_the_site_without_a_model(self):
        pr = profile()
        pr["evidence"]["website"]["value"]["main_text_excerpt"] = (
            "Meny Hjem Om oss Kontakt\nVi godtar informasjonskapsler for å gi deg en bedre opplevelse på nettstedet vårt i dag.\n"
            "Eksempel AS leverer regnskapstjenester, lønn og rådgivning til små og mellomstore bedrifter i hele Oslo-området. "
            "Vi har lang erfaring og fast kontaktperson for hver kunde.")
        env = to_contract(terminal_envelope(pr, run_id="t", modules=["registry_live", "website"],
                                            started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:00:01Z"))
        claim = next(c for c in env["claims"] if c["field"] == "company_description")
        self.assertTrue(claim["value"].startswith("Eksempel AS leverer regnskapstjenester"))
        self.assertEqual(claim["method"], "homepage paragraph naming the company")
        span = next(e["claim_span"] for e in env["evidence"] if e["id"] == claim["evidence_ids"][0])
        self.assertEqual(span, claim["value"])
        self.assertEqual(env["operations"]["third_party_cost_usd"], 0)
        self.assertNotIn("language model", env["synthesis"]["method"])

    def test_no_description_from_an_unproven_site(self):
        env = to_contract(terminal_envelope(profile(publishable=False), run_id="t", modules=["registry_live", "website"],
                                            started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:00:01Z"))
        self.assertFalse(any(c["field"] == "company_description" and c["availability"] == "available" for c in env["claims"]))


if __name__ == "__main__":
    unittest.main()
