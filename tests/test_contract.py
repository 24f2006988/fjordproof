import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.batch import terminal_envelope  # noqa: E402
from norway_company_agent.contract import to_contract  # noqa: E402
from norway_company_agent.evidence import evidence  # noqa: E402

STATES = {"available", "not_available", "blocked", "not_applicable", "ambiguous", "failed"}


class ContractAdapterTest(unittest.TestCase):
    def make(self, website_status):
        profile = {
            "organisation_number": "123456789", "name": "Eksempel AS", "legal_form": "AS", "employees": None,
            "bankrupt": False, "liquidating": False, "municipality": "OSLO", "industry_code": "62.010", "industry_label": "Programmering",
            "evidence": {
                "registry_live": evidence("registry_live", "available", "official_registry_live", "https://data.brreg.no/enhetsregisteret/api/enheter/123456789", value={"a": 1}),
                "website": evidence("website", website_status, "discovered_company_website", "https://example.no/", value={"x": 1} if website_status == "available" else None),
            },
            "run_metrics": {"requests": 2, "latencies_ms": [10, 20], "plus_requests": 1},
        }
        env = terminal_envelope(profile, run_id="t", modules=["registry_live", "website"], started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:00:01Z")
        return to_contract(env)

    def test_shape_and_evidence_links(self):
        env = self.make("available")
        for key in ("run", "claims", "evidence", "changes", "errors", "operations"):
            self.assertIn(key, env)
        ids = {e["id"] for e in env["evidence"]}
        for claim in env["claims"]:
            self.assertIn(claim["availability"], STATES)
            self.assertTrue(set(claim["evidence_ids"]) <= ids)
            if claim["availability"] == "available":
                self.assertTrue(claim["evidence_ids"])
        self.assertEqual(env["operations"], {"requests": 3, "runtime_ms": 30, "third_party_cost_usd": 0})
        self.assertEqual(env["run"]["terminal_status"], "completed")

    def test_missing_values_are_not_zero_filled(self):
        env = self.make("not_found")
        claims = {c["field"]: c for c in env["claims"]}
        self.assertEqual(claims["employees"]["availability"], "not_available")
        self.assertIsNone(claims["employees"]["value"])
        self.assertEqual(claims["official_website"]["availability"], "not_available")

    def test_unproven_website_identity_is_never_published(self):
        env = self.make("available")  # fixture site has no identity_assessment => not proven
        claims = {c["field"]: c for c in env["claims"]}
        self.assertEqual(claims["official_website"]["availability"], "ambiguous")
        self.assertIsNone(claims["official_website"]["value"])
        self.assertEqual(claims["official_website"]["confidence"], 0.0)
        self.assertEqual(claims["website_title"]["availability"], "ambiguous")

    def test_granular_claims_coverage(self):
        profile = {
            "organisation_number": "123456789", "name": "Eksempel AS", "legal_form": "AS", "employees": 5,
            "bankrupt": False, "liquidating": False, "municipality": "OSLO", "industry_code": "62.010", "industry_label": "Programmering",
            "evidence": {
                "registry_live": evidence("registry_live", "available", "official_registry_live", "https://data.brreg.no/enhetsregisteret/api/enheter/123456789",
                                         value={"business_address": {"adresse": ["Storgata 1"], "postnummer": "0182", "poststed": "OSLO", "kommune": "OSLO", "land": "Norge"},
                                                "email": "post@eksempel.no", "phone": "22000000", "share_capital": {"amount": 30000.0, "currency": "NOK"},
                                                "incorporation_date": "2020-01-01", "vat_registered": True}),
                "financials": evidence("financials", "available", "official_annual_accounts", "https://data.brreg.no/regnskapsregisteret/regnskap/123456789",
                                       value={"records": [{"revenue": 1000000.0, "operating_result": 100000.0, "profit_before_tax": 95000.0, "annual_result": 80000.0, "assets": 500000.0, "equity": 200000.0, "debt": 300000.0, "period": {"tilDato": "2024-12-31"}}]}),
                "roles": evidence("roles", "available", "official_roles", "https://data.brreg.no/enhetsregisteret/api/enheter/123456789/roller",
                                  value={"roles": [{"name": "Ola Nordmann", "role_code": "DAGL"}, {"name": "Kari Nordmann", "role_code": "LEDE"}, {"name": "Per Hansen", "role_code": "MEDL"}]}),
                "locations": evidence("locations", "available", "official_subunits", "https://data.brreg.no/enhetsregisteret/api/underenheter?overordnetEnhet=123456789",
                                      value={"locations": [{"organisation_number": "987654321", "name": "Eksempel Avd"}]}),
                "website": evidence("website", "available", "discovered_company_website", "https://eksempel.no/",
                                    value={"title": "Eksempel AS Hjemmeside", "description": "Vi leverer programvare", "identity_assessment": {"publishable": True, "score": 1.0}}),
                "external_footprint": evidence("external_footprint", "available", "company_owned_site", "https://eksempel.no/",
                                               value={"careers_urls": ["https://eksempel.no/karriere"], "news_pages": ["https://eksempel.no/nyheter"], "social_links": [{"platform": "linkedin", "url": "https://linkedin.com/company/eksempel"}], "contact": {"email": "hei@eksempel.no"}}),
            },
            "run_metrics": {"requests": 6, "latencies_ms": [10, 20], "plus_requests": 2},
        }
        env = terminal_envelope(profile, run_id="t", modules=["registry_live", "financials", "roles", "locations", "website", "external_footprint"], started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:00:01Z")
        contract = to_contract(env)
        claims = {c["field"]: c for c in contract["claims"]}
        self.assertEqual(claims["registered_office"]["value"]["postcode"], "0182")
        self.assertEqual(claims["registered_email"]["value"], "post@eksempel.no")
        self.assertEqual(claims["registered_phone"]["value"], "22000000")
        self.assertEqual(claims["revenue"]["value"], 1000000.0)
        self.assertEqual(claims["profit_before_tax"]["value"], 95000.0)
        self.assertEqual(claims["ceo"]["value"], "Ola Nordmann")
        self.assertEqual(claims["board_chair"]["value"], "Kari Nordmann")
        self.assertEqual(claims["board_members"]["value"], ["Per Hansen"])
        self.assertEqual(claims["registered_workplaces_count"]["value"], 1)
        self.assertEqual(len(claims["registered_workplaces"]["value"]), 1)
        self.assertEqual(claims["site_email"]["value"], "hei@eksempel.no")
        self.assertEqual(claims["website_title"]["value"], "Eksempel AS Hjemmeside")
        self.assertEqual(claims["careers_urls"]["value"], ["https://eksempel.no/karriere"])
        self.assertEqual(claims["social_profiles"]["value"], [{"platform": "linkedin", "url": "https://linkedin.com/company/eksempel"}])

    def _care_of_env(self, reasons):
        profile = {
            "organisation_number": "816559472", "name": "TEAMTEC WASTE TO ENERGY AS", "legal_form": "AS", "bankrupt": False, "liquidating": False,
            "evidence": {
                "registry_live": evidence("registry_live", "available", "official_registry_live", "https://data.brreg.no/enhetsregisteret/api/enheter/816559472",
                                          value={"business_address": {"adresse": ["c/o TeamTec AS", "Nyvei 41"], "postnummer": "4900", "poststed": "TVEDESTRAND"}}),
                "website": evidence("website", "available", "discovered_company_website", "https://teamtec.no/",
                                    value={"title": "TeamTec", "identity_assessment": {"publishable": True, "score": 0.9, "reasons": reasons}}),
                "external_footprint": evidence("external_footprint", "available", "company_owned_site", "https://teamtec.no/",
                                               value={"social_links": [{"platform": "linkedin", "url": "https://linkedin.com/company/teamtec"}], "contact": {"email": "post@teamtec.no"}}),
            },
            "run_metrics": {"requests": 3, "latencies_ms": [10], "plus_requests": 1},
        }
        env = terminal_envelope(profile, run_id="t", modules=["registry_live", "website", "external_footprint"], started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:00:01Z")
        return {c["field"]: c for c in to_contract(env)["claims"]}

    def test_care_of_entity_site_needs_exact_org_number_and_its_facts_follow_the_site(self):
        claims = self._care_of_env(["registered street address and postcode printed on https://teamtec.no/"])
        self.assertEqual(claims["official_website"]["availability"], "ambiguous")
        self.assertIsNone(claims["official_website"]["value"])
        self.assertNotEqual(claims["social_profiles"]["availability"], "available")
        self.assertNotEqual(claims["site_email"]["availability"], "available")
        exact = self._care_of_env(["exact organisation number appears in homepage identity evidence"])
        self.assertEqual(exact["official_website"]["availability"], "available")
        self.assertEqual(exact["social_profiles"]["availability"], "available")

    def test_financial_claims_use_latest_period_not_first_record(self):
        profile = {
            "organisation_number": "123456789", "name": "Eksempel AS", "legal_form": "AS", "bankrupt": False, "liquidating": False,
            "evidence": {"financials": evidence("financials", "available", "official_annual_accounts", "https://data.brreg.no/regnskapsregisteret/regnskap/123456789",
                value={"records": [
                    {"revenue": 1.0, "assets": 10.0, "period": {"fraDato": "2023-01-01", "tilDato": "2023-12-31"}},
                    {"revenue": 3.0, "assets": 30.0, "period": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"}},
                    {"revenue": 2.0, "assets": 20.0, "period": {"fraDato": "2024-01-01", "tilDato": "2024-12-31"}}]})},
            "run_metrics": {"requests": 1, "latencies_ms": [1], "plus_requests": 0},
        }
        env = terminal_envelope(profile, run_id="t", modules=["financials"], started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:00:01Z")
        claims = {c["field"]: c for c in to_contract(env)["claims"]}
        self.assertEqual(claims["revenue"]["value"], 3.0)
        self.assertEqual(claims["total_assets"]["value"], 30.0)
        self.assertEqual(claims["latest_accounts_year"]["value"], "2025")
        self.assertEqual(claims["revenue"]["period"]["tilDato"], "2025-12-31")

    def test_financial_claims_state_scope_and_never_mix_group_accounts(self):
        recs = [
            {"record_id": 7, "account_type": "KONSERN", "currency": "NOK", "revenue": 900.0, "period": {"fraDato": "2024-01-01", "tilDato": "2024-12-31"}},
            {"record_id": 8, "account_type": "SELSKAP", "currency": "NOK", "revenue": 100.0, "period": {"fraDato": "2024-01-01", "tilDato": "2024-12-31"}},
            {"record_id": 9, "account_type": "KONSERN", "currency": "NOK", "revenue": 950.0, "period": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"}},
        ]
        profile = {
            "organisation_number": "123456789", "name": "Eksempel AS", "legal_form": "AS", "bankrupt": False, "liquidating": False,
            "evidence": {"financials": evidence("financials", "available", "official_annual_accounts", "https://data.brreg.no/regnskapsregisteret/regnskap/123456789",
                value={"records": recs})},
            "run_metrics": {"requests": 1, "latencies_ms": [1], "plus_requests": 0},
        }
        env = terminal_envelope(profile, run_id="t", modules=["financials"], started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:00:01Z")
        out = to_contract(env)
        claims = {c["field"]: c for c in out["claims"]}
        rev = claims["revenue"]
        self.assertEqual((rev["value"], rev["scope"], rev["currency"], rev["source_record_id"]), (100.0, "SELSKAP", "NOK", 8))
        self.assertEqual(rev["period"]["tilDato"], "2024-12-31")
        self.assertEqual(claims["latest_accounts_year"]["period"]["tilDato"], "2024-12-31")
        ev = {e["id"]: e for e in out["evidence"]}
        self.assertEqual(rev["evidence_ids"], ["ev-financials-latest-revenue"])
        span = ev["ev-financials-latest-revenue"]["claim_span"]
        self.assertTrue(span.startswith("Brreg Regnskapsregisteret sumDriftsinntekter = 100.0;"))
        self.assertIn("record 8", span)
        self.assertIn("SELSKAP", span)
        self.assertIn("record 8", ev["ev-financials-latest"]["claim_span"])
        fin = next(s["text"] for s in out["synthesis"]["statements"] if s["topic"] == "finances")
        self.assertIn("company accounts (SELSKAP)", fin)
        self.assertNotIn("950", fin)

    def test_claim_spans_quote_the_source_value_and_filings_are_listed(self):
        org = "123456789"
        profile = {
            "organisation_number": org, "name": "Eksempel AS", "legal_form": "AS", "bankrupt": False, "liquidating": False,
            "evidence": {
                "registry_live": evidence("registry_live", "available", "official_registry_live", f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}",
                    value={"email": "post@eksempel.no"}, content_sha256="a" * 64),
                "roles": evidence("roles", "available", "official_roles", f"https://data.brreg.no/enhetsregisteret/api/enheter/{org}/roller",
                    value={"roles": [{"name": "Kari Nordmann", "role_code": "DAGL", "role": "Daglig leder", "last_changed": "2024-01-02", "inactive": False}]}),
                "financial_history": evidence("financial_history", "available", "official_annual_account_copies",
                    f"https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/aar",
                    value={"years": ["2024", "2025"], "pdfs": [{"year": "2025", "url": "u25"}, {"year": "2024", "url": "u24"}]}),
            },
            "run_metrics": {"requests": 3, "latencies_ms": [1], "plus_requests": 0},
        }
        env = terminal_envelope(profile, run_id="t", modules=["registry_live", "roles", "financial_history"], started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:00:01Z")
        out = to_contract(env)
        claims = {c["field"]: c for c in out["claims"]}
        ev = {e["id"]: e for e in out["evidence"]}
        self.assertEqual(ev[claims["name"]["evidence_ids"][0]]["claim_span"], f'Brreg Enhetsregisteret {org}: navn = "Eksempel AS"')
        self.assertIn('epostadresse = "post@eksempel.no"', ev[claims["registered_email"]["evidence_ids"][0]]["claim_span"])
        self.assertEqual(ev[claims["name"]["evidence_ids"][0]]["content_sha256"], "a" * 64)  # same source snapshot as the base record
        self.assertIn('Daglig leder (DAGL) = "Kari Nordmann"', ev[claims["ceo"]["evidence_ids"][0]]["claim_span"])
        filings = claims["annual_account_filings"]
        self.assertEqual(filings["availability"], "available")
        self.assertEqual([f["year"] for f in filings["value"]], ["2025", "2024"])
        self.assertIn('aar = ["2024", "2025"]', ev[filings["evidence_ids"][0]]["claim_span"])
        self.assertTrue(ev[filings["evidence_ids"][0]]["source_url"].endswith(f"/kopi/{org}/aar"))


if __name__ == "__main__":
    unittest.main()
