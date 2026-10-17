"""Decision-useful synthesis built only from the envelope's own claims.

Every statement carries the evidence ids of the claims it is built from; nothing is inferred beyond what the
claims say. Unknowns are listed with the reason they are unknown, and changes since the previous run are
summarised. Deterministic: the same claims give the same text.
"""
from __future__ import annotations

from typing import Any

from .official import ACCOUNT_SCOPE, single_scope_accounts

UNKNOWN_WHY = {
    "not_available": "checked, but the source has no value for this company",
    "ambiguous": "a candidate was found but could not be proven to belong to this exact company, so it is not published",
    "blocked": "the source refused the request",
    "failed": "the source could not be read in this run",
    "not_applicable": "does not apply to this kind of entity",
}

# Fields worth explaining when unknown, with a short label and a field-specific reason for not_available.
UNKNOWN_FIELDS = {
    "employees": ("Employee count", "the registry publishes no employee count for this company"),
    "revenue": ("Revenue", "the latest filed accounts report no revenue line"),
    "ceo": ("Chief executive", "no general manager (daglig leder) is registered"),
    "auditor": ("Auditor", "no auditor is registered (small companies may opt out of audit)"),
    "official_website": ("Website", "no company-owned website could be verified with the exact organisation number or address"),
    "news_items": ("Dated news", "no dated news item with a publication date was found on a verified company site"),
    "job_postings": ("Job postings", "no structured job posting was found on a verified company site"),
    "social_profiles": ("Social profiles", "no social profile is linked from a verified company site"),
    "group_structure": ("Group", "the registry lists no group (konsern) relation"),
}


LEGAL_FORMS = {
    "AS": "private limited company (AS)", "ASA": "public limited company (ASA)", "ENK": "sole proprietorship (ENK)",
    "ANS": "general partnership (ANS)", "DA": "partnership with shared liability (DA)", "NUF": "Norwegian branch of a foreign company (NUF)",
    "SA": "cooperative (SA)", "BRL": "housing cooperative (BRL)", "ESEK": "condominium owners' association (ESEK)",
    "STI": "foundation (STI)", "FLI": "association (FLI)", "KS": "limited partnership (KS)", "SAM": "co-ownership (SAM)",
    "IKS": "inter-municipal company (IKS)", "KF": "municipal enterprise (KF)", "BBL": "housing cooperative association (BBL)",
}


def _form(code: Any) -> str:
    text = LEGAL_FORMS.get(str(code or "").upper(), f"registered entity ({code})" if code else "registered entity")
    return ("an " if text[0] in "aeiou" else "a ") + text


def _nok(value: float | None) -> str:
    if value is None:
        return "not reported"
    sign = "-" if value < 0 else ""
    v = abs(value)
    if v >= 1_000_000:
        return f"{sign}NOK {v / 1_000_000:.1f} million"
    if v >= 1_000:
        return f"{sign}NOK {v / 1_000:.0f} thousand"
    return f"{sign}NOK {v:.0f}"


def _pct(new: float, old: float) -> str:
    return f"{(new - old) / abs(old) * 100:+.0f}%"


def _name(value: Any) -> str:
    return " ".join(value) if isinstance(value, list) else str(value)


