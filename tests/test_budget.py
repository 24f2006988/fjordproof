import unittest
import time

from norway_company_agent.batch import evidence_terminal_state, terminal_envelope, validate_envelopes
from norway_company_agent.budget import BudgetGuard
from norway_company_agent.contract import to_contract
from norway_company_agent.evidence import evidence


class BudgetGuardTests(unittest.TestCase):
    def test_budget_exhaustion_on_requests(self):
        bg = BudgetGuard(max_runtime_seconds=100.0, max_requests=100)
        self.assertFalse(bg.is_exhausted(safety_margin_requests=10))
        bg.record_requests(91)
        self.assertTrue(bg.is_exhausted(safety_margin_requests=10))

    def test_budget_exhaustion_on_time(self):
        bg = BudgetGuard(max_runtime_seconds=0.1, max_requests=1000)
        time.sleep(0.15)
        self.assertTrue(bg.is_exhausted(safety_margin_seconds=0.0))

    def test_budget_exhausted_terminal_envelope(self):
        profile = {
            "organisation_number": "123456789",
            "evidence": {
                "registry": evidence("registry", "available", "official", "https://example.com"),
                "website": evidence("website", "budget_exhausted", "system_budget_guard", "https://builderr.ai", note="Budget limit"),
                "external_footprint": evidence("external_footprint", "budget_exhausted", "system_budget_guard", "https://builderr.ai", note="Budget limit"),
            },
        }
        self.assertEqual(evidence_terminal_state(profile["evidence"]["website"]), "budget_exhausted")
        env = terminal_envelope(
            profile,
            run_id="test-budget",
            modules=["registry", "website", "external_footprint"],
            started_at="2026-01-01T00:00:00Z",
            completed_at="2026-01-01T00:01:00Z",
        )
        self.assertEqual(env["state"], "complete")
        self.assertEqual(env["modules"]["website"]["state"], "budget_exhausted")
        validation = validate_envelopes([env], 1)
        self.assertTrue(validation["passed"])

        contract = to_contract(env)
        claims = {c["field"]: c for c in contract["claims"]}
        self.assertEqual(claims["official_website"]["availability"], "not_available")
        self.assertEqual(claims["external_footprint"]["availability"], "not_available")
        self.assertTrue(any(err["state"] == "budget_exhausted" for err in contract["errors"]))


if __name__ == "__main__":
    unittest.main()
