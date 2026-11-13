#!/usr/bin/env python3
"""Starter-kit evaluator command. Runs the FULL FjordProof agent (same as scripts/run_signalpost.py).

    uv run python scripts/run_competition_batch.py --organisations batch.jsonl --profiles-output out/profiles.jsonl \
        --output out/envelopes.jsonl --report out/run-report.json --run-id r1 --expected-count 100

Both this command and scripts/run_signalpost.py write the same envelopes: registry facts, verified websites and their
contacts, dated news, NAV job ads, dated public activity, man-years, atomic claims and the cited synthesis.
The registry-only base step lives in scripts/run_base_batch.py.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    p = argparse.ArgumentParser(description="Signalpost batch (full agent)")
    p.add_argument("--organisations", required=True)
    p.add_argument("--bulk", default=None)
    p.add_argument("--output", required=True, help="Terminal envelope JSONL")
    p.add_argument("--profiles-output", default=None)
    p.add_argument("--report", default=None)
    p.add_argument("--run-id", required=True)
    p.add_argument("--expected-count", type=int, default=100)
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--checkpoint-every", type=int, default=25)  # accepted for starter-kit compatibility
    p.add_argument("--resume", action="store_true")  # accepted for starter-kit compatibility
    p.add_argument("--modules", default=None)  # accepted for starter-kit compatibility; the full agent always runs
    p.add_argument("--max-runtime-seconds", type=float, default=None)
    p.add_argument("--max-requests", type=int, default=None)
    p.add_argument("--previous", default=None)
    a = p.parse_args()
    output = Path(a.output)
    cmd = [sys.executable, str(ROOT / "scripts" / "run_signalpost.py"), "--organisations", a.organisations, "--run-id", a.run_id,
           "--expected-count", str(a.expected_count), "--workers", str(a.workers), "--outdir", str(output.parent),
           "--output", str(output)]
    for flag, value in (("--bulk", a.bulk), ("--profiles-output", a.profiles_output), ("--report", a.report), ("--previous", a.previous),
                        ("--max-runtime-seconds", a.max_runtime_seconds), ("--max-requests", a.max_requests)):
        if value is not None:
            cmd += [flag, str(value)]
    raise SystemExit(subprocess.run(cmd).returncode)


if __name__ == "__main__":
    main()
