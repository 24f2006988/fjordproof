"""Adapter: add the OUTPUT_CONTRACT.md fields (run, claims, evidence, errors, operations) to a native envelope."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .google_search import cost_per_call
from .observations import build_observations
from .official import ACCOUNT_SCOPE, single_scope_accounts
from .site_description import keyless_description
from .synthesis import build_synthesis

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


def _lit(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _record_key(acc: dict[str, Any]) -> str:
    """Selector of one Regnskapsregisteret record: its id, else its period end."""
    rid = acc.get("record_id")
    return str(rid) if rid is not None else f"regnskapsperiode.tilDato={(acc.get('period') or {}).get('tilDato')}"


# claim field -> Brreg Enhetsregisteret field the value is read from (quoted in the claim's evidence span)
REGISTRY_KEYS = {
    "name": "navn", "legal_form": "organisasjonsform.kode", "industry": "naeringskode1", "employees": "antallAnsatte",
    "municipality": "forretningsadresse.kommune", "insolvency_status": "konkurs/underAvvikling",
    "registered_office": "forretningsadresse", "postal_address": "postadresse", "registered_email": "epostadresse",
    "registered_phone": "telefon", "share_capital": "kapital", "incorporation_date": "stiftelsesdato",
    "vat_registered": "registrertIMvaregisteret", "audit_exemption": "fravalgRevisjonBeslutningsDato",
    "registered_activity": "aktivitet", "statutory_purpose": "vedtektsfestetFormaal", "sector": "institusjonellSektorkode",
    "registered_at": "registreringsdatoEnhetsregisteret", "business_register_date": "registreringsdatoForetaksregisteret",
    "vat_register_date": "registreringsdatoMerverdiavgiftsregisteret",
    "forced_dissolution": "underTvangsavviklingEllerTvangsopplosning", "part_of_group": "erIKonsern",
    "statutes_date": "vedtektsdato", "written_standard": "maalform", "registered_mobile": "mobil",
    "registry_website": "hjemmeside",
}
# Website states where the declared homepage could not be checked at all (nothing contradicts the registry entry). A
# domain that does not resolve is not "unchecked": it does not exist, so it never stands in as the website.
UNCHECKED_SITE = {"source_error", "blocked", "budget_exhausted", "not_fetched"}
# Housing co-operatives and condominiums usually list their manager's site (tnbbl.no for a borettslag): register fact only.
MANAGED_FORMS = {"BRL", "ESEK", "SAM", "BBL"}
# live registry_live key -> bulk snapshot key, for the extra registry claims
REGISTRY_EXTRA = {
    "registered_activity": None, "statutory_purpose": None, "sector": "institusjonellSektorkode",
    "registered_at": "registreringsdatoEnhetsregisteret", "business_register_date": "registreringsdatoForetaksregisteret",
    "vat_register_date": "registreringsdatoMerverdiavgiftsregisteret",
    "forced_dissolution": "underTvangsavviklingEllerTvangsopplosning", "part_of_group": "erIKonsern",
    "statutes_date": "vedtektsdato", "written_standard": "maalform", "mobile": "mobil",
}
# claim field -> Brreg Regnskapsregisteret field
ACCOUNT_KEYS = {
    "latest_accounts_year": "regnskapsperiode.tilDato", "revenue": "sumDriftsinntekter", "operating_result": "driftsresultat",
    "profit_before_tax": "ordinaertResultatFoerSkattekostnad", "annual_result": "aarsresultat", "total_assets": "sumEiendeler",
    "total_equity": "sumEgenkapital", "total_debt": "sumGjeld", "wage_costs": "loennskostnad",
}
# claim field -> full JSON path of the figure inside one Regnskapsregisteret record (see official.normalize_financials)
ACCOUNT_PATHS = {
    "latest_accounts_year": "regnskapsperiode.tilDato",
    "revenue": "resultatregnskapResultat.driftsresultat.driftsinntekter.sumDriftsinntekter",
    "operating_result": "resultatregnskapResultat.driftsresultat.driftsresultat",
    "wage_costs": "resultatregnskapResultat.driftsresultat.driftskostnad.loennskostnad",
    "profit_before_tax": "resultatregnskapResultat.ordinaertResultatFoerSkattekostnad",
    "annual_result": "resultatregnskapResultat.aarsresultat",
    "total_assets": "eiendeler.sumEiendeler",
    "total_equity": "egenkapitalGjeld.egenkapital.sumEgenkapital",
    "total_debt": "egenkapitalGjeld.gjeldOversikt.sumGjeld",
}
_SAME = object()  # raw_value default: the published value is the source value unchanged
# claim metric -> key in our normalized account record, for earlier years
PRIOR_YEAR_METRICS = {
    "revenue": "revenue", "operating_result": "operating_result", "wage_costs": "wage_costs",
    "profit_before_tax": "profit_before_tax", "annual_result": "annual_result", "total_assets": "assets",
    "total_equity": "equity", "total_debt": "debt",
}

# module claim -> literal summary of what the source returned
MODULE_SPANS = {
    "financials": lambda org, v: f"Brreg Regnskapsregisteret {org}: " + "; ".join(
        f"record {r.get('record_id')} {r.get('account_type')} {(r.get('period') or {}).get('fraDato')} to {(r.get('period') or {}).get('tilDato')}"
        for r in v.get("records") or []),
    "roles": lambda org, v: f"Brreg roller {org}: " + "; ".join(
        f"{r.get('role_code')} {r.get('name') or r.get('organisation_number')}" for r in v.get("roles") or [] if not r.get("inactive")),
    "locations": lambda org, v: f"Brreg underenheter {org}: " + "; ".join(
        f"{loc.get('organisation_number')} {loc.get('name')}" for loc in v.get("locations") or []),
}


def to_contract(envelope: dict[str, Any]) -> dict[str, Any]:
    profile = envelope["profile"]
    records: dict[str, dict[str, Any]] = profile.get("evidence", {})
    org = envelope["organisation_number"]

    evidence = []
    for name, rec in records.items():
        # A checked-and-empty source with no fetched bytes has no content hash; never invent one (sha256("null")).
        if not rec.get("content_sha256") and rec.get("status") != "available":
            continue
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

    by_id = {e["id"]: e for e in evidence}

    def cite(base: str, field: str, span: str) -> list[str]:
        """Evidence for one claim: the base source record, with a span quoting the exact source field and value."""
        src = by_id.get(f"ev-{base}")
        if not src:
            return []
        eid = f"ev-{base}-{field}"
        if eid not in by_id:
            by_id[eid] = {**src, "id": eid, "claim_span": span[:240]}
            evidence.append(by_id[eid])
        return [eid]

    claims = []
    # Identity claims come from the live registry record when available, else the bulk snapshot.
    ident = "registry_live" if records.get("registry_live", {}).get("status") == "available" else "registry"
    ident_ok = records.get(ident, {}).get("status") == "available"
    register = "Brreg Enhetsregisteret" if ident == "registry_live" else "Brreg Enhetsregisteret bulk"

    live_rec = records.get("registry_live", {}).get("value") or {}
    raw_rec = records.get("registry", {}).get("value") or {}
    # Dataset each registry claim is read from: the live API record or the bulk snapshot row.
    datasets = {"registry_live": f"enheter[{org}]", "registry": f"enheter_bulk[{org}]",
                "accounting_obligation": "brreg_accounting_obligation_rules"}

    def scalar(field: str, value: Any, ev_name: str = ident, conf: float = 0.99, raw: Any = _SAME) -> None:
        present = ident_ok and value not in (None, "", [], {})
        span = f"{register} {org}: {REGISTRY_KEYS.get(field, field)} = {_lit(value)}"
        claim = {
            "field": field,
            "value": value if present else None,
            "availability": "available" if present else ("not_available" if ident_ok else "failed"),
            "confidence": conf if present else 0.0,
            "evidence_ids": cite(ev_name, field, span) if present and ev_name in records else [],
        }
        if present:
            claim["raw_value"] = value if raw is _SAME else raw
            claim["source_path"] = f"{datasets.get(ev_name, ev_name)}.{REGISTRY_KEYS.get(field, 'classification' if ev_name == 'accounting_obligation' else field)}"
        claims.append(claim)

    def bulk_group(prefix: str) -> dict[str, Any] | None:
        """The bulk snapshot's flattened columns of one source object, e.g. kapital.* (raw strings, as published)."""
        group = {k: v for k, v in raw_rec.items() if k.startswith(prefix + ".")}
        return group or None

    # Core official registry identity
    scalar("name", profile.get("name"))
    scalar("legal_form", profile.get("legal_form"))
    scalar("industry", {"code": profile.get("industry_code"), "label": profile.get("industry_label")} if profile.get("industry_code") else None,
           raw=live_rec.get("industry") or bulk_group("naeringskode1"))
    scalar("employees", profile.get("employees"))
    scalar("municipality", profile.get("municipality"))
    scalar("insolvency_status", {"bankrupt": profile.get("bankrupt"), "liquidating": profile.get("liquidating")} if profile.get("bankrupt") is not None else None,
           raw={"konkurs": profile.get("bankrupt"), "underAvvikling": profile.get("liquidating")})

    # Official business address and contact
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
    scalar("registered_office", b_addr, raw=b_addr_raw if isinstance(b_addr_raw, dict) else bulk_group("forretningsadresse"))
    scalar("postal_address", live_rec.get("postal_address") or raw_rec.get("postadresse.postnummer"))

    # Registration details
    email = live_rec.get("email") or raw_rec.get("epostadresse")
    phone = live_rec.get("phone") or raw_rec.get("telefon") or raw_rec.get("mobil")
    scalar("registered_email", email)
    scalar("registered_phone", phone)
    # The homepage the company itself declares in the register: a registry fact, published whether or not our own
    # page check could prove or reach the site (official_website below stays reserved for proven sites).
    declared_site = live_rec.get("website") or raw_rec.get("hjemmeside")
    scalar("registry_website", declared_site)
    cap = live_rec.get("share_capital") or ({"amount": float(raw_rec["kapital.belop"]), "currency": raw_rec.get("kapital.valuta", "NOK"), "shares": raw_rec.get("kapital.antallAksjer")} if "kapital.belop" in raw_rec else None)
    scalar("share_capital", cap, raw=live_rec.get("share_capital") or bulk_group("kapital"))
    scalar("incorporation_date", live_rec.get("incorporation_date") or raw_rec.get("stiftelsesdato"))
    scalar("vat_registered", live_rec.get("vat_registered") if "vat_registered" in live_rec else (raw_rec.get("registrertIMvaregisteret") == "true" if "registrertIMvaregisteret" in raw_rec else None),
           raw=live_rec["vat_registered"] if "vat_registered" in live_rec else raw_rec.get("registrertIMvaregisteret"))
    scalar("audit_exemption", live_rec.get("audit_exemption") or raw_rec.get("fravalgRevisjonBeslutningsDato"))

    # Further fields of the same registry record: activity text, sector, register dates, group flag, etc.
    def bulk(key: str | None) -> Any:
        if not key:
            return None
        if f"{key}.kode" in raw_rec:
            return {"kode": raw_rec.get(f"{key}.kode"), "beskrivelse": raw_rec.get(f"{key}.beskrivelse")}
        v = raw_rec.get(key)
        return {"true": True, "false": False}.get(v, v) if isinstance(v, str) else v
    for live_key, bulk_key in REGISTRY_EXTRA.items():
        value = live_rec[live_key] if live_key in live_rec else bulk(bulk_key)
        field = "registered_mobile" if live_key == "mobile" else live_key
        if live_key in {"forced_dissolution", "part_of_group"} and value is None:
            continue  # a missing flag is not "false"; say nothing
        if live_key == "mobile" and (not value or value == phone):
            continue  # already published as registered_phone
        from_live = live_key in live_rec
        scalar(field, value, ev_name=ident if from_live else "registry",
               raw=_SAME if from_live else (bulk_group(bulk_key) if bulk_key and f"{bulk_key}.kode" in raw_rec else raw_rec.get(bulk_key or "")))

    # Former names (historiskeNavn, with the period each was held) and secondary industry codes, from the same live record.
    if ident == "registry_live":
        for i, n in enumerate(live_rec.get("former_names") or []):
            value = {"name": n.get("navn"), "from": n.get("fraDato"), "to": n.get("tilDato")}
            claims.append({"field": "former_name", "value": value, "availability": "available", "confidence": 0.99,
                           "period": {"fraDato": n.get("fraDato"), "tilDato": n.get("tilDato")},
                           "evidence_ids": cite(ident, f"former_name-{i}", f"{register} {org}: historiskeNavn[{i}] = {_lit(n)}"),
                           "raw_value": n, "source_path": f"enheter[{org}].historiskeNavn[{i}]"})
        for i, code in enumerate(live_rec.get("secondary_industries") or [], 2):
            claims.append({"field": "secondary_industry", "value": {"code": code.get("kode"), "label": code.get("beskrivelse")},
                           "availability": "available", "confidence": 0.99,
                           "evidence_ids": cite(ident, f"naeringskode{i}", f"{register} {org}: naeringskode{i} = {_lit(code)}"),
                           "raw_value": code, "source_path": f"enheter[{org}].naeringskode{i}"})

    # Accounting obligation
    acc_ob = records.get("accounting_obligation", {})
    if acc_ob.get("status") == "available" and acc_ob.get("value"):
        scalar("accounting_obligation", (acc_ob.get("value") or {}).get("classification"), ev_name="accounting_obligation")

    # Granular financial metrics from latest filed accounts
    fin_rec = records.get("financials", {})
    fin_ok = fin_rec.get("status") == "available"
    fin_records = (fin_rec.get("value") or {}).get("records") or []
    # Records are not guaranteed newest-first, and company/group accounts share periods: take the newest
    # filing of a single scope and state period, scope, currency and record id on every claim.
    scoped = single_scope_accounts(fin_records)
    latest_acc = scoped[-1] if scoped else {}
    acc_period = latest_acc.get("period") or None
    fin_ev = []
    fin_context = ""
    if latest_acc and "ev-financials" in by_id:
        p = acc_period or {}
        fin_ev = ["ev-financials-latest"]
        fin_context = (f"record {latest_acc.get('record_id')} for {org}: "
                       f"{ACCOUNT_SCOPE.get(latest_acc.get('account_type'), latest_acc.get('account_type') or 'scope not stated')}, "
                       f"period {p.get('fraDato')} to {p.get('tilDato')}, currency {latest_acc.get('currency') or 'not stated'}")
        by_id["ev-financials-latest"] = {**by_id["ev-financials"], "id": "ev-financials-latest",
                                         "claim_span": f"Brreg annual accounts {fin_context}"[:240]}
        evidence.append(by_id["ev-financials-latest"])
    def fin_scalar(field: str, val: Any) -> None:
        present = fin_ok and val is not None
        # The figure itself first, then the record, scope, period and currency it was read from.
        span = f"Brreg Regnskapsregisteret {ACCOUNT_KEYS.get(field, field)} = {_lit(val)}; {fin_context}"
        claim = {
            "field": field,
            "period": acc_period if present else None,
            "value": val if present else None,
            "availability": "available" if present else ("not_available" if fin_rec.get("status") in {"available", "not_found"} else "failed"),
            "confidence": 0.99 if present else 0.0,
            "evidence_ids": cite("financials-latest", field, span) if present and fin_ev else [],
        }
        if present:
            claim["scope"] = latest_acc.get("account_type")
            claim["currency"] = latest_acc.get("currency") if field != "latest_accounts_year" else None
            claim["source_record_id"] = latest_acc.get("record_id")
            # The year is published as YYYY; the source holds the full period end date.
            claim["raw_value"] = (acc_period or {}).get("tilDato") if field == "latest_accounts_year" and latest_acc else val
            claim["source_path"] = f"regnskap[{_record_key(latest_acc)}].{ACCOUNT_PATHS[field]}" if latest_acc else f"enheter[{org}].sisteInnsendteAarsregnskap"
        claims.append(claim)
    figures_year = (latest_acc.get("period") or {}).get("tilDato", "")[:4]
    submitted_year = str(profile.get("latest_submitted_accounts") or "")
    if submitted_year.isdigit() and submitted_year > figures_year and ident_ok:
        # The registry records a newer submitted filing than the accounts register returned figures for:
        # report the registry's year from the registry record, never the older figures' year as "latest".
        claims.append({"field": "latest_accounts_year", "period": None, "value": submitted_year, "availability": "available",
                       "confidence": 0.99, "source_field": "sisteInnsendteAarsregnskap",
                       "raw_value": profile.get("latest_submitted_accounts"), "source_path": f"{datasets[ident]}.sisteInnsendteAarsregnskap",
                       "evidence_ids": cite(ident, "latest_accounts_year", f"{register} {org}: sisteInnsendteAarsregnskap = {_lit(submitted_year)}"),
                       "note": f"figures in the accounts register end with {figures_year or 'no period'}"})
    else:
        fin_scalar("latest_accounts_year", figures_year or profile.get("latest_submitted_accounts"))
    fin_scalar("revenue", latest_acc.get("revenue"))
    fin_scalar("operating_result", latest_acc.get("operating_result"))
    fin_scalar("profit_before_tax", latest_acc.get("profit_before_tax"))
    fin_scalar("annual_result", latest_acc.get("annual_result"))
    fin_scalar("total_assets", latest_acc.get("assets"))
    fin_scalar("total_equity", latest_acc.get("equity"))
    fin_scalar("total_debt", latest_acc.get("debt"))
    fin_scalar("wage_costs", latest_acc.get("wage_costs"))

    # Earlier filed years we already hold: one claim per figure per year, each quoting its own record and period.
    for acc in scoped[:-1] if fin_ok and "ev-financials" in by_id else []:
        p = acc.get("period") or {}
        year = str(p.get("tilDato") or "")[:4]
        ctx = (f"record {acc.get('record_id')} for {org}: {ACCOUNT_SCOPE.get(acc.get('account_type'), acc.get('account_type') or 'scope not stated')}, "
               f"period {p.get('fraDato')} to {p.get('tilDato')}, currency {acc.get('currency') or 'not stated'}")
        for metric, key in PRIOR_YEAR_METRICS.items():
            val = acc.get(key)
            if val is None:
                continue
            claims.append({
                "field": "annual_account_figure", "metric": metric, "value": val, "period": p or None,
                "accounts_year": year, "scope": acc.get("account_type"), "currency": acc.get("currency"),
                "source_record_id": acc.get("record_id"), "availability": "available", "confidence": 0.99,
                "raw_value": val, "source_path": f"regnskap[{_record_key(acc)}].{ACCOUNT_PATHS[metric]}",
                "evidence_ids": cite("financials", f"{metric}-{year}-{acc.get('record_id')}",
                                     f"Brreg Regnskapsregisteret {ACCOUNT_KEYS.get(metric, metric)} = {_lit(val)}; {ctx}"),
            })

    # Every annual account filed with the Register of Company Accounts: year + official copy URL.
    fh_rec = records.get("financial_history", {})
    fh_val = fh_rec.get("value") or {}
    filings = fh_val.get("pdfs") or []
    fh_present = fh_rec.get("status") == "available" and bool(filings)
    claims.append({
        "field": "annual_account_filings",
        "value": filings if fh_present else None,
        "availability": "available" if fh_present else ("not_available" if fh_rec.get("status") in {"available", "not_found"} else "failed"),
        "confidence": 0.99 if fh_present else 0.0,
        "evidence_ids": cite("financial_history", "annual_account_filings",
                             f"Brreg Regnskapsregisteret aarsregnskap/kopi/{org}/aar = {_lit(fh_val.get('years'))}") if fh_present else [],
        **({"raw_value": fh_val.get("years"), "source_path": f"aarsregnskap/kopi[{org}].aar"} if fh_present else {}),
    })

    # Man-years from the filed annual account's own notes page (read from the official copy).
    wf_rec = records.get("workforce", {})
    wf_val = wf_rec.get("value") or {}
    wf_present = wf_rec.get("status") == "available" and wf_val.get("full_time_equivalents") is not None
    claims.append({
        "field": "workforce_fte",
        "value": wf_val.get("full_time_equivalents") if wf_present else None,
        "period": {"accounts_year": wf_val.get("accounts_year")} if wf_present else None,
        "availability": "available" if wf_present else ("not_available" if wf_rec.get("status") in {"available", "not_found", "budget_exhausted"} else "failed"),
        "confidence": 0.95 if wf_present else 0.0,
        "evidence_ids": cite("workforce", "workforce_fte",
                             f"Brreg annual-account copy {org} {wf_val.get('accounts_year')}, NOTEOPPLYSNINGER page {wf_val.get('page')}: "
                             f"{wf_val.get('span')} (OCR confidence {wf_val.get('ocr_confidence')})") if wf_present else [],
        **({"raw_value": wf_val.get("span"),
            "source_path": f"aarsregnskap/kopi[{org}][{wf_val.get('accounts_year')}].pdf#page={wf_val.get('page')}"} if wf_present else {}),
    })

    # Granular leadership roles
    roles_rec = records.get("roles", {})
    roles_ok = roles_rec.get("status") == "available"
    roles_list = (roles_rec.get("value") or {}).get("roles") or []

    def role_path(r: dict[str, Any]) -> str:
        """Where the holder sits in the roller response: role group, role type, then person name or entity number."""
        leaf = "enhet.organisasjonsnummer" if r.get("organisation_number") and not r.get("name") else ("person.navn" if r.get("organisation_number") is None else "enhet.navn")
        group = f"[type.kode={r['group_code']}]" if r.get("group_code") else ""
        return f"roller[{org}].rollegrupper{group}.roller[type.kode={r.get('role_code')}].{leaf}"

    def role_scalar(field: str, code: str) -> None:
        role = next((r for r in roles_list if r.get("role_code") == code and not r.get("inactive") and (r.get("name") or r.get("organisation_number"))), None)
        holder = (role.get("name") or role.get("organisation_number")) if role else None
        present = roles_ok and holder is not None
        span = f"Brreg roller {org}: {(role or {}).get('role') or code} ({code}) = {_lit(holder)}, last changed {(role or {}).get('last_changed')}"
        claims.append({
            "field": field,
            "value": holder if present else None,
            "availability": "available" if present else ("not_available" if roles_rec.get("status") in {"available", "not_found"} else "failed"),
            "confidence": 0.99 if present else 0.0,
            "evidence_ids": cite("roles", field, span) if present else [],
            **({"raw_value": holder, "source_path": role_path(role)} if present else {}),
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
        "evidence_ids": cite("roles", "board_members", f"Brreg roller {org}: Styremedlem (MEDL) = {_lit(board_members)}") if (roles_ok and board_members) else [],
        **({"raw_value": board_members, "source_path": f"roller[{org}].rollegrupper[type.kode=STYR].roller[type.kode=MEDL]"} if (roles_ok and board_members) else {}),
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
        "evidence_ids": cite("locations", "registered_workplaces_count", f"Brreg underenheter {org}: {len(loc_list)} registered sub-units") if loc_ok else [],
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
        "evidence_ids": cite("locations", "registered_workplaces", f"Brreg underenheter {org}: " + "; ".join(
            f"{w['organisation_number']} {w['name']}, {(w.get('address') or {}).get('postnummer') or ''} {(w.get('address') or {}).get('poststed') or ''}".strip()
            for w in workplaces)) if (loc_ok and workplaces) else [],
    })

    # Granular website details
    web_rec = records.get("website", {})
    web_val = web_rec.get("value") or {}
    # A fetched site whose identity is not proven for this exact legal entity is never published (wrong-company risk).
    web_assess = web_val.get("identity_assessment") or {}
    web_ambiguous = web_rec.get("status") == "available" and web_assess.get("publishable") is not True
    # An entity registered "c/o" another party shares that party's address, so an address match proves the host
    # (e.g. TEAMTEC WASTE TO ENERGY AS c/o TeamTec AS -> teamtec.no is TeamTec AS's site). Only an exact
    # organisation number can prove such a site.
    live_val = records.get("registry_live", {}).get("value") or {}
    reg_addr = " ".join(str(x) for a in (live_val.get("business_address"), live_val.get("postal_address")) if isinstance(a, dict)
                        for x in (a.get("adresse") or [])) + " " + str(raw_rec.get("forretningsadresse.adresse") or "")
    care_of = bool(re.search(r"\b(c/o|v/)", reg_addr, re.IGNORECASE))
    exact_proof = any("exact organisation number" in str(r) for r in web_assess.get("reasons") or [])
    if web_rec.get("status") == "available" and not web_ambiguous and care_of and not exact_proof:
        web_ambiguous = True
    web_ok = web_rec.get("status") == "available" and not web_ambiguous
    web_conf = 0.9 if web_ok else 0.0
    if web_ok and web_rec.get("source_type") == "registry_linked_company_website":
        score = (web_val.get("identity_assessment") or {}).get("score")
        if score is None or score < 0.9:
            web_conf = 0.5
    for fld, key, selector in (("website_title", "title", "html>head>title"), ("website_description", "description", 'html>head>meta[name="description"]@content')):
        val = web_val.get(key)
        present = web_ok and val not in (None, "")
        claims.append({
            "field": fld,
            "value": val if present else None,
            "availability": "available" if present else ("ambiguous" if web_ambiguous else "not_available" if web_rec.get("status") in {"available", "not_found"} else "failed"),
            "confidence": web_conf if present else 0.0,
            "evidence_ids": ["ev-website"] if present and "website" in records else [],
            **({"raw_value": val, "source_path": f"{web_val.get('final_url') or web_rec.get('source_url')} {selector}"} if present else {}),
        })

    # What the company does, quoted from its verified website. With GEMINI_API_KEY the step picks sentences (each checked
    # verbatim on the page); otherwise, or when it found none, the site's own first prose paragraph / structured or meta
    # description. Never published from an unproven site.
    desc_rec = records.get("site_description")
    desc_val = (desc_rec or {}).get("value") or {}
    sentences = (desc_val.get("sentences") or []) if (desc_rec or {}).get("status") == "available" and web_ok else []
    keyless = None if sentences or not web_ok else keyless_description(web_val, profile.get("name"))
    if sentences:
        desc_ids = []
        for i, s in enumerate(sentences, 1):
            eid = f"ev-site_description-{i}"
            evidence.append({"id": eid, "source_url": desc_rec.get("source_url"), "source_class": "company_owned_site",
                             "retrieved_at": desc_rec.get("retrieved_at"), "content_sha256": desc_rec.get("content_sha256"), "claim_span": s[:240]})
            desc_ids.append(eid)
        claims.append({"field": "company_description", "value": " ".join(sentences), "availability": "available", "confidence": 0.85,
                       "evidence_ids": desc_ids, "raw_value": sentences,
                       "source_path": f"{desc_rec.get('source_url')} main text, sentences selected by {desc_val.get('model')}",
                       "method": desc_val.get("method")})
    elif keyless:
        text, method, where = keyless
        claims.append({"field": "company_description", "value": text, "availability": "available", "confidence": 0.8,
                       "evidence_ids": cite("website", "company_description", text), "raw_value": text,
                       "source_path": f"{web_val.get('final_url') or web_rec.get('source_url')} {where}", "method": method})
    elif web_ok or desc_rec is not None:
        claims.append({"field": "company_description", "value": None,
                       "availability": "not_available" if web_ok else ("not_applicable" if (desc_rec or {}).get("status") == "not_applicable" else "failed"),
                       "confidence": 0.0, "evidence_ids": []})

    # Granular external footprint signals
    fp_rec = records.get("external_footprint", {})
    # Facts read from a site are only as good as the site's identity: none are published from an unproven site.
    fp_ok = fp_rec.get("status") == "available" and web_ok
    fp_val = fp_rec.get("value") or {}
    item_sources = fp_val.get("item_sources") or {}

    def fp_evidence(*source_keys: str) -> list[str]:
        """One evidence record per (field, page) that really contains the published items."""
        groups: dict[tuple[str, str, str], list[str]] = {}
        for sk in source_keys:
            for it in item_sources.get(sk, []):
                groups.setdefault((sk, it["source_url"], it["sha256"]), []).append(it["span"])
        ids = []
        for (sk, url, sha), spans in groups.items():
            eid = f"ev-fp-{sk}-{len(evidence)}"
            evidence.append({"id": eid, "source_url": url, "source_class": "company_owned_site", "retrieved_at": fp_rec.get("retrieved_at"),
                             "content_sha256": sha, "claim_span": " | ".join(spans)[:240]})
            ids.append(eid)
        return ids or (["ev-external_footprint"] if "external_footprint" in records else [])

    for fld, key, sks in (("careers_urls", "careers_urls", ("careers_urls",)), ("news_urls", "news_pages", ("news_pages",)),
                          ("social_profiles", "social_links", ("social_links",)), ("site_contact", "contact", ("contact_email", "contact_phone")),
                          ("dated_activity", "dated_activity", ("dated_activity",))):
        val = fp_val.get(key)
        present = fp_ok and val not in (None, "", [], {})
        claims.append({
            "field": fld,
            "value": val if present else None,
            "availability": "available" if present else ("not_available" if fp_rec.get("status") in {"available", "not_found"} else "failed"),
            "confidence": 0.80 if present else 0.0,
            "evidence_ids": fp_evidence(*sks) if present else [],
        })
    # Article-level news: title, direct URL and publication date as declared by the item's own page or feed.
    news = [{"title": i["title"], "url": i["url"], "published_date": i["date"], "date_kind": i.get("date_kind", "feed_item_date")}
            for i in (fp_val.get("dated_activity") or []) if i.get("title") and i.get("url") and i.get("date") and i.get("date_kind") != "sitemap_lastmod"]
    news.sort(key=lambda i: (i["published_date"], i["url"]), reverse=True)
    news_present = fp_ok and bool(news)
    claims.append({
        "field": "news_items",
        "value": news if news_present else None,
        "availability": "available" if news_present else ("not_available" if fp_rec.get("status") in {"available", "not_found"} else "failed"),
        "confidence": 0.85 if news_present else 0.0,
        "evidence_ids": fp_evidence("dated_activity") if news_present else [],
    })
    contact_dict = fp_val.get("contact") or {}
    for cfld, ckey, csk in (("site_email", "email", "contact_email"), ("site_phone", "phone", "contact_phone")):
        cval = contact_dict.get(ckey)
        cpresent = fp_ok and cval not in (None, "")
        claims.append({
            "field": cfld,
            "value": cval if cpresent else None,
            "availability": "available" if cpresent else ("not_available" if fp_rec.get("status") in {"available", "not_found"} else "failed"),
            "confidence": 0.80 if cpresent else 0.0,
            "evidence_ids": fp_evidence(csk) if cpresent else [],
        })
    # Job postings: structured ads on the verified site, plus NAV ads whose employer number is this company or its sub-unit.
    site_jobs = (fp_val.get("job_postings") or []) if fp_ok else []
    nav_rec = records.get("nav_jobs") or {}
    nav_jobs = ((nav_rec.get("value") or {}).get("job_postings") or []) if nav_rec.get("status") == "available" else []

    def nav_job_evidence(j: dict[str, Any]) -> list[str]:
        eid = f"ev-nav_jobs-{len(evidence)}"
        evidence.append({"id": eid, "source_url": j["url"], "source_record": j["source_record"], "source_class": "official_job_feed",
                         "retrieved_at": j.get("retrieved_at") or nav_rec.get("retrieved_at"), "content_sha256": j["content_sha256"],
                         "claim_span": j["span"]})
        return [eid]

    nav_items = [({k: v for k, v in j.items() if k not in {"span", "content_sha256", "retrieved_at", "source_record"}}, nav_job_evidence(j))
                 for j in nav_jobs if j.get("content_sha256")]
    jobs_val = site_jobs + [item for item, _ in nav_items]
    jobs_present = bool(jobs_val)
    checked = fp_rec.get("status") in {"available", "not_found"} or nav_rec.get("status") in {"available", "not_found"}
    claims.append({
        "field": "job_postings",
        "value": jobs_val if jobs_present else None,
        "availability": "available" if jobs_present else ("not_available" if checked else "failed"),
        "confidence": 0.80 if jobs_present else 0.0,
        "evidence_ids": ((fp_evidence("job_postings") if site_jobs else []) + [e for _, ids in nav_items for e in ids]) if jobs_present else [],
    })

    # Baseline module claims (preserving existing contract compatibility)
    for module, (field, confidence) in MODULE_CLAIMS.items():
        rec = records.get(module)
        if rec is None:
            claims.append({"field": field, "value": None, "availability": "failed", "confidence": 0.0, "evidence_ids": []})
            continue
        availability = AVAILABILITY.get(rec.get("status"), "failed")
        if module == "website" and web_ambiguous:
            availability = "ambiguous"
        value = rec.get("value")
        if module == "website":
            value = rec.get("source_url") if availability == "available" else None
        if module == "website" and availability == "available":
            score = ((rec.get("value") or {}).get("identity_assessment") or {}).get("score")
            if rec.get("source_type") == "registry_linked_company_website" and (score is None or score < 0.9):
                confidence = 0.5
        ev_ids = [f"ev-{module}"]
        if availability == "available" and module in MODULE_SPANS:
            ev_ids = cite(module, field, MODULE_SPANS[module](org, value or {}))
        site_exists = not re.search(r"did not resolve|no valid registry website|getaddrinfo|name or service not known|nodename", str(rec.get("note") or ""), re.I)
        if (module == "website" and rec.get("status") in UNCHECKED_SITE and declared_site and ident_ok and site_exists
                and str(profile.get("legal_form") or "").upper() not in MANAGED_FORMS):
            # Our page check never reached the declared homepage (timeout, robots, budget): publish the company's own
            # register entry as its website, cited to the register, with lower confidence than a proven site.
            claims.append({"field": field, "value": declared_site, "availability": "available", "confidence": 0.6,
                           "method": "registry_declared", "raw_value": declared_site, "source_path": f"{datasets[ident]}.hjemmeside",
                           "evidence_ids": cite(ident, "official_website", f"{register} {org}: hjemmeside = {_lit(declared_site)}"),
                           "note": f"declared in the register; our own page check could not reach it ({rec.get('status')})"})
            continue
        claims.append(
            {
                "field": field,
                "value": value if availability == "available" else None,
                "availability": availability,
                "confidence": confidence if availability == "available" else 0.0,
                "evidence_ids": ev_ids,
            }
        )

    # Atomic claims: one claim per individual fact (role, sub-unit, filing, social profile, article, job), each citing
    # its own span. Added next to the list claims above, which stay unchanged for the viewer and synthesis.
    def atomic(field: str, value: Any, ev_ids: list[str], conf: float, **extra: Any) -> None:
        if ev_ids:
            claims.append({"field": field, "value": value, "availability": "available", "confidence": conf, "evidence_ids": ev_ids, **extra})

    def fp_item_evidence(sk: str, item: str) -> list[str]:
        """Evidence for one footprint item: the page that contains it and the literal span found there."""
        for it in item_sources.get(sk, []):
            if it.get("item") == item:
                eid = f"ev-fp-{sk}-item-{len(evidence)}"
                evidence.append({"id": eid, "source_url": it["source_url"], "source_class": "company_owned_site",
                                 "retrieved_at": fp_rec.get("retrieved_at"), "content_sha256": it["sha256"], "claim_span": str(it["span"])[:240]})
                return [eid]
        return []

    if roles_ok:
        seen_roles = set()
        for r in roles_list:
            holder = r.get("name") or r.get("organisation_number")
            key = (holder if isinstance(holder, str) else _lit(holder), r.get("role_code"))
            if r.get("inactive") or not holder or key in seen_roles:
                continue
            seen_roles.add(key)
            span = f"Brreg roller {org}: {r.get('role') or r.get('role_code')} ({r.get('role_code')}) = {_lit(holder)}, last changed {r.get('last_changed')}"
            atomic("role", {"name": " ".join(holder) if isinstance(holder, list) else holder, "role_code": r.get("role_code"), "role": r.get("role"), "last_changed": r.get("last_changed")},
                   cite("roles", f"role-{len(seen_roles)}", span), 0.99, raw_value=holder, source_path=role_path(r))
    if loc_ok:
        for i, w in enumerate(workplaces, 1):
            a = w.get("address") or {}
            span = (f"Brreg underenheter {org}: {w.get('organisation_number')} {w.get('name')}, "
                    f"{' '.join(a.get('adresse') or [])} {a.get('postnummer') or ''} {a.get('poststed') or ''}").strip()
            atomic("workplace", w, cite("locations", f"workplace-{i}", span), 0.99, raw_value=w,
                   source_path=f"underenheter[overordnetEnhet={org}][organisasjonsnummer={w.get('organisation_number')}]")
    if fh_present:
        for f in filings:
            atomic("annual_account_filing", f, cite("financial_history", f"filing-{f.get('year')}",
                                                    f"Brreg Regnskapsregisteret annual-account copy {f.get('year')}: {f.get('url')}"),
                   0.99, period={"accounts_year": f.get("year")}, raw_value=f.get("year"), source_path=f"aarsregnskap/kopi[{org}].aar")
    if fp_ok:
        for s in fp_val.get("social_links") or []:
            atomic("social_profile", s, fp_item_evidence("social_links", s.get("url")), 0.80)
        for n in news:
            atomic("news_item", n, fp_item_evidence("dated_activity", n["url"]), 0.85)
        for addr in contact_dict.get("emails") or []:
            atomic("contact_email", addr, fp_item_evidence("contact_emails", addr), 0.85)
        for phone in contact_dict.get("phones") or []:
            atomic("contact_phone", phone, fp_item_evidence("contact_phones", phone), 0.85)
        for j in site_jobs:
            if isinstance(j, dict) and j.get("title"):
                atomic("job_posting", j, fp_item_evidence("job_postings", j["title"]), 0.80)
    for item, ids in nav_items:
        atomic("job_posting", item, ids, 0.95, period={"published": item.get("published"), "expires": item.get("expires")})

    known = {e["id"]: e for e in evidence}
    for claim in claims:
        claim["evidence_ids"] = [i for i in claim["evidence_ids"] if i in known]
        # Every published fact names the value as the source holds it and where in the source it sits. Claims read from
        # a page or feed item (site facts, NAV ads) point at that page or item; the literal excerpt is the evidence span.
        if claim["availability"] == "available" and claim["evidence_ids"] and "source_path" not in claim:
            ev = known[claim["evidence_ids"][0]]
            claim["source_path"] = ev.get("source_record") or ev.get("source_url")
            claim.setdefault("raw_value", claim["value"])

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
    # The same external facts in the starter kit's observation schema (platform / signal_type / identity proof).
    envelope["external_observations"] = build_observations(
        org, records, web_ok=web_ok, fp_ok=fp_ok, news=news if fp_ok else [], site_jobs=site_jobs, nav_jobs=nav_jobs,
        wf_present=wf_present, employees=profile.get("employees"), employees_ok=ident_ok,
        declared_site=declared_site, reg_ok=ident == "registry_live" and ident_ok)
    envelope["evidence"] = evidence
    envelope["errors"] = errors
    envelope["operations"] = {
        "requests": int(metrics.get("requests", 0)) + int(metrics.get("plus_requests", 0)) + int(metrics.get("workforce_requests", 0)),
        "runtime_ms": int(sum(metrics.get("latencies_ms", []))),
        "third_party_cost_usd": round(float(desc_val.get("cost_usd") or 0)
                                      + sum(n * cost_per_call(prov) for prov, n in (metrics.get("paid_search_calls") or {}).items())
                                      + float(metrics.get("model_discovery_cost_usd") or 0), 6) or 0,
    }
    envelope.setdefault("changes", [])
    envelope["synthesis"] = build_synthesis(envelope)
    return envelope
