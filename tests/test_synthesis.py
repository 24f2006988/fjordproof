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

    def test_one_person_as_general_manager_and_chair_is_named_once(self):
        env = envelope()
        env["claims"] = [c for c in env["claims"] if c["field"] != "ceo"] + [
            claim("ceo", "Kari Nordmann", "ev-roles"), claim("board_chair", "Kari Nordmann", "ev-roles")]
        s = build_synthesis(env)
        lead = next(x["text"] for x in s["statements"] if x["text"].startswith("Registered roles"))
        self.assertIn("general manager and board chair Kari Nordmann", lead)
        self.assertEqual(lead.count("Kari Nordmann"), 1)
        self.assertIn("in Oslo", s["headline"])
        self.assertNotIn("current roles in total", s["conclusion"])  # the two roles of one person are all the roles
        self.assertNotIn("online", {x["topic"] for x in s["statements"]})
        self.assertTrue(s["headline"].startswith("EKSEMPEL AS (985589003): private limited company (AS) in Oslo"), s["headline"])
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

    def test_verified_site_without_jobs_never_also_says_no_site(self):
        env = envelope()
        env["claims"] = [c for c in env["claims"] if c["field"] != "official_website"] + [
            claim("official_website", "https://eksempel.no/", "ev-website"), claim("job_postings", None, "ev-nav_jobs")]
        s = build_synthesis(env)
        self.assertIn("A company-owned website is verified (https://eksempel.no/)", s["conclusion"])
        self.assertNotIn("No company-owned website could be verified", s["conclusion"])

    def test_what_changed_and_what_is_unknown_are_cited_sections(self):
        env = envelope()
        env["evidence"] = [{"id": i} for i in ("ev-registry_live", "ev-financials", "ev-roles", "ev-website", "ev-nav_jobs")]
        by = {x["label"]: x for x in build_synthesis(env)["sections"]}
        self.assertIn("first observation", by["What changed"]["text"])
        self.assertIn("revenue +50% from 2024 to 2025", by["What changed"]["text"])
        self.assertIn("ev-financials", by["What changed"]["evidence_ids"])
        self.assertIn("auditor (the company has opted out of audit", by["What is unknown"]["text"])
        self.assertTrue(by["What is unknown"]["evidence_ids"])
        self.assertIn("ev-nav_jobs", by["Working here"]["evidence_ids"])
        quiet = envelope()
        quiet["claims"] = [c for c in quiet["claims"] if c["field"] != "annual_accounts"]
        self.assertIn("No changes found in the sources checked", build_synthesis(quiet)["changes"])

    def test_missing_registry_gives_honest_headline(self):
        s = build_synthesis({"organisation_number": "999999999", "claims": [], "changes": []})
        self.assertEqual(s["statements"], [])
        self.assertIn("no registry record", s["headline"])


if __name__ == "__main__":
    unittest.main()


class ConclusionAndUnknownSources(unittest.TestCase):
    def with_evidence(self, env, ids=("ev-registry_live", "ev-financials", "ev-roles", "ev-fp-dated_activity-9")):
        env["evidence"] = [{"id": i} for i in ids]
        return env

    def test_conclusion_restates_exact_figures_and_cites_evidence(self):
        s = build_synthesis(self.with_evidence(envelope()))
        self.assertTrue(s["conclusion"].startswith("EKSEMPEL AS (985589003) is a private limited company (AS) in Oslo"))
        self.assertIn("an annual loss of NOK 20 thousand", s["conclusion"])
        self.assertIn("revenue +50% from 2024", s["conclusion"])
        first = s["statements"][0]
        self.assertEqual(first["topic"], "conclusion")
        self.assertTrue(first["evidence_ids"])

    def test_unknowns_cite_only_evidence_present_in_the_envelope(self):
        env = self.with_evidence(envelope())
        s = build_synthesis(env)
        known = {e["id"] for e in env["evidence"]}
        by = {u["field"]: u for u in s["unknowns"]}
        self.assertEqual(by["employees"]["evidence_ids"], ["ev-registry_live"])
        self.assertEqual(by["ceo"]["evidence_ids"], ["ev-roles"])
        for u in s["unknowns"]:
            self.assertTrue(u["evidence_ids"], u)
            self.assertTrue(set(u["evidence_ids"]) <= known, u)

    def test_negative_base_year_gives_amounts_not_a_percentage(self):
        env = envelope()
        acc = next(c for c in env["claims"] if c["field"] == "annual_accounts")["value"]["records"]
        acc[0]["revenue"] = -100_000.0
        s = build_synthesis(self.with_evidence(env))
        trend = " ".join(x["text"] for x in s["statements"] if x["topic"] == "trend")
        self.assertIn("Change 2024 to 2025: from -NOK 100 thousand to NOK 1.5 million.", trend)
        self.assertNotIn("revenue_growth", {x["signal"] for x in s["signals"]})

    def test_conclusion_never_calls_figures_newest_filed_when_a_newer_filing_is_registered(self):
        env = envelope()
        env["claims"].append({"field": "latest_accounts_year", "value": "2026", "availability": "available", "confidence": 0.99,
                              "evidence_ids": ["ev-registry_live"], "source_field": "sisteInnsendteAarsregnskap"})
        s = build_synthesis(self.with_evidence(env))
        self.assertNotIn("newest filed", s["conclusion"])
        self.assertIn("The newest figures in the accounts register", s["conclusion"])
        self.assertIn("accounts for 2026 are registered as submitted but their figures are not yet available", s["conclusion"])


class FiveAreas(unittest.TestCase):
    def test_sections_answer_the_brief_questions_with_evidence(self):
        env = envelope()
        env["claims"] += [claim("registered_activity", "Butikk i kjeden Eksempel."), claim("workforce_fte", 2.0, "ev-workforce"),
                          claim("board_chair", "Kari Nordmann", "ev-roles")]
        env["claims"][[c["field"] for c in env["claims"]].index("workforce_fte")]["period"] = {"accounts_year": "2025"}
        s = build_synthesis(env)
        self.assertEqual([x["label"] for x in s["sections"]],
                         ["Company brief", "Latest financials", "Who runs it?", "Working here", "Recent activity",
                          "What changed", "What is unknown"])
        by = {x["label"]: x for x in s["sections"]}
        self.assertIn('"Butikk i kjeden Eksempel"', by["Company brief"]["text"])
        self.assertIn("ev-registry_live", by["Company brief"]["evidence_ids"])
        self.assertIn("2 man-years", by["Working here"]["text"])
        self.assertIn("2026-09-01", by["Recent activity"]["text"])
        self.assertIn("Kari Nordmann", s["conclusion"])
        # man-years are known, so the employee count is not listed as an unknown
        self.assertNotIn("employees", [u["field"] for u in s["unknowns"]])

    def test_operating_margin_is_computed_from_filed_figures(self):
        env = envelope()
        env["claims"][[c["field"] for c in env["claims"]].index("annual_accounts")]["value"]["records"][-1]["operating_result"] = 150_000.0
        s = build_synthesis(env)
        self.assertTrue(any("Operating margin 10.0% in 2025" in x["text"] for x in s["statements"]))

    def test_unanswered_area_says_so_and_still_cites_the_checked_source(self):
        env = envelope()
        env["claims"] = [c for c in env["claims"] if c["field"] != "news_items"]
        by = {x["label"]: x for x in build_synthesis(env)["sections"]}
        self.assertFalse(by["Recent activity"]["answered"])
        self.assertEqual(by["Recent activity"]["text"], "No dated public activity was found.")
