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


if __name__ == "__main__":
    unittest.main()
