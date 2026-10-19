"""The envelope's external facts restated as observations in the starter kit's schema (external_footprint.py).

One observation per published external fact: verified website, social profile linked from it, dated site news,
site and NAV job postings, and workforce snapshots from Brreg. Each is built only from a fact the contract
already publishes as a claim, with the same source, hash and span, so claims and observations never disagree.
"""
from __future__ import annotations

import hashlib
from typing import Any

SOCIAL_PLATFORMS = {"facebook": "facebook", "instagram": "instagram", "linkedin": "linkedin", "youtube": "youtube",
                    "x": "x", "twitter": "x", "tiktok": "tiktok"}


def _id(kind: str, org: str, *parts: Any) -> str:
    return f"fjordproof-{kind}-" + hashlib.sha256("|".join([org, *map(str, parts)]).encode("utf-8")).hexdigest()[:24]


def _obs(org: str, kind: str, key: Any, **fields: Any) -> dict[str, Any]:
    return {"id": _id(kind, org, key), "organisation_number": org, "exact_entity": True, "rights_status": "approved", **fields}


def build_observations(org: str, records: dict[str, dict[str, Any]], *, web_ok: bool, fp_ok: bool, news: list[dict[str, Any]],
                       site_jobs: list[dict[str, Any]], nav_jobs: list[dict[str, Any]], wf_present: bool,
                       employees: Any, employees_ok: bool) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    web = records.get("website") or {}
    fp = records.get("external_footprint") or {}
    item_sources = (fp.get("value") or {}).get("item_sources") or {}

    def source_of(sk: str, item: Any) -> dict[str, Any] | None:
        return next((it for it in item_sources.get(sk, []) if it.get("item") == item), None)

    if web_ok:
        site = web.get("source_url")
        assess = (web.get("value") or {}).get("identity_assessment") or {}
        out.append(_obs(org, "site", site, platform="company_site", signal_type="company_profile", source_url=site,
                        retrieved_at=web.get("retrieved_at"), content_sha256=web.get("content_sha256"),
                        identity_proof=[{"type": assess.get("method") or "website_identity", "value": r} for r in assess.get("reasons") or []]
                        or [{"type": "website_identity", "value": assess.get("status")}],
                        acquisition_mode="permitted_public_page", source_class="company_owned_site",
                        evidence_span=str((web.get("value") or {}).get("title") or site)[:240], strategy="company_site_identity"))
    if fp_ok:
        site = web.get("source_url")
        for s in (fp.get("value") or {}).get("social_links") or []:
            platform = SOCIAL_PLATFORMS.get(str(s.get("platform") or "").casefold())
            src = source_of("social_links", s.get("url"))
            if not platform or not src:
                continue
            out.append(_obs(org, "handle", s["url"], platform=platform, signal_type="profile_handle", source_url=s["url"],
                            retrieved_at=fp.get("retrieved_at"), content_sha256=src["sha256"],
                            identity_proof=[{"type": "linked_from_verified_company_site", "value": src["source_url"]}],
                            acquisition_mode="permitted_public_page", source_class="company_owned_site",
                            evidence_span=str(src["span"])[:240], strategy="verified_handle_extraction", linked_from=site))
        for n in news:
            src = source_of("dated_activity", n["url"])
            if not src:
                continue
            out.append(_obs(org, "post", n["url"], platform="company_site", signal_type="public_post", source_url=n["url"],
                            retrieved_at=fp.get("retrieved_at"), content_sha256=src["sha256"], published_at=n["published_date"],
                            identity_proof=[{"type": "published_on_verified_company_site", "value": site}],
                            acquisition_mode="permitted_public_page", source_class="company_owned_site",
                            evidence_span=n["title"][:240], strategy="company_site_activity"))
        for j in site_jobs:
            src = source_of("job_postings", j.get("title")) if isinstance(j, dict) else None
            if not src:
                continue
            out.append(_obs(org, "sitejob", j["title"], platform="company_site", signal_type="job_posting",
                            source_url=j.get("url") or src["source_url"], retrieved_at=fp.get("retrieved_at"), content_sha256=src["sha256"],
                            identity_proof=[{"type": "published_on_verified_company_site", "value": site}],
                            acquisition_mode="permitted_public_page", source_class="company_owned_site",
                            evidence_span=str(src["span"])[:240], strategy="company_site_activity"))
    for j in nav_jobs:
        if not j.get("content_sha256"):
            continue
        out.append(_obs(org, "navjob", j["url"], platform="job_board", signal_type="job_posting", source_url=j["url"],
                        source_record=j["source_record"], retrieved_at=j.get("retrieved_at"), content_sha256=j["content_sha256"],
                        published_at=j.get("published"), expires_at=j.get("expires"),
                        identity_proof=[{"type": "employer_organisation_number_in_official_job_feed", "value": j["employer_organisation_number"]},
                                        {"type": "brreg_" + ("same_entity" if j["employer_link"] == "own" else "subunit_of_company"), "value": org}],
                        acquisition_mode="official_api", source_class="official_job_feed", evidence_span=j["span"],
                        strategy="jobs_feed_discovery"))
    wf = records.get("workforce") or {}
    if wf_present:
        v = wf.get("value") or {}
        out.append(_obs(org, "fte", v.get("accounts_year"), platform="brreg", signal_type="workforce_snapshot", source_url=wf.get("source_url"),
                        retrieved_at=wf.get("retrieved_at"), content_sha256=wf.get("content_sha256"), effective_at=str(v.get("accounts_year")),
                        identity_proof=[{"type": "official_report_url_organisation_number", "value": org},
                                        {"type": "organisation_number_on_ocr_page", "value": org}],
                        acquisition_mode="official_api", source_class="official_annual_account_copy", evidence_span=str(v.get("span"))[:240],
                        metrics={"workforce_value": v.get("full_time_equivalents"), "measure": "full_time_equivalents",
                                 "full_time_equivalents": v.get("full_time_equivalents"), "year": str(v.get("accounts_year")),
                                 "ocr_confidence": v.get("ocr_confidence")},
                        strategy="annual_report_workforce_snapshot"))
    reg = records.get("registry_live") or {}
    if employees_ok and isinstance(employees, int) and employees > 0 and reg.get("status") == "available":
        out.append(_obs(org, "employees", employees, platform="brreg", signal_type="workforce_snapshot", source_url=reg.get("source_url"),
                        retrieved_at=reg.get("retrieved_at"), content_sha256=reg.get("content_sha256"),
                        identity_proof=[{"type": "official_registry_organisation_number", "value": org}],
                        acquisition_mode="official_api", source_class="official_registry",
                        evidence_span=f"Brreg Enhetsregisteret {org}: antallAnsatte = {employees}",
                        metrics={"workforce_value": employees, "measure": "registered_employees", "registered_employees": employees},
                        strategy="registry_workforce_snapshot"))
    return [o for o in out if o.get("source_url") and len(str(o.get("content_sha256") or "")) == 64 and o.get("retrieved_at")]
