import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.batch import terminal_envelope  # noqa: E402
from norway_company_agent.contract import to_contract  # noqa: E402
from norway_company_agent.evidence import evidence  # noqa: E402
from norway_company_agent.workforce import read_fte, zero_contradiction  # noqa: E402

PDF = ROOT / "tests" / "fixtures" / "annual-copy-999592236-2025.pdf"  # Brreg's public copy of AUTONEXT AS's 2025 accounts


class WorkforceTest(unittest.TestCase):
    def test_reads_man_years_from_generated_notes_page(self):
        result = read_fte(PDF.read_bytes(), "999592236")
        self.assertEqual(result["value"], 2)
        self.assertEqual(result["page"], 5)
        self.assertIn("2.00", result["span"])
        self.assertGreaterEqual(result["confidence"], 0.9)

    def test_other_organisation_number_is_never_read(self):
        result = read_fte(PDF.read_bytes(), "123456789")
        self.assertIsNone(result["value"])

    def test_claim_cites_page_and_span(self):
        org = "999592236"
        url = f"https://data.brreg.no/regnskapsregisteret/regnskap/aarsregnskap/kopi/{org}/2025"
        profile = {"organisation_number": org, "name": "AUTONEXT AS", "evidence": {
            "workforce": evidence("workforce", "available", "official_annual_account_copy", url, content_sha256="b" * 64,
                                  value={"full_time_equivalents": 2, "accounts_year": "2025", "page": 5,
                                         "span": "Antall arsverk i regnskapsaret 2.00", "ocr_confidence": 0.984})},
            "run_metrics": {"requests": 1, "latencies_ms": [1], "plus_requests": 0}}
        out = to_contract(terminal_envelope(profile, run_id="t", modules=["workforce"], started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:00:01Z"))
        claim = next(c for c in out["claims"] if c["field"] == "workforce_fte")
        self.assertEqual((claim["value"], claim["availability"], claim["period"]), (2, "available", {"accounts_year": "2025"}))
        ev = next(e for e in out["evidence"] if e["id"] == claim["evidence_ids"][0])
        self.assertEqual(ev["source_url"], url)
        self.assertIn("page 5: Antall arsverk i regnskapsaret 2.00", ev["claim_span"])

    def test_printed_zero_is_withheld_when_wages_were_paid(self):
        def profile(wages, employees=None):
            rec = {"period": {"tilDato": "2025-12-31"}, "wage_costs": wages}
            return {"employees": employees, "evidence": {"financials": {"value": {"records": [rec]}}}}
        self.assertIn("wage costs", zero_contradiction(profile(2361433.0, 9), "2025"))
        self.assertIsNone(zero_contradiction(profile(0.0, 5), "2025"))  # no wages that year: the filed zero stands
        self.assertIsNone(zero_contradiction(profile(22820.0), "2025"))  # board fee level
        self.assertIn("wage costs", zero_contradiction(profile(484925.0), "2025"))
        self.assertIn("registry reports 9", zero_contradiction({"employees": 9, "evidence": {}}, "2025"))
        self.assertIsNone(zero_contradiction({"employees": None, "evidence": {}}, "2025"))

    def test_missing_value_is_not_zero(self):
        profile = {"organisation_number": "999592236", "name": "X AS", "evidence": {
            "workforce": evidence("workforce", "not_found", "official_annual_account_copy", "https://data.brreg.no", note="no field")},
            "run_metrics": {"requests": 1, "latencies_ms": [1], "plus_requests": 0}}
        out = to_contract(terminal_envelope(profile, run_id="t", modules=["workforce"], started_at="2026-01-01T00:00:00Z", completed_at="2026-01-01T00:00:01Z"))
        claim = next(c for c in out["claims"] if c["field"] == "workforce_fte")
        self.assertEqual((claim["value"], claim["availability"]), (None, "not_available"))


if __name__ == "__main__":
    unittest.main()


class PrefetchedCopy(unittest.TestCase):
    def test_copy_is_downloaded_once_and_counted_once(self):
        from unittest import mock
        from norway_company_agent import workforce as wf
        wf._downloads.clear()

        class Resp:
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b"%PDF-1.4 not really"
        with mock.patch.object(wf, "_reserve_history_slot"), mock.patch.object(wf.urllib.request, "urlopen", return_value=Resp()) as get:
            (raw, failed, _, _), used = wf.download_copy("123456789", "2025")   # prefetch: counted here
            self.assertEqual((raw[:4], failed, used), (b"%PDF", None, 1))
            rec, used2 = wf.fetch_workforce("123456789", "2025")              # OCR stage: from memory, not counted again
            self.assertEqual(get.call_count, 1)
            self.assertEqual(used2, 0)
            self.assertEqual(rec["status"], "not_found")  # unreadable copy is not a fact
        wf._downloads.clear()
