"""Adapter: add the OUTPUT_CONTRACT.md fields (run, claims, evidence, errors, operations) to a native envelope."""
from __future__ import annotations

import hashlib
import json
from typing import Any

AVAILABILITY = {
    "available": "available",
    "not_found": "not_available",
    "not_applicable": "not_applicable",
    "blocked": "blocked",
    "source_error": "failed",
    "not_fetched": "failed",
}

# module -> (claim field, confidence when available)
MODULE_CLAIMS = {
    "financials": ("annual_accounts", 0.99),
    "roles": ("roles", 0.99),
    "group": ("group_structure", 0.99),
    "locations": ("locations", 0.99),
    "website": ("official_website", 0.9),
    "external_footprint": ("external_footprint", 0.8),
}


def _sha(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")).hexdigest()


def _span(record: dict[str, Any], default: str) -> str:
    text = str(record.get("note") or "") or default
    return text[:240]


def to_contract(envelope: dict[str, Any]) -> dict[str, Any]:
    profile = envelope["profile"]
    records: dict[str, dict[str, Any]] = profile.get("evidence", {})
    org = envelope["organisation_number"]

    evidence = []
    for name, rec in records.items():
        evidence.append(
            {
                "id": f"ev-{name}",
                "source_url": rec.get("source_url"),
                "source_class": rec.get("source_class"),
                "retrieved_at": rec.get("retrieved_at"),
                "content_sha256": rec.get("content_sha256") or _sha(rec.get("value")),
                "claim_span": _span(rec, f"{name} for organisation number {org}"),
            }
        )

    claims = []
    # Identity claims come from the live registry record when available, else the bulk snapshot.
    ident = "registry_live" if records.get("registry_live", {}).get("status") == "available" else "registry"
    ident_ok = records.get(ident, {}).get("status") == "available"

    def scalar(field: str, value: Any) -> None:
        present = ident_ok and value not in (None, "")
        claims.append(
            {
                "field": field,
                "value": value if present else None,
                "availability": "available" if present else ("not_available" if ident_ok else "failed"),
                "confidence": 0.99 if present else 0.0,
                "evidence_ids": [f"ev-{ident}"] if ident in records else [],
            }
        )

    scalar("name", profile.get("name"))
    scalar("legal_form", profile.get("legal_form"))
    scalar("industry", {"code": profile.get("industry_code"), "label": profile.get("industry_label")} if profile.get("industry_code") else None)
    scalar("employees", profile.get("employees"))
    scalar("municipality", profile.get("municipality"))
    scalar("insolvency_status", {"bankrupt": profile.get("bankrupt"), "liquidating": profile.get("liquidating")} if profile.get("bankrupt") is not None else None)

    for module, (field, confidence) in MODULE_CLAIMS.items():
        rec = records.get(module)
        if rec is None:
            claims.append({"field": field, "value": None, "availability": "failed", "confidence": 0.0, "evidence_ids": []})
            continue
        availability = AVAILABILITY.get(rec.get("status"), "failed")
        value = rec.get("value")
        if module == "website":
            value = rec.get("source_url") if availability == "available" else None
        if module == "website" and availability == "available":
            # A site the registry lists but whose page does not confirm the legal entity (brand, franchise, shared or
            # interstitial page) stays visible as the registry's statement, with low confidence, never as a verified match.
            score = ((rec.get("value") or {}).get("identity_assessment") or {}).get("score")
            if rec.get("source_type") == "registry_linked_company_website" and (score is None or score < 0.9):
                confidence = 0.5
        claims.append(
            {
                "field": field,
                "value": value if availability == "available" else None,
                "availability": availability,
                "confidence": confidence if availability == "available" else 0.0,
                "evidence_ids": [f"ev-{module}"],
            }
        )

    errors = [
        {"module": module, "state": state["state"], "message": str((records.get(module) or {}).get("note") or "")[:200]}
        for module, state in envelope["modules"].items()
        if state["state"] in ("source_error", "submission_error", "budget_exhausted")
    ]

    metrics = profile.get("run_metrics", {})
    envelope["run"] = {
        "run_id": envelope["run_id"],
        "started_at": envelope["started_at"],
        "completed_at": envelope["completed_at"],
        "terminal_status": "completed" if envelope["state"] == "complete" else "failed",
    }
    envelope["claims"] = claims
    envelope["evidence"] = evidence
    envelope["errors"] = errors
    envelope["operations"] = {
        "requests": int(metrics.get("requests", 0)) + int(metrics.get("plus_requests", 0)),
        "runtime_ms": int(sum(metrics.get("latencies_ms", []))),
        "third_party_cost_usd": 0,
    }
    envelope.setdefault("changes", [])
    return envelope
