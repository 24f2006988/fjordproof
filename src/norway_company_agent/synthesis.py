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
    "job_postings": ("Job postings", "no open ad names this company in NAV's job feed and no structured job posting was found on a verified company site"),
    "social_profiles": ("Social profiles", "no social profile is linked from a verified company site"),
    "group_structure": ("Group", "the registry lists no group (konsern) relation"),
}

# The checked source behind each unknown, in order of preference; only evidence records present in the envelope are cited.
# Site-derived fields fall back to the registry record, which is where "no registered or verified website" was checked.
UNKNOWN_SOURCES = {
    "employees": ("ev-registry_live", "ev-registry"),
    "revenue": ("ev-financials-latest", "ev-financials"),
    "ceo": ("ev-roles",),
    "auditor": ("ev-roles", "ev-registry_live"),
    "official_website": ("ev-website", "ev-registry_live", "ev-registry"),
    "news_items": ("ev-website", "ev-registry_live"),
    "job_postings": ("ev-nav_jobs", "ev-website", "ev-registry_live"),
    "social_profiles": ("ev-website", "ev-registry_live"),
    "group_structure": ("ev-group", "ev-registry_live"),
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
    return f"{(new - old) / old * 100:+.0f}%"


def _change(new: float, old: float | None) -> str:
    """Percentage only from a positive base; otherwise the two amounts, so a sign flip is never misread."""
    return _pct(new, old) if old and old > 0 else f"from {_nok(old)} to {_nok(new)}"


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
    activity_text = val("registered_activity") or val("statutory_purpose")
    if activity_text:
        src = "registered_activity" if val("registered_activity") else "statutory_purpose"
        say("activity", f"In its own registered words, the company's activity is: \"{activity_text}\"", src)

    # Size: registry headcount and the man-years (årsverk) stated in the filed accounts' notes
    emp, wp = val("employees"), val("registered_workplaces_count")
    fte = val("workforce_fte")
    fte_year = ((claims.get("workforce_fte") or {}).get("period") or {}).get("accounts_year")
    if emp is not None or wp or fte is not None:
        parts = []
        if emp is not None:
            parts.append(f"{emp} employees registered")
        if fte is not None:
            parts.append(f"{fte:g} man-years (årsverk) stated in the {fte_year or 'latest'} annual accounts")
        if wp:
            parts.append(f"{wp} registered sub-unit{'s' if wp != 1 else ''} (workplaces)")
        say("size", "Size: " + " and ".join(parts) + ".", "employees", "workforce_fte", "registered_workplaces_count")

    # Finances: newest filing of one scope, the multi-year trend, and a rule-based assessment.
    acc = (val("annual_accounts") or {}).get("records") or []
    acc = single_scope_accounts(acc)
    source_changes: list[str] = []  # changes visible inside the sources themselves (year on year)
    assessment: list[str] = []
    period = ""
    fin_summary = ""  # exact figures for the overall conclusion
    revenue_change = ""
    if acc:
        last = acc[-1]
        lp = last.get("period") or {}
        period = (lp.get("tilDato") or "")[:4] or "the newest retained period"
        scope = ACCOUNT_SCOPE.get(last.get("account_type"), "accounts")
        span = f" ({lp['fraDato']} to {lp['tilDato']}, {last.get('currency') or 'currency not stated'})" if lp.get("fraDato") and lp.get("tilDato") else ""
        say("finances", f"Newest figures in the accounts register, {scope}, {period}{span}: revenue {_nok(last.get('revenue'))}, operating result "
            f"{_nok(last.get('operating_result'))}, annual result {_nok(last.get('annual_result'))}, "
            f"equity {_nok(last.get('equity'))}, total assets {_nok(last.get('assets'))}.",
            "annual_accounts", "revenue", "annual_result", "total_equity", "total_assets")
        submitted = (claims.get("latest_accounts_year") or {})
        if submitted.get("source_field") == "sisteInnsendteAarsregnskap" and str(submitted.get("value") or "") > period:
            say("finances", f"The registry records accounts for {submitted['value']} as submitted, but the accounts register returned "
                f"figures only up to {period}; the {period} figures are therefore not the newest filing.", "latest_accounts_year")
            flag("newer_filing_pending", f"Accounts for {submitted['value']} are registered as submitted; their figures were not yet available.", "latest_accounts_year")
        res, eq, assets = last.get("annual_result"), last.get("equity"), last.get("assets")
        rev, op = last.get("revenue"), last.get("operating_result")
        if rev and rev > 0 and op is not None:
            say("finances", f"Operating margin {op / rev * 100:.1f}% in {period} (operating result {_nok(op)} / revenue {_nok(rev)}, computed from the filed figures).",
                "revenue", "operating_result")
        fin_bits = []
        if last.get("revenue") is not None:
            fin_bits.append(f"revenue {_nok(last.get('revenue'))}")
        if res is not None:
            fin_bits.append(f"an annual {'loss' if res < 0 else 'profit'} of {_nok(abs(res))}")
        if eq is not None and eq < 0:
            fin_bits.append(f"negative equity ({_nok(eq)})")
        elif eq is not None and assets:
            fin_bits.append(f"equity of {eq / assets * 100:.0f}% of total assets ({_nok(eq)} of {_nok(assets)})")
        if fin_bits:
            fin_summary = f"The newest figures in the accounts register, {scope}, for {period}, show " + ", ".join(fin_bits)
            if submitted.get("source_field") == "sisteInnsendteAarsregnskap" and str(submitted.get("value") or "") > period:
                fin_summary += f"; accounts for {submitted['value']} are registered as submitted but their figures are not yet available"
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
                if r0 is not None:
                    text += f" Change {pyear} to {period}: {_change(r1, r0)}."
                    source_changes.append(f"revenue {_change(r1, r0)} from {pyear} to {period}")
                    revenue_change = f"revenue {_change(r1, r0)} from {pyear}"
                    if r0 > 0 and r1 >= r0 * 1.2:
                        flag("revenue_growth", f"Revenue grew {_pct(r1, r0)} from {pyear} to {period}.", "annual_accounts")
                    elif r0 > 0 and r1 <= r0 * 0.8:
                        flag("revenue_decline", f"Revenue fell {_pct(r1, r0)} from {pyear} to {period}.", "annual_accounts")
                    if r0 > 0:
                        assessment.append(f"revenue {_pct(r1, r0)} year on year")
                if len(acc) > 2 and (first.get("revenue") or 0) > 0 and r1 is not None:
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
    recent: list[dict[str, Any]] = []
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
        nav = [str(j.get("source") or "").startswith("NAV") for j in jobs]
        where = "in NAV's job feed" if all(nav) else "on the company site and in NAV's job feed" if any(nav) else "on the company site"
        say("hiring", f"{len(jobs)} open job posting{'s' if len(jobs) != 1 else ''} {where}, e.g. \"{jobs[0]['title']}\".", "job_postings")
        flag("hiring", "The company advertises open positions.", "job_postings")

    # Unknowns, each with a reason and the evidence of the source that was checked
    known_ev = {e.get("id") for e in envelope.get("evidence", [])}
    unknowns = []
    for field, (label, why) in UNKNOWN_FIELDS.items():
        c = claims.get(field)
        if not c or c.get("availability") == "available":
            continue
        if field == "employees" and fte is not None:
            continue  # the filed accounts state the workforce; not an unknown
        state = c.get("availability")
        reason = why if state == "not_available" else UNKNOWN_WHY.get(state, state)
        sources = [i for i in c.get("evidence_ids") or [] if i in known_ev]
        if not sources:
            sources = [next(i for i in UNKNOWN_SOURCES.get(field, ()) + ("",) if not i or i in known_ev)]
        if field == "auditor" and state == "not_available" and val("audit_exemption"):
            reason = f"the company has opted out of audit (decision dated {val('audit_exemption')})"
            sources = list(dict.fromkeys(sources + ev("audit_exemption")))
        unknowns.append({"field": field, "label": label, "availability": state, "reason": reason,
                         "evidence_ids": [i for i in sources if i]})
    unknown_ev = list(dict.fromkeys(i for u in unknowns for i in u["evidence_ids"]))
    if unknowns and unknown_ev:
        statements.append({"topic": "unknowns", "text": "Not established from the checked sources: "
                           + "; ".join(f"{u['label'].lower()} ({u['reason']})" for u in unknowns) + ".",
                           "evidence_ids": unknown_ev})

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

    # Overall conclusion: one paragraph across the company, restating only exact cited figures (no judgement words).
    conclusion = ""
    if name:
        status = val("insolvency_status") or {}
        state = ("is registered as bankrupt" if status.get("bankrupt") else "is under liquidation" if status.get("liquidating")
                 else "has no bankruptcy or liquidation registered")
        label = industry.get("label") if industry else None
        parts = [f"{name} ({org}) is {_form(form)}{' in ' + mun if mun else ''}"
                 + (f", registered for {label.lower()} (NACE {industry.get('code')})" if label else "")
                 + (f" since {str(founded)[:4]}" if founded else "") + f", and {state}."]
        if activity_text:
            parts.append(f"Its registered activity: \"{activity_text.rstrip('.')}\".")
        if emp is not None:
            parts.append(f"The registry reports {emp} employee{'s' if emp != 1 else ''}.")
        if fte is not None:
            parts.append(f"The {fte_year or 'latest'} annual accounts state {fte:g} man-years.")
        active = [r for r in ((val("roles") or {}).get("roles") or []) if not r.get("inactive")]
        runners = [p for p in people if p.startswith(("general manager", "board chair"))]
        if runners:
            parts.append("It is run by " + " and ".join(runners) + (f" ({len(active)} current roles in total)" if len(active) > len(runners) else "")
                         + (f"; roles last changed on {recent[0].get('last_changed')}" if recent else "") + ".")
        elif active:
            parts.append(f"The role register lists {len(active)} current role{'s' if len(active) != 1 else ''}"
                         + (f", most recently changed on {recent[0].get('last_changed')}" if recent else "") + ".")
        if fin_summary:
            parts.append(fin_summary + (f"; {revenue_change}" if revenue_change else "") + ".")
        elif (claims.get("annual_accounts") or {}).get("availability") == "not_available":
            parts.append("No filed annual accounts were found in the public accounts register.")
        if site:
            web = f"A company-owned website is verified ({site})"
            if news:
                web += f", with dated news up to {news[0]['published_date']}"
            parts.append(web + ".")
        if jobs:
            parts.append(f"It advertises {len(jobs)} open job posting{'s' if len(jobs) != 1 else ''}.")
        else:
            parts.append("No company-owned website could be verified, so no website, social, news or job facts are published.")
        if unknowns:
            parts.append("Not established: " + ", ".join(u["label"].lower() for u in unknowns) + ".")
        conclusion = " ".join(parts)
        statements.insert(0, {"topic": "conclusion", "text": conclusion, "evidence_ids": list(dict.fromkeys(
            ev("name", "legal_form", "municipality", "industry", "incorporation_date", "insolvency_status", "annual_accounts",
               "employees", "roles", "official_website", "news_items", "job_postings")
            + [i for u in unknowns for i in u["evidence_ids"]]))})

    if name:
        what = f"{industry.get('label').lower()}" if industry and industry.get("label") else "registered entity"
        since = f" since {str(founded)[:4]}" if founded else ""
        head = f"{name} ({org}): {_form(form)[2:].lstrip()}{' in ' + mun if mun else ''}, {what}{since}"
        head += f"; {'; '.join(assessment)}." if assessment else "."
        headline = head
    else:
        headline = statements[0]["text"] if statements else f"Organisation number {org}: no registry record could be read."
    # The five questions of the brief, each answered from the cited facts above or stated as not established.
    def section(label: str, texts: list[str], fields: tuple[str, ...], missing: str) -> dict[str, Any]:
        texts = [t for t in texts if t]
        return {"label": label, "answered": bool(texts), "text": " ".join(texts) if texts else missing,
                "evidence_ids": ev(*fields) if texts else list(dict.fromkeys(i for u in unknowns for i in u["evidence_ids"]))[:3]}

    def first(topic: str) -> str:
        return next((s["text"] for s in statements if s["topic"] == topic), "")

    brief = [first("identity"), first("activity")]
    if activity_text:
        brief.append(f"Registered activity in its own words: \"{activity_text.rstrip('.')}\".")
    site_desc = val("company_description")
    if site_desc:
        brief.append(f"Its verified website says: \"{site_desc}\"")
    former = [c["value"] for c in envelope.get("claims", []) if c.get("field") == "former_name" and c.get("availability") == "available"]
    if former:
        brief.append("Former registered names: " + "; ".join(
            f"{f['name']} ({str(f.get('from') or '')[:4]}-{str(f.get('to') or '')[:4]})" for f in former[-3:]) + ".")

    fin_texts = [s["text"] for s in statements if s["topic"] in {"finances", "trend"}]
    run_texts = [s["text"] for s in statements if s["topic"] in {"leadership", "group"}]
    work = [first("size")]
    if jobs:
        work.append(first("hiring"))
    elif (claims.get("job_postings") or {}).get("availability") == "not_available":
        work.append("No open job ad for this company was found in NAV's job feed on the run date.")
    dated: list[tuple[str, str]] = []
    if news:
        dated.append((str(news[0]["published_date"]), f"latest company news {news[0]['published_date']}: \"{news[0]['title']}\""))
    for j in jobs[:1]:
        when = str(j.get("published") or j.get("published_date") or "")[:10]
        if when:
            dated.append((when, f"job ad \"{j['title']}\" published {when}"))
    for r in recent[:2]:
        dated.append((str(r.get("last_changed")), f"role registration changed: {r.get('role')} {_name(r.get('name') or r.get('organisation_number'))} ({r.get('last_changed')})"))
    filings = val("annual_account_filings") or []
    if filings:
        dated.append((str(filings[0].get("year")), f"newest annual accounts filed with the accounts register: {filings[0].get('year')}"))
    dated.sort(key=lambda d: d[0], reverse=True)
    activity_line = ("Dated public activity, newest first: " + "; ".join(t for _, t in dated) + ".") if dated else ""
    sections = [
        section("Company brief", brief, ("name", "legal_form", "municipality", "insolvency_status", "industry", "incorporation_date",
                                         "registered_activity", "statutory_purpose", "company_description", "former_name"), "No registry record could be read."),
        section("Latest financials", fin_texts, ("annual_accounts", "revenue", "operating_result", "annual_result", "total_equity", "total_assets",
                                                 "latest_accounts_year"), "No filed annual accounts were found in the public accounts register."),
        section("Who runs it?", run_texts, ("ceo", "board_chair", "auditor", "audit_exemption", "roles", "group_structure"),
                "No current roles are registered."),
        section("Working here", work, ("employees", "workforce_fte", "registered_workplaces_count", "job_postings"),
                "Neither a headcount, man-years nor an open job ad could be established."),
        section("Recent activity", [activity_line], ("news_items", "job_postings", "roles", "annual_account_filings"),
                "No dated public activity was found."),
    ]

    return {
        "headline": headline,
        "conclusion": conclusion,
        "sections": sections,
        "statements": statements,
        "signals": signals,
        "unknowns": unknowns,
        "changes": change_text,
        "method": "Generated deterministically from this envelope's claims; every statement cites the evidence of those claims. No model or external inference is used."
                  + (" The quoted website sentences were chosen by a language model and kept only where they occur verbatim on the verified page."
                     if site_desc and "selected by" in str((claims.get("company_description") or {}).get("source_path") or "") else ""),
    }
