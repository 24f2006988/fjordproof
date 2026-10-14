import unittest

from norway_company_agent.synthesis import build_synthesis


def claim(field, value, ev="ev-registry_live", availability=None):
    present = value is not None
    return {"field": field, "value": value, "availability": availability or ("available" if present else "not_available"),
            "confidence": 0.99 if present else 0.0, "evidence_ids": [ev] if present else []}


def envelope(**extra):
    accounts = {"records": [
        {"period": {"fraDato": "2024-01-01", "tilDato": "2024-12-31"}, "revenue": 1_000_000.0, "annual_result": 50_000.0, "equity": 200_000.0},
        {"period": {"fraDato": "2025-01-01", "tilDato": "2025-12-31"}, "revenue": 1_500_000.0, "annual_result": -20_000.0, "equity": -5_000.0},
    ]}
    claims = [
        claim("name", "EKSEMPEL AS"), claim("legal_form", "AS"), claim("municipality", "OSLO"),
        claim("insolvency_status", {"bankrupt": False, "liquidating": False}),
        claim("annual_accounts", accounts, "ev-financials"), claim("employees", None), claim("ceo", None, "ev-roles"),
        claim("auditor", None, "ev-roles"), claim("audit_exemption", "2022-05-19"),
        claim("official_website", None, "ev-website", availability="ambiguous"),
        claim("news_items", [{"title": "Ny avtale", "url": "https://eksempel.no/nyheter/ny-avtale", "published_date": "2026-09-01"}], "ev-fp-dated_activity-9"),
    ]
    return {"organisation_number": "985589003", "claims": claims, "changes": [], **extra}


class Synthesis(unittest.TestCase):
    def test_every_statement_and_signal_cites_evidence(self):
        s = build_synthesis(envelope())
        self.assertTrue(s["statements"])
        for item in s["statements"] + s["signals"]:
            self.assertTrue(item["evidence_ids"], item)

    def test_financial_signals_and_trend_come_from_filed_accounts(self):
        s = build_synthesis(envelope())
        codes = {x["signal"] for x in s["signals"]}
        self.assertEqual(codes, {"loss_making", "negative_equity", "revenue_growth", "recent_news"})
        trend = [x["text"] for x in s["statements"] if x["topic"] == "trend"]
        self.assertIn("Revenue changed +50% from 2024 (NOK 1.0 million) to 2025 (NOK 1.5 million).", trend)
        self.assertIn("The annual result turned from a profit in 2024 to a loss in 2025.", trend)

    def test_unknowns_carry_state_and_reason_and_ambiguous_site_is_not_stated(self):
        s = build_synthesis(envelope())
        by = {u["field"]: u for u in s["unknowns"]}
        self.assertEqual(by["official_website"]["availability"], "ambiguous")
        self.assertIn("could not be proven", by["official_website"]["reason"])
        self.assertIn("opted out of audit", by["auditor"]["reason"])
        self.assertNotIn("online", {x["topic"] for x in s["statements"]})
        self.assertTrue(s["headline"].startswith("EKSEMPEL AS (organisation number 985589003) is a private limited company (AS) in OSLO"))

    def test_changes_are_summarised(self):
        s = build_synthesis(envelope(changes=[{"field": "employees", "before": 3, "after": 5}]))
        self.assertEqual(s["changes"], "1 material change since the previous run: employees.")
        self.assertIn("baseline", build_synthesis(envelope())["changes"])

    def test_missing_registry_gives_honest_headline(self):
        s = build_synthesis({"organisation_number": "999999999", "claims": [], "changes": []})
        self.assertEqual(s["statements"], [])
        self.assertIn("no registry record", s["headline"])


if __name__ == "__main__":
    unittest.main()
