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
    "budget_exhausted": "not_available",
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

    def scalar(field: str, value: Any, ev_name: str = ident, conf: float = 0.99) -> None:
        present = ident_ok and value not in (None, "", [], {})
        claims.append(
            {
                "field": field,
                "value": value if present else None,
                "availability": "available" if present else ("not_available" if ident_ok else "failed"),
                "confidence": conf if present else 0.0,
                "evidence_ids": [f"ev-{ev_name}"] if present and ev_name in records else [],
            }
        )

    # Core official registry identity
    scalar("name", profile.get("name"))
    scalar("legal_form", profile.get("legal_form"))
    scalar("industry", {"code": profile.get("industry_code"), "label": profile.get("industry_label")} if profile.get("industry_code") else None)
    scalar("employees", profile.get("employees"))
    scalar("municipality", profile.get("municipality"))
    scalar("insolvency_status", {"bankrupt": profile.get("bankrupt"), "liquidating": profile.get("liquidating")} if profile.get("bankrupt") is not None else None)

    # Official business address and contact
    live_rec = records.get("registry_live", {}).get("value") or {}
    raw_rec = records.get("registry", {}).get("value") or {}
    b_addr_raw = live_rec.get("business_address")
    b_addr = None
    if isinstance(b_addr_raw, dict):
        b_addr = {
            "address": b_addr_raw.get("adresse"),
            "postcode": b_addr_raw.get("postnummer"),
            "city": b_addr_raw.get("poststed"),
            "municipality": b_addr_raw.get("kommune"),
            "country": b_addr_raw.get("land", "Norge"),
        }
    elif raw_rec.get("forretningsadresse.postnummer"):
        b_addr = {
            "address": raw_rec.get("forretningsadresse.adresse") or [v for k, v in raw_rec.items() if "forretningsadresse.adresse" in k],
            "postcode": raw_rec.get("forretningsadresse.postnummer"),
            "city": raw_rec.get("forretningsadresse.poststed"),
            "municipality": raw_rec.get("forretningsadresse.kommune"),
            "country": raw_rec.get("forretningsadresse.land", "Norge"),
        }
    scalar("registered_office", b_addr)
    scalar("postal_address", live_rec.get("postal_address") or raw_rec.get("postadresse.postnummer"))

    # Registration details
    email = live_rec.get("email") or raw_rec.get("epostadresse")
    phone = live_rec.get("phone") or raw_rec.get("telefon") or raw_rec.get("mobil")
    scalar("registered_email", email)
    scalar("registered_phone", phone)
    cap = live_rec.get("share_capital") or ({"amount": float(raw_rec["kapital.belop"]), "currency": raw_rec.get("kapital.valuta", "NOK"), "shares": raw_rec.get("kapital.antallAksjer")} if "kapital.belop" in raw_rec else None)
    scalar("share_capital", cap)
    scalar("incorporation_date", live_rec.get("incorporation_date") or raw_rec.get("stiftelsesdato"))
    scalar("vat_registered", live_rec.get("vat_registered") if "vat_registered" in live_rec else (raw_rec.get("registrertIMvaregisteret") == "true" if "registrertIMvaregisteret" in raw_rec else None))
    scalar("audit_exemption", live_rec.get("audit_exemption") or raw_rec.get("fravalgRevisjonBeslutningsDato"))

    # Accounting obligation
    acc_ob = records.get("accounting_obligation", {})
    if acc_ob.get("status") == "available" and acc_ob.get("value"):
        scalar("accounting_obligation", (acc_ob.get("value") or {}).get("classification"), ev_name="accounting_obligation")

    # Granular financial metrics from latest filed accounts
    fin_rec = records.get("financials", {})
    fin_ok = fin_rec.get("status") == "available"
    fin_records = (fin_rec.get("value") or {}).get("records") or []
    # Records are not guaranteed newest-first: pick the filing with the latest period end.
    latest_acc = max(fin_records, key=lambda r: str((r.get("period") or {}).get("tilDato") or ""), default={})
    acc_period = latest_acc.get("period") or None
    def fin_scalar(field: str, val: Any) -> None:
        present = fin_ok and val is not None
        claims.append({
            "field": field,
            "period": acc_period if present and field != "latest_accounts_year" else None,
            "value": val if present else None,
            "availability": "available" if present else ("not_available" if fin_rec.get("status") in {"available", "not_found"} else "failed"),
            "confidence": 0.99 if present else 0.0,
            "evidence_ids": ["ev-financials"] if present and "financials" in records else [],
        })
    fin_scalar("latest_accounts_year", (latest_acc.get("period") or {}).get("tilDato", "")[:4] or profile.get("latest_submitted_accounts"))
    fin_scalar("revenue", latest_acc.get("revenue"))
    fin_scalar("operating_result", latest_acc.get("operating_result"))
    fin_scalar("profit_before_tax", latest_acc.get("profit_before_tax"))
    fin_scalar("annual_result", latest_acc.get("annual_result"))
    fin_scalar("total_assets", latest_acc.get("assets"))
    fin_scalar("total_equity", latest_acc.get("equity"))
    fin_scalar("total_debt", latest_acc.get("debt"))

    # Granular leadership roles
    roles_rec = records.get("roles", {})
    roles_ok = roles_rec.get("status") == "available"
    roles_list = (roles_rec.get("value") or {}).get("roles") or []
    def role_scalar(field: str, code: str) -> None:
        holder = next((r.get("name") or r.get("organisation_number") for r in roles_list if r.get("role_code") == code and not r.get("inactive")), None)
        present = roles_ok and holder is not None
        claims.append({
            "field": field,
            "value": holder if present else None,
            "availability": "available" if present else ("not_available" if roles_rec.get("status") in {"available", "not_found"} else "failed"),
            "confidence": 0.99 if present else 0.0,
            "evidence_ids": ["ev-roles"] if present and "roles" in records else [],
        })
    role_scalar("ceo", "DAGL")
    role_scalar("board_chair", "LEDE")
    role_scalar("deputy_chair", "NEST")
    role_scalar("auditor", "REVI")
    role_scalar("accountant", "REGN")

    board_members = [
        r.get("name") or r.get("organisation_number")
        for r in roles_list
        if r.get("role_code") == "MEDL" and not r.get("inactive") and (r.get("name") or r.get("organisation_number"))
    ]
    claims.append({
        "field": "board_members",
        "value": board_members if (roles_ok and board_members) else None,
        "availability": "available" if (roles_ok and board_members) else ("not_available" if roles_rec.get("status") in {"available", "not_found"} else "failed"),
        "confidence": 0.99 if (roles_ok and board_members) else 0.0,
        "evidence_ids": ["ev-roles"] if (roles_ok and board_members and "roles" in records) else [],
    })

    # Granular workplaces count and details
    loc_rec = records.get("locations", {})
    loc_ok = loc_rec.get("status") == "available"
    loc_list = (loc_rec.get("value") or {}).get("locations") or []
    claims.append({
        "field": "registered_workplaces_count",
        "value": len(loc_list) if loc_ok else None,
        "availability": "available" if loc_ok else ("not_available" if loc_rec.get("status") in {"available", "not_found"} else "failed"),
        "confidence": 0.99 if loc_ok else 0.0,
        "evidence_ids": ["ev-locations"] if loc_ok and "locations" in records else [],
    })
    workplaces = [
        {
            "name": loc.get("name"),
            "organisation_number": loc.get("organisation_number"),
            "address": loc.get("address"),
            "industry": loc.get("industry"),
            "employees": loc.get("employees"),
        }
        for loc in loc_list
        if loc.get("name") or loc.get("organisation_number")
    ]
    claims.append({
        "field": "registered_workplaces",
        "value": workplaces if (loc_ok and workplaces) else None,
        "availability": "available" if (loc_ok and workplaces) else ("not_available" if loc_rec.get("status") in {"available", "not_found"} else "failed"),
        "confidence": 0.99 if (loc_ok and workplaces) else 0.0,
        "evidence_ids": ["ev-locations"] if (loc_ok and workplaces and "locations" in records) else [],
    })

    # Granular website details
    web_rec = records.get("website", {})
    web_ok = web_rec.get("status") == "available"
    web_val = web_rec.get("value") or {}
    web_conf = 0.9 if web_ok else 0.0
    if web_ok and web_rec.get("source_type") == "registry_linked_company_website":
        score = (web_val.get("identity_assessment") or {}).get("score")
        if score is None or score < 0.9:
            web_conf = 0.5
    for fld, key in (("website_title", "title"), ("website_description", "description")):
        val = web_val.get(key)
        present = web_ok and val not in (None, "")
        claims.append({
            "field": fld,
            "value": val if present else None,
            "availability": "available" if present else ("not_available" if web_rec.get("status") in {"available", "not_found"} else "failed"),
            "confidence": web_conf if present else 0.0,
            "evidence_ids": ["ev-website"] if present and "website" in records else [],
        })

    # Granular external footprint signals
    fp_rec = records.get("external_footprint", {})
    fp_ok = fp_rec.get("status") == "available"
    fp_val = fp_rec.get("value") or {}
    for fld, key in (("careers_urls", "careers_urls"), ("news_urls", "news_pages"), ("social_profiles", "social_links"), ("site_contact", "contact"), ("dated_activity", "dated_activity")):
        val = fp_val.get(key)
        present = fp_ok and val not in (None, "", [], {})
        claims.append({
            "field": fld,
            "value": val if present else None,
            "availability": "available" if present else ("not_available" if fp_rec.get("status") in {"available", "not_found"} else "failed"),
            "confidence": 0.80 if present else 0.0,
            "evidence_ids": ["ev-external_footprint"] if present and "external_footprint" in records else [],
        })
    contact_dict = fp_val.get("contact") or {}
    for cfld, ckey in (("site_email", "email"), ("site_phone", "phone")):
        cval = contact_dict.get(ckey)
        cpresent = fp_ok and cval not in (None, "")
        claims.append({
            "field": cfld,
            "value": cval if cpresent else None,
            "availability": "available" if cpresent else ("not_available" if fp_rec.get("status") in {"available", "not_found"} else "failed"),
            "confidence": 0.80 if cpresent else 0.0,
            "evidence_ids": ["ev-external_footprint"] if cpresent and "external_footprint" in records else [],
        })
    jobs_val = fp_val.get("job_postings") or []
    jobs_present = fp_ok and bool(jobs_val)
    claims.append({
        "field": "job_postings",
        "value": jobs_val if jobs_present else None,
        "availability": "available" if jobs_present else ("not_available" if fp_rec.get("status") in {"available", "not_found"} else "failed"),
        "confidence": 0.80 if jobs_present else 0.0,
        "evidence_ids": ["ev-external_footprint"] if jobs_present and "external_footprint" in records else [],
    })

    # Baseline module claims (preserving existing contract compatibility)
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
