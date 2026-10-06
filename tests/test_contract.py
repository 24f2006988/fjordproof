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
                                    value={"title": "Eksempel AS Hjemmeside", "description": "Vi leverer programvare"}),
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


if __name__ == "__main__":
    unittest.main()
