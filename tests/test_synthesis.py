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
        self.assertIn("Revenue by year: 2024 NOK 1.0 million, 2025 NOK 1.5 million. Change 2024 to 2025: +50%.", trend)
        self.assertIn("The annual result turned from a profit in 2024 (NOK 50 thousand) to a loss in 2025 (-NOK 20 thousand).", trend)

    def test_unknowns_carry_state_and_reason_and_ambiguous_site_is_not_stated(self):
        s = build_synthesis(envelope())
        by = {u["field"]: u for u in s["unknowns"]}
        self.assertEqual(by["official_website"]["availability"], "ambiguous")
        self.assertIn("could not be proven", by["official_website"]["reason"])
        self.assertIn("opted out of audit", by["auditor"]["reason"])
        self.assertNotIn("online", {x["topic"] for x in s["statements"]})
        self.assertTrue(s["headline"].startswith("EKSEMPEL AS (985589003): private limited company (AS) in OSLO"), s["headline"])
        self.assertIn("annual loss NOK 20 thousand in 2025", s["headline"])
        self.assertIn("revenue +50% year on year", s["headline"])
        self.assertIn("negative equity at the end of 2025", s["headline"])
        self.assertIn("no verified website", s["headline"])

    def test_changes_are_summarised(self):
        s = build_synthesis(envelope(changes=[{"field": "employees", "before": 3, "after": 5}]))
        self.assertTrue(s["changes"].startswith("1 material change since the previous run: employees."))
        base = build_synthesis(envelope())["changes"]
        self.assertIn("baseline", base)
        self.assertIn("revenue +50% from 2024 to 2025", base)

    def test_newer_submitted_filing_is_not_hidden_behind_older_figures(self):
        env = envelope()
        env["claims"].append({"field": "latest_accounts_year", "value": "2026", "availability": "available", "confidence": 0.99,
                              "evidence_ids": ["ev-registry_live"], "source_field": "sisteInnsendteAarsregnskap"})
        s = build_synthesis(env)
        self.assertIn("newer_filing_pending", {x["signal"] for x in s["signals"]})
        texts = " ".join(x["text"] for x in s["statements"])
        self.assertIn("accounts for 2026 as submitted", texts)
        self.assertIn("figures only up to 2025", texts)

    def test_recent_role_changes_are_reported_with_dates(self):
        env = envelope(completed_at="2026-10-17T04:00:00Z")
        env["claims"].append(claim("roles", {"roles": [
            {"name": "Ny Revisor AS", "role": "Revisor", "last_changed": "2025-03-01", "inactive": False},
            {"name": ["Ola", "Nordmann"], "role": "Daglig leder", "last_changed": "2010-01-01", "inactive": False}]}, "ev-roles"))
        s = build_synthesis(env)
        texts = " ".join(x["text"] for x in s["statements"])
        self.assertIn("Revisor Ny Revisor AS (2025-03-01)", texts)
        self.assertNotIn("2010-01-01", texts)
        self.assertIn("role registrations changed", s["changes"])

    def test_assessment_restates_cited_facts_only(self):
        s = build_synthesis(envelope())
        a = next(x for x in s["statements"] if x["topic"] == "assessment")
        self.assertIn("rule-based", a["text"])
        self.assertTrue(a["evidence_ids"])

    def test_missing_registry_gives_honest_headline(self):
        s = build_synthesis({"organisation_number": "999999999", "claims": [], "changes": []})
        self.assertEqual(s["statements"], [])
        self.assertIn("no registry record", s["headline"])


if __name__ == "__main__":
    unittest.main()
