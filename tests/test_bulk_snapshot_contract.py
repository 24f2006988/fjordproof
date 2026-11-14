"""A registry snapshot row (--bulk) carries every column, empty when the company has no such value.

Regression: an empty share-capital cell made the output-contract step raise at the very end of the run, so the full
results were never written and the registry-only file from the first step stayed at the output path.
"""
import copy
import json
from pathlib import Path

from norway_company_agent.batch import terminal_envelope
from norway_company_agent.contract import _amount, to_contract

FIXTURE = Path(__file__).parent / "fixtures" / "bulk_profile_empty_capital.json"


def _envelope(profile: dict) -> dict:
    envelope = terminal_envelope(profile, run_id="t", modules=list(profile["evidence"]), started_at="2026-11-14T00:00:00Z",
                                 completed_at="2026-11-14T00:00:01Z")
    envelope["changes"] = []
    return envelope


def test_amount_reads_numbers_and_never_invents_zero():
    assert _amount("30000.00") == 30000.0
    assert _amount("30 000,50") == 30000.5
    assert _amount(100) == 100.0
    assert _amount("") is None
    assert _amount("   ") is None
    assert _amount(None) is None
    assert _amount("ukjent") is None


def test_empty_share_capital_in_snapshot_row_is_not_available_not_an_error():
    profile = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert profile["evidence"]["registry"]["value"]["kapital.belop"] == ""
    envelope = to_contract(_envelope(profile)) or None
    claims = (envelope or {}).get("claims") if envelope else None
    if claims is None:  # to_contract changes the envelope in place
        envelope = _envelope(copy.deepcopy(profile))
        to_contract(envelope)
        claims = envelope["claims"]
    capital = [c for c in claims if c["field"] == "share_capital"]
    assert capital and all(c["availability"] != "available" for c in capital)
    assert all(c.get("value") in (None, "", []) for c in capital)


def test_share_capital_in_snapshot_row_is_published_when_present():
    profile = json.loads(FIXTURE.read_text(encoding="utf-8"))
    profile["evidence"]["registry"]["value"]["kapital.belop"] = "30000.00"
    envelope = _envelope(profile)
    to_contract(envelope)
    capital = [c for c in envelope["claims"] if c["field"] == "share_capital"]
    assert capital and capital[0]["availability"] == "available"
