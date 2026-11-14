"""One company's unexpected data, or one stage's unexpected error, must never cost the whole batch its results."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_script():
    spec = importlib.util.spec_from_file_location("run_signalpost_for_tests", ROOT / "scripts" / "run_signalpost.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_guarded_stage_reports_and_continues(capsys):
    script = _load_script()
    reached = []
    with script.guarded("websites"):
        raise RuntimeError("unexpected value")
    reached.append("after")
    assert reached == ["after"]
    assert "stage 'websites' stopped early: RuntimeError: unexpected value" in capsys.readouterr().err


def test_contract_failure_keeps_the_envelope(monkeypatch, capsys):
    script = _load_script()

    def broken(_envelope):
        raise ValueError("could not convert string to float: ''")

    monkeypatch.setattr(script, "to_contract", broken)
    envelope = {"organisation_number": "999999999", "state": "complete", "modules": {}, "profile": {"evidence": {}}}
    script.contract_or_keep(envelope)
    assert envelope["state"] == "complete"
    assert envelope["claims"] == [] and envelope["evidence"] == []
    assert envelope["errors"][-1]["module"] == "contract"
    assert "contract form not built for 999999999" in capsys.readouterr().err


def test_output_file_is_swapped_in_whole(tmp_path):
    script = _load_script()
    target = tmp_path / "out" / "envelopes.jsonl"
    script.write_jsonl(target, [{"organisation_number": "1"}])
    script.write_jsonl(target, [{"organisation_number": "1"}, {"organisation_number": "2"}])
    assert len(target.read_text(encoding="utf-8").splitlines()) == 2
    assert not list(target.parent.glob("*.tmp"))