def build_synthesis(envelope: dict[str, Any]) -> dict[str, Any]:
    claims = {c["field"]: c for c in envelope.get("claims", [])}

    def val(field: str) -> Any:
        c = claims.get(field)
        return c["value"] if c and c.get("availability") == "available" else None

    def ev(*fields: str) -> list[str]:
        ids: list[str] = []
        for f in fields:
            for i in (claims.get(f) or {}).get("evidence_ids", []):
                if i not in ids:
                    ids.append(i)
        return ids

    statements: list[dict[str, Any]] = []
    signals: list[dict[str, Any]] = []

    def say(topic: str, text: str, *fields: str) -> None:
        statements.append({"topic": topic, "text": text, "evidence_ids": ev(*fields)})

    def flag(code: str, text: str, *fields: str) -> None:
        signals.append({"signal": code, "text": text, "evidence_ids": ev(*fields)})

    # Identity and status
    name, form, mun = val("name"), val("legal_form"), val("municipality")
    org = envelope.get("organisation_number")
    if name:
        status = val("insolvency_status") or {}
        state = ("is registered as bankrupt" if status.get("bankrupt") else
                 "is under liquidation" if status.get("liquidating") else
                 "has no bankruptcy or liquidation registered")
        say("identity", f"{name} (organisation number {org}) is {_form(form)}"
            f"{' in ' + mun if mun else ''} and {state}.", "name", "legal_form", "municipality", "insolvency_status")
        if status.get("bankrupt") or status.get("liquidating"):
            flag("insolvency", "The registry shows bankruptcy or liquidation.", "insolvency_status")
    industry = val("industry")
    founded = val("incorporation_date")
    if industry:
        say("activity", f"Registered activity: {industry.get('label')} (NACE {industry.get('code')})"
            f"{'; founded ' + str(founded) if founded else ''}.", "industry", "incorporation_date")

    # Size
    emp, wp = val("employees"), val("registered_workplaces_count")
    if emp is not None or wp:
        parts = []
        if emp is not None:
            parts.append(f"{emp} employees registered")
        if wp:
            parts.append(f"{wp} registered sub-unit{'s' if wp != 1 else ''} (workplaces)")
        say("size", "Size: " + " and ".join(parts) + ".", "employees", "registered_workplaces_count")

    # Finances: newest filing of one scope, the multi-year trend, and a rule-based assessment.
    acc = (val("annual_accounts") or {}).get("records") or []
    acc = single_scope_accounts(acc)
    source_changes: list[str] = []  # changes visible inside the sources themselves (year on year)
    assessment: list[str] = []
    period = ""
    if acc:
        last = acc[-1]
        lp = last.get("period") or {}
        period = (lp.get("tilDato") or "")[:4] or "the newest retained period"
        scope = ACCOUNT_SCOPE.get(last.get("account_type"), "accounts")
        span = f" ({lp['fraDato']} to {lp['tilDato']}, {last.get('currency') or 'currency not stated'})" if lp.get("fraDato") and lp.get("tilDato") else ""
        say("finances", f"Newest figures in the accounts register ({scope}), {period}{span}: revenue {_nok(last.get('revenue'))}, operating result "
            f"{_nok(last.get('operating_result'))}, annual result {_nok(last.get('annual_result'))}, "
            f"equity {_nok(last.get('equity'))}, total assets {_nok(last.get('assets'))}.",
            "annual_accounts", "revenue", "annual_result", "total_equity", "total_assets")
        submitted = (claims.get("latest_accounts_year") or {})
        if submitted.get("source_field") == "sisteInnsendteAarsregnskap" and str(submitted.get("value") or "") > period:
            say("finances", f"The registry records accounts for {submitted['value']} as submitted, but the accounts register returned "
                f"figures only up to {period}; the {period} figures are therefore not the newest filing.", "latest_accounts_year")
            flag("newer_filing_pending", f"Accounts for {submitted['value']} are registered as submitted; their figures were not yet available.", "latest_accounts_year")
        res, eq, assets = last.get("annual_result"), last.get("equity"), last.get("assets")
        if res is not None:
            assessment.append(f"annual {'loss' if res < 0 else 'profit'} {_nok(abs(res))} in {period}")
            if res < 0:
                flag("loss_making", f"The company reported a loss in {period}.", "annual_accounts")
        if eq is not None and eq < 0:
            assessment.append(f"negative equity at the end of {period}")
            flag("negative_equity", f"Equity was negative at the end of {period}.", "annual_accounts")
        elif eq is not None and assets:
            ratio = eq / assets * 100
            assessment.append(f"equity ratio {ratio:.0f}%")
            say("finances", f"Equity ratio {ratio:.0f}% at the end of {period} (equity {_nok(eq)} / total assets {_nok(assets)}, computed from the filed figures).",
                "total_equity", "total_assets")
            if ratio < 10:
                flag("thin_equity", f"Equity is below 10% of total assets ({ratio:.0f}%) at the end of {period}.", "total_equity", "total_assets")
        if len(acc) > 1:
            years = [((r.get("period") or {}).get("tilDato") or "")[:4] for r in acc]
            first, prev = acc[0], acc[-2]
            pyear = years[-2]
            series = ", ".join(f"{y} {_nok(r.get('revenue'))}" for y, r in zip(years, acc))
            r0, r1 = prev.get("revenue"), last.get("revenue")
            if r1 is not None and any(r.get("revenue") is not None for r in acc[:-1]):
                text = f"Revenue by year: {series}."
                if r0:
                    text += f" Change {pyear} to {period}: {_pct(r1, r0)}."
                    source_changes.append(f"revenue {_pct(r1, r0)} from {pyear} to {period}")
                    if r1 >= r0 * 1.2:
                        flag("revenue_growth", f"Revenue grew {_pct(r1, r0)} from {pyear} to {period}.", "annual_accounts")
                        assessment.append(f"revenue {_pct(r1, r0)} year on year")
                    elif r1 <= r0 * 0.8:
                        flag("revenue_decline", f"Revenue fell {_pct(r1, r0)} from {pyear} to {period}.", "annual_accounts")
                        assessment.append(f"revenue {_pct(r1, r0)} year on year")
                    else:
                        assessment.append(f"revenue {_pct(r1, r0)} year on year")
                if len(acc) > 2 and first.get("revenue") and r1 is not None:
                    text += f" Over {years[0]} to {period}: {_pct(r1, first['revenue'])}."
                say("trend", text, "annual_accounts")
            a0, a1 = prev.get("annual_result"), last.get("annual_result")
            if a0 is not None and a1 is not None:
                if (a0 < 0) != (a1 < 0):
                    say("trend", f"The annual result turned from {'a loss' if a0 < 0 else 'a profit'} in {pyear} ({_nok(a0)}) to "
                        f"{'a loss' if a1 < 0 else 'a profit'} in {period} ({_nok(a1)}).", "annual_accounts")
                source_changes.append(f"annual result {_nok(a0)} in {pyear} to {_nok(a1)} in {period}")
            e0, e1 = prev.get("equity"), last.get("equity")
            if e0 is not None and e1 is not None and _nok(e0) != _nok(e1):
                source_changes.append(f"equity {_nok(e0)} to {_nok(e1)}")
    elif (claims.get("annual_accounts") or {}).get("availability") == "not_available":
        say("finances", "No filed annual accounts were found in the public accounts register.", "annual_accounts")

    # People and control
    people = []
    if val("ceo"):
        people.append(f"general manager {_name(val('ceo'))}")
    if val("board_chair"):
        people.append(f"board chair {_name(val('board_chair'))}")
    if val("auditor"):
        people.append(f"auditor {_name(val('auditor'))}")
    elif val("audit_exemption"):
        people.append(f"no auditor (audit opt-out decided {val('audit_exemption')})")
    if people:
        say("leadership", "Registered roles: " + ", ".join(people) + ".", "ceo", "board_chair", "auditor", "audit_exemption")
    # Role registrations changed in the two years before this run (Brreg's per-role last-changed date).
    run_day = str(envelope.get("completed_at") or envelope.get("started_at") or "")[:10]
    if len(run_day) == 10 and run_day[:4].isdigit():
        cutoff = f"{int(run_day[:4]) - 2}{run_day[4:]}"
        recent = sorted((r for r in ((val("roles") or {}).get("roles") or [])
                         if not r.get("inactive") and cutoff <= str(r.get("last_changed") or "") <= run_day),
                        key=lambda r: str(r.get("last_changed")), reverse=True)
        if recent:
            items = [f"{r.get('role')} {_name(r.get('name') or r.get('organisation_number'))} ({r.get('last_changed')})" for r in recent[:4]]
            say("leadership", "Role registrations changed in the two years before this run: " + "; ".join(items) + ".", "roles")
            source_changes.append("role registrations changed: " + "; ".join(items))
    group = val("group_structure")
    if group:
        kids = group.get("children") or []
        is_root = group.get("organisasjonsnummer") == org
        say("group", (f"Parent of a group with {len(kids)} registered subsidiar{'ies' if len(kids) != 1 else 'y'}." if is_root
                      else f"Part of the group led by {group.get('navn')} ({group.get('organisasjonsnummer')})."), "group_structure")

    # Online presence and dated activity (verified company-owned site only)
    site = val("official_website")
    if site:
        extras = []
        social = val("social_profiles") or []
        if social:
            extras.append(f"{len(social)} linked social profile{'s' if len(social) != 1 else ''}")
        if val("careers_urls"):
            extras.append("a careers page")
        say("online", f"Verified company website: {site}" + (f", with {' and '.join(extras)}" if extras else "") + ".",
            "official_website", "social_profiles", "careers_urls")
    news = val("news_items") or []
    if news:
        latest = news[0]
        say("news", f"{len(news)} dated news item{'s' if len(news) != 1 else ''} on the company site; latest "
            f"{latest['published_date']}: \"{latest['title']}\".", "news_items")
        flag("recent_news", f"Latest company news is dated {latest['published_date']}.", "news_items")
    jobs = val("job_postings") or []
    if jobs:
        say("hiring", f"{len(jobs)} open job posting{'s' if len(jobs) != 1 else ''} on the company site, e.g. \"{jobs[0]['title']}\".", "job_postings")
        flag("hiring", "The company advertises open positions.", "job_postings")

    # Unknowns, each with a reason
    unknowns = []
    for field, (label, why) in UNKNOWN_FIELDS.items():
        c = claims.get(field)
        if not c or c.get("availability") == "available":
            continue
        state = c.get("availability")
        reason = why if state == "not_available" else UNKNOWN_WHY.get(state, state)
        if field == "auditor" and state == "not_available" and val("audit_exemption"):
            reason = f"the company has opted out of audit (decision dated {val('audit_exemption')})"
        unknowns.append({"field": field, "label": label, "availability": state, "reason": reason})

    changes = envelope.get("changes") or []
    in_sources = ("Changes visible in the sources (filed accounts, role register): " + "; ".join(source_changes) + ".") if source_changes else ""
    if changes:
        fields = sorted({str(c.get("field")) for c in changes})
        change_text = f"{len(changes)} material change{'s' if len(changes) != 1 else ''} since the previous run: {', '.join(fields)}." + (" " + in_sources if in_sources else "")
    else:
        change_text = (in_sources + " " if in_sources else "") + "No earlier snapshot of this agent to compare with; this run is the baseline and later runs report changes against it."

    # Overall assessment: a rule-based conclusion that only restates the cited facts above.
    status = val("insolvency_status") or {}
    if status.get("bankrupt") or status.get("liquidating"):
        assessment.insert(0, "bankruptcy or liquidation registered")
    if site:
        assessment.append("verified company website")
    elif (claims.get("official_website") or {}).get("availability") in {"not_available", "ambiguous"}:
        assessment.append("no verified website")
    if news:
        assessment.append(f"company news up to {news[0]['published_date']}")
    if jobs:
        assessment.append("hiring")
    if assessment:
        say("assessment", "Assessment (rule-based, from the facts above): " + "; ".join(assessment) + ".",
            "annual_accounts", "total_equity", "total_assets", "insolvency_status", "official_website", "news_items", "job_postings")

    if name:
        what = f"{industry.get('label').lower()}" if industry and industry.get("label") else "registered entity"
        since = f" since {str(founded)[:4]}" if founded else ""
        head = f"{name} ({org}): {_form(form)[2:].lstrip()}{' in ' + mun if mun else ''}, {what}{since}"
        head += f"; {'; '.join(assessment)}." if assessment else "."
        headline = head
    else:
        headline = statements[0]["text"] if statements else f"Organisation number {org}: no registry record could be read."
    return {
        "headline": headline,
        "statements": statements,
        "signals": signals,
        "unknowns": unknowns,
        "changes": change_text,
        "method": "Generated deterministically from this envelope's claims; every statement cites the evidence of those claims. No model or external inference is used.",
    }
