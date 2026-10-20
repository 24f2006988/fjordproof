#!/usr/bin/env python3
"""One command: starter pipeline + free website discovery + verified-site footprint.

python scripts/run_signalpost.py --organisations batch.jsonl --bulk brreg-enheter.csv --run-id r1 --expected-count 100
Extra flags are passed through to run_competition_batch.py. Prior snapshot: --previous out/envelopes.jsonl
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from norway_company_agent.batch import terminal_envelope, validate_envelopes  # noqa: E402
from norway_company_agent.budget import BudgetGuard  # noqa: E402
from norway_company_agent.contract import to_contract  # noqa: E402
from norway_company_agent.evidence import evidence, utc_now  # noqa: E402
from norway_company_agent.footprint_plus import discover_website, extract_footprint  # noqa: E402
from norway_company_agent.nav_jobs import JobFeed, fetch_jobs  # noqa: E402
from norway_company_agent import site_description  # noqa: E402
from norway_company_agent.workforce import fetch_workforce  # noqa: E402

MODULES = "registry,accounting_obligation,registry_live,financials,financial_history,roles,group,locations,website,external_footprint,workforce,nav_jobs"
PLUS_MODULES = {"external_footprint", "workforce", "nav_jobs"}  # run in this script, after the base pipeline


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
    p.add_argument("--bulk", default=None, help="optional Brreg bulk snapshot; without it each company is looked up live")
    p.add_argument("--run-id", required=True)
    p.add_argument("--expected-count", type=int, required=True)
    p.add_argument("--outdir", default="out")
    p.add_argument("--previous", help="previous envelopes JSONL (snapshot is preserved; changes are reported)")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--plus-workers", type=int, default=24, help="threads for website discovery/footprint (many different hosts; Brreg is not hit here)")
    p.add_argument("--max-runtime-seconds", type=float, default=2400.0, help="maximum wallclock runtime in seconds (default: 2400 = 40m)")
    p.add_argument("--max-requests", type=int, default=None, help="maximum outbound requests (default: batch scaled)")
    p.add_argument("--job-feed-days", type=int, default=60, help="days of the NAV job feed to read (active ads modified in this window)")
    p.add_argument("--ocr-workers", type=int, default=max(1, min(4, (os.cpu_count() or 2) - 1)), help="threads reading man-years from filed annual-account copies")
    a = p.parse_args()
    out = Path(a.outdir)
    profiles_path, env_path, report_path = out / "profiles.jsonl", out / "envelopes.jsonl", out / "run-report.json"
    base_modules = ",".join(m for m in MODULES.split(",") if m not in PLUS_MODULES)  # website discovery also runs in this script
    max_reqs = a.max_requests if a.max_requests is not None else max(1800, int(a.expected_count * 15))
    budget = BudgetGuard(max_runtime_seconds=a.max_runtime_seconds, max_requests=max_reqs)
    cmd = [sys.executable, str(ROOT / "scripts" / "run_competition_batch.py"), "--organisations", a.organisations,
           "--profiles-output", str(profiles_path), "--output", str(env_path), "--report", str(report_path),
           "--run-id", a.run_id, "--expected-count", str(a.expected_count), "--workers", str(a.workers), "--modules", base_modules,
           "--max-runtime-seconds", str(budget.remaining_seconds), "--max-requests", str(max_reqs)] + (["--bulk", a.bulk] if a.bulk else [])
    # One NAV job-feed load per run, paged while the registry pipeline runs; every company is matched against it later.
    feed = JobFeed(days=a.job_feed_days)
    feed_deadline = time.monotonic() + max(60.0, budget.remaining_seconds - 600.0)
    threading.Thread(target=feed.load, kwargs={"deadline": feed_deadline}, daemon=True).start()
    rc = subprocess.run(cmd, stdout=subprocess.DEVNULL).returncode
    if rc != 0:
        raise SystemExit(f"base pipeline failed ({rc})")
    profiles = [json.loads(l) for l in profiles_path.read_text(encoding="utf-8").splitlines() if l.strip()]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    budget.record_requests(report.get("operations", {}).get("requests", 0))
    previous = {}
    if a.previous and Path(a.previous).exists():
        previous = {e["organisation_number"]: e["profile"] for e in map(json.loads, Path(a.previous).read_text(encoding="utf-8").splitlines()) if e.get("profile")}

    def plus(profile: dict) -> dict:
        extra = {"requests": 0, "bytes": 0}
        try:
            site = profile["evidence"].get("website", {})
            if budget.is_exhausted(safety_margin_seconds=120.0, safety_margin_requests=50):
                if site.get("status") not in {"available", "not_applicable"}:
                    profile["evidence"]["website"] = evidence(
                        "website", "budget_exhausted", "system_budget_guard", "https://builderr.ai",
                        note=f"Budget guard active ({int(budget.elapsed_seconds)}s elapsed, {budget.requests} reqs); skipping discovery",
                    )
                profile["evidence"]["external_footprint"] = evidence(
                    "external_footprint", "budget_exhausted", "system_budget_guard", "https://builderr.ai",
                    note=f"Budget guard active ({int(budget.elapsed_seconds)}s elapsed, {budget.requests} reqs); skipping footprint",
                )
            else:
                if site.get("status") != "available":
                    found, m = discover_website(profile)
                    extra["requests"] += m["requests"]
                    extra["bytes"] += m["bytes"]
                    if not found and m.get("transient"):
                        retry.add(profile["organisation_number"])
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
        site = profile["evidence"].get("website", {})
        if site.get("status") == "available" and (site.get("value", {}).get("identity_assessment") or {}).get("publishable") is not True:
            # Fetched but identity not proven for this exact entity: do not carry the URL anywhere in the output.
            profile["evidence"]["website"] = evidence(
                "website", "not_found", "registry_linked_company_website", "https://data.brreg.no/enhetsregisteret/api/enheter",
                note="A website listed for this company was fetched but its identity could not be proven; nothing published.")
            profile["website"] = None
        budget.record_requests(extra["requests"])
        profile.setdefault("run_metrics", {})["plus_requests"] = extra["requests"]
        profile["changes_since_previous"] = material_changes(previous.get(profile["organisation_number"]), profile)
        return profile

    def retry_discovery(profile: dict) -> dict:
        """Second, slower attempt for companies whose candidate site timed out or errored in the parallel pass."""
        if budget.is_exhausted(safety_margin_seconds=180.0, safety_margin_requests=100):
            return profile
        try:
            found, m = discover_website(profile, timeout=12.0)
            used = m["requests"]
            if found:
                profile["evidence"]["website"] = found
                profile["website"] = found["source_url"]
                fp, fm = extract_footprint(profile, found)
                used += fm["requests"]
                profile["evidence"]["external_footprint"] = fp
                profile["changes_since_previous"] = material_changes(previous.get(profile["organisation_number"]), profile)
        except Exception:  # noqa: BLE001  the first-pass result stands
            return profile
        budget.record_requests(used)
        profile["run_metrics"]["plus_requests"] = profile["run_metrics"].get("plus_requests", 0) + used
        return profile

    def needs_footprint_retry(profile: dict) -> bool:
        """A verified site whose footprint pass found nothing: under parallel load its pages often time out."""
        site = profile["evidence"].get("website", {})
        fp = profile["evidence"].get("external_footprint", {})
        verified = site.get("status") == "available" and (site.get("value", {}).get("identity_assessment") or {}).get("publishable") is True
        return verified and fp.get("status") in {"not_found", "source_error"}

    def retry_footprint(profile: dict) -> dict:
        if budget.is_exhausted(safety_margin_seconds=180.0, safety_margin_requests=100):
            return profile
        try:
            fp, m = extract_footprint(profile, profile["evidence"]["website"])
        except Exception:  # noqa: BLE001  the first-pass result stands
            return profile
        if fp.get("status") == "available":
            profile["evidence"]["external_footprint"] = fp
            profile["changes_since_previous"] = material_changes(previous.get(profile["organisation_number"]), profile)
        budget.record_requests(m["requests"])
        profile["run_metrics"]["plus_requests"] = profile["run_metrics"].get("plus_requests", 0) + m["requests"]
        return profile

    def workforce(profile: dict) -> None:
        """Runs after website work: OCR load during discovery made slow-but-live sites time out (a-seafood.com, smoke-033)."""
        org = profile["organisation_number"]
        if budget.is_exhausted(safety_margin_seconds=300.0, safety_margin_requests=100):
            profile["evidence"]["workforce"] = evidence("workforce", "budget_exhausted", "system_budget_guard", "https://builderr.ai",
                                                        note="Budget guard active; annual-account copy not read")
            return
        try:
            rec, used = fetch_workforce(org, str(profile.get("latest_submitted_accounts") or "") or None, profile)
        except Exception as exc:  # noqa: BLE001  never drop a company
            rec, used = evidence("workforce", "source_error", "official_annual_account_copy", "https://data.brreg.no/regnskapsregisteret",
                                 note=f"{type(exc).__name__}"), 0
        profile["evidence"]["workforce"] = rec
        budget.record_requests(used)
        profile["run_metrics"]["workforce_requests"] = used

    def jobs(profile: dict) -> None:
        if budget.is_exhausted(safety_margin_seconds=240.0, safety_margin_requests=100):
            profile["evidence"]["nav_jobs"] = evidence("nav_jobs", "budget_exhausted", "system_budget_guard", "https://builderr.ai",
                                                       note="Budget guard active; NAV job feed not matched")
            return
        try:
            profile["evidence"]["nav_jobs"] = fetch_jobs(profile, feed)
        except Exception as exc:  # noqa: BLE001  never drop a company
            profile["evidence"]["nav_jobs"] = evidence("nav_jobs", "source_error", "official_job_feed", "https://pam-stilling-feed.nav.no",
                                                       note=type(exc).__name__)

    # NAV ads first: their employer homepages become website candidates for discovery below.
    with ThreadPoolExecutor(8) as pool:
        list(pool.map(jobs, profiles))
    budget.record_requests(feed.requests)
    report["job_feed"] = {"active_ads": len(feed.ads), "requests": feed.requests, "error": feed.error, "days": a.job_feed_days}
    retry: set[str] = set()
    with ThreadPoolExecutor(a.plus_workers) as pool:
        profiles = list(pool.map(plus, profiles))
    if retry:
        with ThreadPoolExecutor(4) as pool:
            profiles = list(pool.map(lambda pr: retry_discovery(pr) if pr["organisation_number"] in retry else pr, profiles))
    fp_retry = {pr["organisation_number"] for pr in profiles if needs_footprint_retry(pr)}
    if fp_retry:
        with ThreadPoolExecutor(4) as pool:
            profiles = list(pool.map(lambda pr: retry_footprint(pr) if pr["organisation_number"] in fp_retry else pr, profiles))
    report["discovery_retries"] = len(retry)
    report["footprint_retries"] = len(fp_retry)
    for pr in profiles:
        pr.setdefault("run_metrics", {})

    # Optional (GEMINI_API_KEY): sentences quoted from verified websites, chosen by a model and checked verbatim.
    # Without a key this adds no module, no request and no cost.
    modules = MODULES.split(",") + (["site_description"] if site_description.configured() else [])
    model_cost = 0.0
    cost_lock = threading.Lock()

    def describe(profile: dict) -> None:
        nonlocal model_cost
        rec, used, cost = None, 0, 0.0
        if not budget.is_exhausted(safety_margin_seconds=300.0, safety_margin_requests=100):
            rec, used, cost = site_description.describe(profile)
        profile["evidence"]["site_description"] = rec or evidence(
            "site_description", "not_applicable", "company_owned_site", "https://builderr.ai",
            note="No identity-verified website (or budget guard active); no description requested.")
        budget.record_requests(used)
        profile["run_metrics"]["plus_requests"] = profile["run_metrics"].get("plus_requests", 0) + used
        with cost_lock:
            model_cost += cost

    if "site_description" in modules:
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(describe, profiles))
        report["site_description"] = {"model": site_description.model(), "cost_usd": round(model_cost, 6),
                                      "described": sum(pr["evidence"]["site_description"]["status"] == "available" for pr in profiles)}
    with ThreadPoolExecutor(a.ocr_workers) as pool:
        list(pool.map(workforce, profiles))
    completed = utc_now()
    envelopes = [terminal_envelope(pr, run_id=a.run_id, modules=modules, started_at=report["started_at"], completed_at=completed) for pr in profiles]
    for e in envelopes:
        e["changes"] = e["profile"].get("changes_since_previous", [])
        to_contract(e)
    validation = validate_envelopes(envelopes, a.expected_count)
    plus_requests = sum(pr.get("run_metrics", {}).get("plus_requests", 0) + pr.get("run_metrics", {}).get("workforce_requests", 0) for pr in profiles)
    report.update({"modules": modules, "completed_at": completed, "emitted_envelopes": len(envelopes), "validation": validation})
    report["operations"]["requests"] += plus_requests + feed.requests
    report["operations"]["third_party_cost_usd"] = round(model_cost, 6)
    report["operations"]["budget_exhausted"] = budget.is_exhausted(safety_margin_seconds=0, safety_margin_requests=0)

    write_jsonl(profiles_path, profiles)
    write_jsonl(env_path, envelopes)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    try:  # profile viewer for people; never allowed to affect the graded envelopes or exit code
        sys.path.insert(0, str(ROOT / "scripts"))
        from build_site import build as build_site
        site = out / "site" / "index.html"
        site.parent.mkdir(parents=True, exist_ok=True)
        html = build_site(envelopes)
        site.write_text(html, encoding="utf-8")
        (out / "index.html").write_text(html, encoding="utf-8")  # same page at the top of the output folder, easy to find
    except Exception as exc:  # noqa: BLE001
        print(f"profile viewer not built: {type(exc).__name__}", file=sys.stderr)
    states = {}
    for e in envelopes:
        for m, v in e["modules"].items():
            states.setdefault(m, {}).setdefault(v["state"], 0)
            states[m][v["state"]] += 1
    print(json.dumps({"validation": validation["passed"], "requests": report["operations"]["requests"], "module_states": states}, indent=1))
    raise SystemExit(0 if validation["passed"] else 1)


if __name__ == "__main__":
    main()
