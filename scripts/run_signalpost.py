#!/usr/bin/env python3
"""One command: starter pipeline + free website discovery + verified-site footprint.

python scripts/run_signalpost.py --organisations batch.jsonl --bulk brreg-enheter.csv --run-id r1 --expected-count 100
Extra flags are passed through to run_competition_batch.py. Prior snapshot: --previous out/envelopes.jsonl
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.batch import terminal_envelope, validate_envelopes  # noqa: E402
from norway_company_agent.evidence import evidence, utc_now  # noqa: E402
from norway_company_agent.footprint_plus import discover_website, extract_footprint  # noqa: E402

MODULES = "registry,accounting_obligation,registry_live,financials,roles,group,locations,website,external_footprint"


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in rows), encoding="utf-8")


def material_changes(old: dict | None, new: dict) -> list[dict]:
    if not old:
        return []
    changes = []
    fields = ("name", "legal_form", "employees", "bankrupt", "liquidating", "municipality", "industry_code", "website", "latest_submitted_accounts")
    for f in fields:
        if old.get(f) != new.get(f):
            changes.append({"field": f, "before": old.get(f), "after": new.get(f)})
    for mod in ("roles", "locations", "financials", "external_footprint"):
        a = (old.get("evidence", {}).get(mod) or {}).get("value")
        b = (new.get("evidence", {}).get(mod) or {}).get("value")
        if json.dumps(a, sort_keys=True, default=str) != json.dumps(b, sort_keys=True, default=str):
            changes.append({"field": mod, "before_hash": hash(json.dumps(a, sort_keys=True, default=str)) & 0xFFFFFFFF, "changed": True})
    return changes


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--organisations", required=True)
    p.add_argument("--bulk", required=True)
    p.add_argument("--run-id", required=True)
    p.add_argument("--expected-count", type=int, required=True)
    p.add_argument("--outdir", default="out")
    p.add_argument("--previous", help="previous envelopes JSONL (snapshot is preserved; changes are reported)")
    p.add_argument("--workers", type=int, default=8)
    a = p.parse_args()
    out = Path(a.outdir)
    profiles_path, env_path, report_path = out / "profiles.jsonl", out / "envelopes.jsonl", out / "run-report.json"
    base_modules = ",".join(MODULES.split(",")[:-1])
    cmd = [sys.executable, str(ROOT / "scripts" / "run_competition_batch.py"), "--organisations", a.organisations, "--bulk", a.bulk,
           "--profiles-output", str(profiles_path), "--output", str(env_path), "--report", str(report_path),
           "--run-id", a.run_id, "--expected-count", str(a.expected_count), "--workers", str(a.workers), "--modules", base_modules]
    rc = subprocess.run(cmd, stdout=subprocess.DEVNULL).returncode
    if rc != 0:
        raise SystemExit(f"base pipeline failed ({rc})")
    profiles = [json.loads(l) for l in profiles_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    previous = {}
    if a.previous and Path(a.previous).exists():
        previous = {e["organisation_number"]: e["profile"] for e in map(json.loads, Path(a.previous).read_text(encoding="utf-8").splitlines()) if e.get("profile")}

    def plus(profile: dict) -> dict:
        extra = {"requests": 0, "bytes": 0}
        try:
            site = profile["evidence"].get("website", {})
            if site.get("status") != "available":
                found, m = discover_website(profile)
                extra["requests"] += m["requests"]
                extra["bytes"] += m["bytes"]
                if found:
                    profile["evidence"]["website"] = found
                    profile["website"] = found["source_url"]
            fp, m = extract_footprint(profile, profile["evidence"]["website"])
            extra["requests"] += m["requests"]
            extra["bytes"] += m["bytes"]
            profile["evidence"]["external_footprint"] = fp
        except Exception as exc:  # never drop a company
            profile["evidence"]["external_footprint"] = evidence("external_footprint", "source_error", "company_owned_site",
                                                                  "https://builderr.ai", note=f"{type(exc).__name__}: {str(exc)[:150]}")
        profile.setdefault("run_metrics", {})["plus_requests"] = extra["requests"]
        profile["changes_since_previous"] = material_changes(previous.get(profile["organisation_number"]), profile)
        return profile

    with ThreadPoolExecutor(a.workers) as pool:
        profiles = list(pool.map(plus, profiles))
    completed = utc_now()
    envelopes = [terminal_envelope(pr, run_id=a.run_id, modules=MODULES.split(","), started_at=report["started_at"], completed_at=completed) for pr in profiles]
    for e in envelopes:
        e["changes"] = e["profile"].get("changes_since_previous", [])
    validation = validate_envelopes(envelopes, a.expected_count)
    plus_requests = sum(pr.get("run_metrics", {}).get("plus_requests", 0) for pr in profiles)
    report.update({"modules": MODULES.split(","), "completed_at": completed, "emitted_envelopes": len(envelopes), "validation": validation})
    report["operations"]["requests"] += plus_requests
    report["operations"]["third_party_cost_usd"] = 0
    write_jsonl(profiles_path, profiles)
    write_jsonl(env_path, envelopes)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    states = {}
    for e in envelopes:
        for m, v in e["modules"].items():
            states.setdefault(m, {}).setdefault(v["state"], 0)
            states[m][v["state"]] += 1
    print(json.dumps({"validation": validation["passed"], "requests": report["operations"]["requests"], "module_states": states}, indent=1))
    raise SystemExit(0 if validation["passed"] else 1)


if __name__ == "__main__":
    main()
