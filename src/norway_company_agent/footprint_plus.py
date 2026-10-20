"""Free website discovery and company-owned footprint extraction with strict exact-entity gating.

Only candidates the registry itself points to (its e-mail domain) or that print the exact organisation
number are published. Everything else stays `not_found`. Uses the starter's SSRF-safe, robots-aware fetcher.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import threading
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any

from bs4 import BeautifulSoup

from . import discovery_index
from .evidence import evidence, utc_now
from .google_search import google_candidate_hosts
from .identity import LEGAL_AND_GENERIC, _tokens, apply_website_identity_gate, assess_social_identity
from .nav_jobs import homepage_hosts
from .website import (
    SAFE_OPENER,
    USER_AGENT,
    _cache_page,
    _get_cached_page,
    _get_cached_robots_body,
    _registered_domain,
    _robots_allowed,
    _robots_check,
    _social_links,
    assert_public_url,
    fetch_website,
    normalize_social_url,
)

FREE_MAIL = {"gmail.com", "hotmail.com", "outlook.com", "live.com", "yahoo.com", "icloud.com", "online.no",
             "msn.com", "me.com", "hotmail.no", "outlook.no", "live.no", "yahoo.no", "getmail.no", "start.no",
             "broadpark.no", "frisurf.no", "c2i.net", "lyse.net", "powertech.no", "tele2.no", "altibox.no",
             "proton.me", "protonmail.com", "bbhosting.no"}


GENERIC_SLUG_TOKENS = {
    "holding", "invest", "eiendom", "eiendommer", "drift", "forvaltning", "gruppen", "group",
    "markedforing", "markedsforing", "service", "partner", "consulting", "norge", "norway",
    "as", "solutions", "teknologi", "handel", "transport", "bygg", "montasje",
    "advokat", "arkitekt", "arkitektfirma", "revisjon", "regnskap", "borettslag",
    "sameiet", "sameie", "boligbyggelag", "stiftelse", "forening", "lag", "klubb",
    "forbund", "bedrift", "selskap", "forretning", "entreprenor", "maler", "rorlegger",
    "elektriker", "auto", "bil", "verksted", "butikk", "kafe", "restaurant", "hotell", "studio",
    "finans", "kapital", "management", "radgivning", "raadgivning", "utvikling", "kjop", "salg",
    "baerum", "oslo", "bergen", "stavanger", "trondheim", "drammen", "vest", "nord", "sor", "ost",
    "takst", "eiendomstaksering", "boligtaksering", "kraft", "kraftverk", "industrier", "gard", "gaard",
    "barnehage", "interior", "farge", "marked", "media", "digital",
}
BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"

_SEARCH_CACHE_LOCK = threading.Lock()
_GOOGLE_CANDIDATE_CACHE: dict[str, list[str]] = {}
_BRAVE_CANDIDATE_CACHE: dict[str, list[str]] = {}


def clear_search_caches() -> None:
    """Clear per-run search candidate caches (used by tests)."""
    with _SEARCH_CACHE_LOCK:
        _GOOGLE_CANDIDATE_CACHE.clear()
        _BRAVE_CANDIDATE_CACHE.clear()


def _brave_search_candidates(profile: dict[str, Any], api_key: str, timeout: float = 8.0) -> list[str]:
    try:
        from .discovery import build_company_search_query, choose_search_candidate, parse_brave_web_results
        query = build_company_search_query(profile)
        url = BRAVE_ENDPOINT + "?" + urllib.parse.urlencode({
            "q": query,
            "count": 5,
            "country": "no",
            "search_lang": "nb",
            "safesearch": "moderate",
            "spellcheck": "0",
        })
        req = urllib.request.Request(url, headers={
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Cache-Control": "no-cache",
            "User-Agent": "builderr-signalpost-poc/0.1 (+https://builderr.ai)",
            "X-Subscription-Token": api_key,
        })
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = json.loads(resp.read())
        results = parse_brave_web_results(payload, query=query)
        decision = choose_search_candidate(profile, results)
        selected = decision.get("selected")
        if selected and selected.get("url"):
            parsed = urllib.parse.urlparse(selected["url"])
            host = (parsed.hostname or "").casefold().removeprefix("www.")
            if host:
                return [host]
    except Exception:
        pass
    return []


def _resolves(host: str) -> bool:
    # DNS only: a raw TCP probe wrongly rejected apex domains that answer only via www, and it connected
    # to guessed hosts before the public-URL check.
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return True
    except OSError:
        return False


COMPOUND_SECTOR_PREFIXES = (
    "bygg", "maskin", "elektro", "eiendoms", "eiendom", "bil", "transport",
    "helse", "renhold", "konsulent", "industri", "energi", "varme", "kulde",
)


def _raw_tokens(value: Any) -> list[str]:
    text = str(value or "").strip().casefold()
    folded: list[str] = []
    for ch in text:
        if ch in "æøå":
            folded.append(ch)
        else:
            norm = unicodedata.normalize("NFKD", ch)
            folded.append("".join(c for c in norm if not unicodedata.combining(c)))
    cleaned = "".join(folded)
    return [tok for tok in re.findall(r"[a-z0-9æøå]+", cleaned) if tok not in LEGAL_AND_GENERIC and len(tok) > 1]


def _slug_variants(text: str) -> set[str]:
    """Generate Norwegian romanization variants: å -> a / aa, ø -> o / oe, æ -> ae."""
    v1 = text.translate(str.maketrans({"ø": "o", "å": "a", "æ": "ae"}))
    v2 = text.translate(str.maketrans({"ø": "oe", "å": "aa", "æ": "ae"}))
    v3 = text.translate(str.maketrans({"ø": "o", "å": "aa", "æ": "ae"}))
    return {v for v in (v1, v2, v3) if 3 <= len(v) <= 40}



# Candidate sources named by a third party (registry e-mail, NAV ad, crawl cache): address-only proof is never enough.
STRICT_METHODS = {"registry_email_domain", "job_feed_homepage", "discovery_index"}
# Crawl-cache candidates are published only on the exact organisation number printed live today.
EXACT_ONLY_METHODS = {"discovery_index"}

def candidate_hosts(profile: dict[str, Any]) -> list[tuple[str, str]]:
    """(host, method) pairs, best first. Method decides how strict the identity gate must be."""
    out: list[tuple[str, str]] = []
    tokens = _tokens(profile.get("name"))
    raw_toks = _raw_tokens(profile.get("name"))
    raw_name = str(profile.get("name") or "").strip().casefold()
    org_key = re.sub(r"\D", "", str(profile.get("organisation_number") or ""))
    search_timeout = float(getattr(_NET, "search_timeout", 8.0))
    search_reqs = 0

    # 0. Declared Common Crawl discovery cache: hosts that printed this org number when crawled
    for idx_host, _page in discovery_index.candidates(profile):
        out.append((idx_host, "discovery_index"))

    # 1. Registered email domains (from registry bulk, live, and subunits)
    emails: list[str] = []
    raw = (profile.get("evidence", {}).get("registry", {}).get("value") or {})
    live = (profile.get("evidence", {}).get("registry_live", {}).get("value") or {})
    if raw.get("epostadresse"):
        emails.append(str(raw["epostadresse"]))
    if live.get("email"):
        emails.append(str(live["email"]))
    for loc in (profile.get("evidence", {}).get("locations", {}).get("value") or {}).get("locations", []):
        if loc.get("email"):
            emails.append(str(loc["email"]))

    freemail_locals: list[str] = []
    for em in emails:
        if "@" in em:
            local_part, d = em.rsplit("@", 1)
            d = d.strip().lower()
            if d and d not in FREE_MAIL and "." in d:
                out.append((d, "registry_email_domain"))
                # If subdomain (e.g. mail.firma.no), also add root domain
                parts = d.split(".")
                if len(parts) > 2 and parts[-1] in {"no", "com", "org", "net"}:
                    root_d = ".".join(parts[-2:])
                    if root_d not in FREE_MAIL:
                        out.append((root_d, "registry_email_domain"))
            elif d in FREE_MAIL:
                cleaned_local = re.sub(r"^(?:post|info|kontakt|kontor|firma|mail)[._\-]+", "", local_part.strip().lower())
                cleaned_local = re.sub(r"[._\-]+(?:no|as|asa)$", "", cleaned_local)
                slug_local = re.sub(r"[^a-z0-9-]", "", cleaned_local).strip("-")
                if 4 <= len(slug_local) <= 30 and not slug_local.isdigit():
                    joined_toks = "".join(tokens)
                    if any(t[:3] in slug_local for t in tokens if len(t) >= 3) or any(
                        slug_local[i : i + 4] in joined_toks for i in range(len(slug_local) - 3)
                    ):
                        freemail_locals.append(slug_local)

    # 1b. Homepages the employer itself names in NAV job ads tied to this company's organisation number
    for nav_host in homepage_hosts(profile):
        out.append((nav_host, "job_feed_homepage"))

    # 2a. Google Search / Places candidates (once per company per run; off unless env keys set)
    g_cached: list[str] | None = None
    if org_key:
        with _SEARCH_CACHE_LOCK:
            g_cached = _GOOGLE_CANDIDATE_CACHE.get(org_key)
    if g_cached is not None:
        g_hosts = g_cached
    else:
        g_hosts, g_reqs, g_provider = google_candidate_hosts(profile, timeout=search_timeout)
        search_reqs += int(g_reqs or 0)
        if org_key and (g_reqs or g_provider is not None or g_hosts):
            with _SEARCH_CACHE_LOCK:
                _GOOGLE_CANDIDATE_CACHE[org_key] = list(g_hosts)
    for g_host in g_hosts:
        out.append((g_host, "google_search_candidate"))

    # 2b. Brave Search candidate (if key present in env; once per company per run)
    brave_key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    if brave_key:
        b_cached: list[str] | None = None
        if org_key:
            with _SEARCH_CACHE_LOCK:
                b_cached = _BRAVE_CANDIDATE_CACHE.get(org_key)
        if b_cached is not None:
            b_hosts = b_cached
        else:
            b_hosts = _brave_search_candidates(profile, brave_key, timeout=search_timeout)
            search_reqs += 1
            if org_key:
                with _SEARCH_CACHE_LOCK:
                    _BRAVE_CANDIDATE_CACHE[org_key] = list(b_hosts)
        for b_host in b_hosts:
            out.append((b_host, "brave_search_candidate"))

    _NET.search_requests = search_reqs

    # 3. Free-mail local-part brand candidates (subject to full _accept_proof gate)
    for fl in freemail_locals:
        out.append((f"{fl}.no", "brand_token_guess"))
        out.append((f"{fl}.com", "brand_token_guess"))

    # 3b. Hyphenated single-letter prefix slugs (e.g. A-TEC AS -> a-tec.no, atec.no)
    m_prefix = re.match(r"^([a-zæøå])\s*-\s*([a-zæøå0-9]{2,20})\b", raw_name)
    if m_prefix:
        p_letter, p_word = m_prefix.group(1), m_prefix.group(2)
        for v in sorted(_slug_variants(f"{p_letter}-{p_word}") | _slug_variants(f"{p_letter}{p_word}"), key=len):
            out.append((f"{v}.no", "name_slug_guess"))
            out.append((f"{v}.com", "name_slug_guess"))

    # 3c. Dash or slash separated brand segments (e.g. AB TAKST - AKTIV BOLIGTAKSERING AS)
    if re.search(r"\s+[-–/]\s+", raw_name):
        for seg in re.split(r"\s+[-–/]\s+", raw_name):
            seg_clean = re.sub(r"\s+\b(?:asa|as|ans|da|ks|sa|ba|nuf|enk|ik|sf|kf|fkf|oy|ab|ltd|gmbh|inc|holding|konsern)\b\s*$", "", seg.strip())
            seg_tokens = [t for t in re.findall(r"[a-z0-9æøå]+", seg_clean) if len(t) > 1 and not t.isdigit()]
            if seg_tokens:
                for v in sorted(_slug_variants("".join(seg_tokens)) | (_slug_variants("-".join(seg_tokens)) if len(seg_tokens) > 1 else set()), key=len):
                    out.append((f"{v}.no", "brand_token_guess"))
                    out.append((f"{v}.com", "brand_token_guess"))

    # 3. Name slug candidates with Norwegian vowel digraphs and generic token stripping
    slug_tokens = raw_toks or tokens
    if slug_tokens:
        # Full name slug candidates
        joined = "".join(slug_tokens)
        hyphen = "-".join(slug_tokens)
        all_forms = _slug_variants(joined)
        if len(slug_tokens) > 1:
            all_forms.update(_slug_variants(hyphen))
        for slug in sorted(all_forms, key=len):
            out.append((f"{slug}.no", "name_slug_guess"))
            out.append((f"{slug}.com", "name_slug_guess"))

        # Compound second-token initialism (e.g. ÅSGREINA BYGGASSISTANSE AS -> aasgreinaba.no)
        if len(slug_tokens) == 2:
            t1_ascii = next(iter(sorted(_slug_variants(slug_tokens[1]), key=len)), "")
            for pref in COMPOUND_SECTOR_PREFIXES:
                if t1_ascii.startswith(pref) and len(t1_ascii) >= len(pref) + 4:
                    rest = t1_ascii[len(pref):]
                    for v0 in sorted(_slug_variants(slug_tokens[0]), key=len):
                        comb = f"{v0}{pref[0]}{rest[0]}"
                        if 5 <= len(comb) <= 30:
                            out.append((f"{comb}.no", "name_slug_guess"))
                    break

        # Core tokens without generic corporate terms (e.g. 'holding', 'invest')
        core = [
            rt for rt, nt in zip(slug_tokens, tokens)
            if nt not in GENERIC_SLUG_TOKENS and rt not in GENERIC_SLUG_TOKENS
        ]
        if core and len(core) < len(slug_tokens) and len(core) >= 1:
            c_joined = "".join(core)
            c_hyphen = "-".join(core)
            c_forms = _slug_variants(c_joined)
            if len(core) > 1:
                c_forms.update(_slug_variants(c_hyphen))
            for slug in sorted(c_forms, key=len):
                out.append((f"{slug}.no", "name_slug_guess"))
                out.append((f"{slug}.com", "name_slug_guess"))

        # Distinctive single brand tokens (length >= 4 and not in generic words)
        for rt, nt in zip(slug_tokens, tokens):
            if len(nt) >= 4 and nt not in GENERIC_SLUG_TOKENS and rt not in GENERIC_SLUG_TOKENS:
                for slug in sorted(_slug_variants(rt), key=len):
                    out.append((f"{slug}.no", "brand_token_guess"))
                    out.append((f"{slug}.com", "brand_token_guess"))

    # 4. Subunit name slug candidates (operating brand distinct from legal entity)
    for loc in (profile.get("evidence", {}).get("locations", {}).get("value") or {}).get("locations", []):
        loc_name = loc.get("name")
        if loc_name and loc_name.strip().casefold() != str(profile.get("name") or "").strip().casefold():
            sub_raw = _raw_tokens(loc_name)
            sub_norm = _tokens(loc_name)
            sub_tokens = sub_raw or sub_norm
            if sub_tokens:
                sub_core = [
                    rt for rt, nt in zip(sub_tokens, sub_norm)
                    if nt not in GENERIC_SLUG_TOKENS and rt not in GENERIC_SLUG_TOKENS
                ]
                sub_sets = [sub_tokens]
                if sub_core and len(sub_core) < len(sub_tokens) and len(sub_core) >= 1:
                    sub_sets.append(sub_core)
                for stset in sub_sets:
                    s_joined = "".join(stset)
                    s_hyphen = "-".join(stset)
                    s_forms = _slug_variants(s_joined)
                    if len(stset) > 1:
                        s_forms.update(_slug_variants(s_hyphen))
                    for slug in sorted(s_forms, key=len):
                        out.append((f"{slug}.no", "subunit_name_guess"))
                        out.append((f"{slug}.com", "subunit_name_guess"))

    # Prioritize .no hosts first (since >95% of Norwegian business sites use .no)
    no_hosts: list[tuple[str, str]] = []
    other_hosts: list[tuple[str, str]] = []
    seen = set()
    for host, method in out:
        if host not in seen:
            seen.add(host)
            if method in {"discovery_index", "registry_email_domain", "job_feed_homepage", "google_search_candidate", "brave_search_candidate"} or host.endswith(".no"):
                no_hosts.append((host, method))
            else:
                other_hosts.append((host, method))
    return (no_hosts + other_hosts)[:16]


def _is_care_of(profile: dict[str, Any] | None) -> bool:
    if not isinstance(profile, dict):
        return False
    ev = profile.get("evidence") or {}
    live_val = (ev.get("registry_live") or {}).get("value") or {}
    raw_val = (ev.get("registry") or {}).get("value") or {}
    reg_addr = (
        " ".join(
            str(x)
            for a in (live_val.get("business_address"), live_val.get("postal_address"))
            if isinstance(a, dict)
            for x in (a.get("adresse") or [])
        )
        + " "
        + str(raw_val.get("forretningsadresse.adresse") or "")
        + " "
        + str(raw_val.get("postadresse.adresse") or "")
    )
    return bool(re.search(r"\b(c/o|v/)", reg_addr, re.IGNORECASE))


STRUCTURAL_QUALIFIERS = {"holding", "eiendom", "eiendommer", "invest", "finans", "kapital"}
LABELED_ORG_RE = re.compile(
    r"(?:org\.?\s*(?:nr\.?|nummer)?|organisasjonsnummer|foretaksregisteret|\bno\b)\s*[:.]?\s*([89]\d{2}[\s.]?\d{3}[\s.]?\d{3}|\b[89]\d{8}\b)"
    r"|([89]\d{2}[\s.]?\d{3}[\s.]?\d{3}|\b[89]\d{8}\b)\s*mva\b",
    re.I,
)


def _has_foreign_labeled_org(text: str, allowed_orgs: set[str]) -> bool:
    for m in LABELED_ORG_RE.finditer(text):
        raw_num = m.group(1) or m.group(2) or ""
        digits = re.sub(r"\D", "", raw_num)
        if len(digits) == 9 and digits not in allowed_orgs:
            return True
    return False


def _accept_proof(
    proof: str | None,
    assessment: dict[str, Any],
    method: str,
    profile: dict[str, Any] | None = None,
) -> bool:
    """Exact org number always proves the entity. Address+postcode alone does not: managers, landlords,
    federations and care-of (c/o, v/) hosts share a registered address, so it is accepted only on a
    guessed/search domain whose page also carries most of the legal name, never on a registry e-mail
    domain or when the entity is registered c/o or v/ another party."""
    if not proof:
        return False
    if proof.startswith("exact organisation number"):
        return True
    if _is_care_of(profile):
        return False
    if isinstance(profile, dict):
        name_toks = set(_tokens(profile.get("name")))
        if name_toks & STRUCTURAL_QUALIFIERS:
            reasons = assessment.get("reasons") or []
            if "all normalized legal-name tokens appear together in homepage identity evidence" not in reasons:
                return False
    return method not in STRICT_METHODS and assessment.get("score", 0) >= 0.85


def discover_website(profile: dict[str, Any], max_tested_hosts: int = 3, timeout: float = 8.0) -> tuple[dict[str, Any] | None, dict[str, int]]:
    """First candidate host with hard proof of the exact entity, else None.

    ``metrics["transient"]`` counts resolving candidates whose homepage fetch failed with a network error or
    timeout, so the caller can retry only those companies once, more slowly, instead of losing a site to load.
    """
    effective_timeout = max(timeout, 18.0) if timeout > 8.0 else timeout
    metrics = {"requests": 0, "bytes": 0, "transient": 0}
    net_errors_before = _transient_count()
    tested = 0
    _NET.search_requests = 0
    _NET.search_timeout = effective_timeout
    hosts = candidate_hosts(profile)
    metrics["requests"] += getattr(_NET, "search_requests", 0)
    index_pages = {h: u for h, u in discovery_index.candidates(profile)}
    for host, method in hosts:
        if not _resolves(host):
            continue
        tested += 1
        record, m = fetch_website(host, timeout=effective_timeout, crawl_secondary=False)
        metrics["requests"] += m["requests"]
        metrics["bytes"] += m["bytes"]
        if record.get("status") == "source_error" and _is_transient(str(record.get("note") or "")):
            metrics["transient"] += 1
        if record.get("status") != "available":
            if tested >= max_tested_hosts:
                break
            continue
        gated = apply_website_identity_gate(profile, record)
        assessment = gated["assessment"] or {}
        exact_only = method in EXACT_ONLY_METHODS
        ok = False if exact_only else (bool(assessment.get("publishable")) if method in STRICT_METHODS else assessment.get("score") == 1.0)
        if not ok and (exact_only or assessment.get("score", 0) >= 0.3):
            weak = assessment.get("score", 0) < 0.5 and not exact_only
            hint = index_pages.get(host) if exact_only else None
            proof, pm = hard_identity_proof(profile, record, WEAK_CANDIDATE_PROOF_PAGES if weak else MAX_PROOF_PAGES, effective_timeout,
                                            **({"extra_urls": [hint]} if hint else {}))
            metrics["requests"] += pm["requests"]
            if (record.get("value") or {}).get("pages"):
                gated = apply_website_identity_gate(profile, record)
                assessment = gated["assessment"] or {}
            ok = _accept_proof(proof, assessment, method, profile)
            if exact_only:
                ok = bool(proof and proof.startswith("exact organisation number"))
            if ok:
                exact = proof.startswith("exact organisation number")
                assessment["publishable"] = True
                assessment["score"] = 1.0 if exact else 0.9
                assessment["status"] = "exact" if exact else "review"
                assessment["reasons"] = list(assessment.get("reasons", [])) + [proof]
                val = record.setdefault("value", {})
                val["identity_assessment"] = assessment
                if val.get("social_link_assessments") and not val.get("social_links"):
                    val["social_links"] = [
                        {"platform": item["platform"], "url": item["url"]}
                        for item in val["social_link_assessments"]
                        if item.get("publishable")
                    ]
        if not ok:
            if tested >= max_tested_hosts:
                break
            continue
        record["source_type"] = record["source_class"] = "discovered_company_website"
        source = ("candidate from the declared Common Crawl discovery index, identity verified live"
                  if method == "discovery_index" else f"Discovered via {method}")
        record["note"] = (f"{source}; identity: {'; '.join(assessment.get('reasons', []))}. "
                          "Company-controlled claim layer; not an official registry fact.")
        return record, metrics
    metrics["transient"] += _transient_count() - net_errors_before
    return None, metrics


PROOF_PATHS = ("/kontakt", "/kontakt-oss", "/om-oss", "/om", "/contact", "/about", "/about-us", "/personvern", "/personvernerklaering",
               "/privacy", "/privacy-policy", "/vilkar", "/vilkaar", "/handlebetingelser", "/salgsbetingelser", "/impressum", "/cookies")
PROOF_LINK_TERMS = ("kontakt", "contact", "om-oss", "omoss", "om oss", "about", "personvern", "privacy", "vilk", "terms", "betingelser",
                    "impressum", "cookie", "fakturering", "selskap", "company")
MAX_PROOF_PAGES = 4
MAX_FEED_ITEMS = 20  # dated items kept per site feed; the feed is already downloaded, so more items cost no requests
WEAK_CANDIDATE_PROOF_PAGES = 2  # name score < 0.5: most such pages are look-alikes, keep the proof cheap


def _proof_links(root: str, html: str) -> list[str]:
    """Same-host links on the homepage whose address or text suggests a contact / legal / about page."""
    host = urllib.parse.urlparse(root).netloc.lower().removeprefix("www.")
    found: list[str] = []
    seen_norm: set[str] = set()
    for a in BeautifulSoup(html, "lxml").find_all("a", href=True):
        href = urllib.parse.urljoin(root + "/", a["href"].strip()).split("#")[0]
        parsed = urllib.parse.urlparse(href)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower().removeprefix("www.") != host:
            continue
        label = (parsed.path + " " + a.get_text(" ", strip=True)).casefold()
        norm_href = (href.rstrip("/") or href).casefold()
        if any(term in label for term in PROOF_LINK_TERMS) and norm_href not in seen_norm:
            seen_norm.add(norm_href)
            found.append(href)
    return found


def _extract_company_proof_targets(profile: dict[str, Any]) -> dict[str, Any]:
    org = re.sub(r"\D", "", str(profile.get("organisation_number") or ""))
    allowed_orgs: set[str] = {org} if len(org) == 9 else set()
    for loc in (profile.get("evidence", {}).get("locations", {}).get("value") or {}).get("locations", []):
        if isinstance(loc, dict):
            sub_org = re.sub(r"\D", "", str(loc.get("organisation_number") or ""))
            if len(sub_org) == 9:
                allowed_orgs.add(sub_org)
    for sub in profile.get("sub_units") or []:
        if isinstance(sub, dict):
            sub_org = re.sub(r"\D", "", str(sub.get("organisation_number") or ""))
            if len(sub_org) == 9:
                allowed_orgs.add(sub_org)
    targets: dict[str, Any] = {
        "org": org,
        "allowed_orgs": allowed_orgs,
        "streets": [],
        "zipc": "",
    }
    # from registry_live
    live = (profile.get("evidence", {}).get("registry_live", {}).get("value") or {})
    b_addr = live.get("business_address") or {}
    p_addr = live.get("postal_address") or {}
    for addr in (b_addr, p_addr):
        if isinstance(addr, dict):
            for line in (addr.get("adresse") or []):
                if isinstance(line, str) and len(line.strip()) >= 3:
                    targets["streets"].append(line.strip().casefold())
            if not targets["zipc"] and addr.get("postnummer"):
                targets["zipc"] = str(addr.get("postnummer")).strip()

    # from raw registry
    raw = (profile.get("evidence", {}).get("registry", {}).get("value") or {})
    for key, val in raw.items():
        if "adresse" in key and isinstance(val, str) and len(val.strip()) >= 3:
            targets["streets"].append(val.strip().casefold())
        if "postnummer" in key and isinstance(val, str) and not targets["zipc"]:
            targets["zipc"] = val.strip()

    targets["streets"] = list(dict.fromkeys(targets["streets"]))
    return targets


def hard_identity_proof(
    profile: dict[str, Any],
    website: dict[str, Any],
    max_pages: int = MAX_PROOF_PAGES,
    timeout: float = 8.0,
    extra_urls: list[str] | None = None,
) -> tuple[str | None, dict[str, int]]:
    """Exact org number, or registered street address + postcode. Phone/leader matches are NOT proof (sister companies share them).

    Looks at the homepage, then contact/about/legal pages linked from it, then a short list of conventional paths.
    The org number must stand alone (not be part of a longer digit string) so a phone number cannot match.
    If a page prints a foreign org number under an org-number label (and not our own), address-only proof is rejected.
    """
    metrics = {"requests": 0}
    targets = _extract_company_proof_targets(profile)
    org = targets["org"]
    allowed_orgs = targets["allowed_orgs"]
    zipc = targets["zipc"]
    base = website.get("source_url") or ""
    parsed = urllib.parse.urlparse(base)
    root = f"{parsed.scheme}://{parsed.netloc}"
    org_re = re.compile(rf"(?<!\d){org}(?!\d)") if len(org) == 9 else None
    seen: set[str] = set()
    queue = [base]
    host = parsed.netloc.lower().removeprefix("www.")
    # Pages already known to carry the org number (crawl cache), same site only, right after the homepage
    for extra in extra_urls or []:
        extra_host = (urllib.parse.urlparse(extra).hostname or "").lower().removeprefix("www.")
        if host and (extra_host == host or extra_host.endswith("." + host) or host.endswith("." + extra_host)):
            queue.append(extra)
    fetched = 0
    first_html = None
    foreign_org_seen = False
    wval = website.get("value") if isinstance(website.get("value"), dict) else None
    while queue and fetched < max_pages:
        url = queue.pop(0)
        norm_url = (url.rstrip("/") or url).casefold()
        if norm_url in seen:
            continue
        seen.add(norm_url)
        _NET.last_requests = 1
        body, final_url = _get(url, timeout)
        if fetched:
            metrics["requests"] += getattr(_NET, "last_requests", 1)
        fetched += 1
        if not body:
            if first_html is None and url == base:
                queue.extend(root + path for path in PROOF_PATHS)
            continue
        html = body.decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text(" ", strip=True)
        if fetched > 1 and wval is not None and isinstance(wval.get("pages"), list):
            resolved_url = final_url or url
            if not any((p.get("url") or "").rstrip("/") == resolved_url.rstrip("/") for p in wval["pages"]):
                wval["pages"].append({
                    "url": resolved_url,
                    "title": soup.title.get_text(" ", strip=True)[:500] if soup.title else "",
                    "main_text_excerpt": text[:5000],
                    "content_sha256": hashlib.sha256(body).hexdigest(),
                })
            page_socials = _social_links(resolved_url, soup)
            if page_socials:
                disc = list(wval.get("discovered_social_links") or wval.get("social_links") or [])
                for sl in page_socials:
                    if not any(d.get("url") == sl["url"] for d in disc):
                        disc.append(sl)
                wval["discovered_social_links"] = disc
        squeezed = re.sub(r"(?<=\d)[\s.\-_](?=\d)", "", text)
        raw_squeezed = re.sub(r"(?<=\d)[\s.\-_](?=\d)", "", html)
        if org_re and (org_re.search(squeezed) or org_re.search(raw_squeezed)):
            return f"exact organisation number printed on {url}", metrics
        if _has_foreign_labeled_org(text, allowed_orgs):
            foreign_org_seen = True
        if (
            not foreign_org_seen
            and zipc
            and any(len(s) >= 6 and s in text.casefold() for s in targets["streets"])
            and re.search(rf"(?<!\d){zipc}(?!\d)", text)
        ):
            return f"registered street address and postcode printed on {url}", metrics
        if first_html is None:
            first_html = html
            queue.extend(_proof_links(root, html)[:4])
            queue.extend(root + path for path in PROOF_PATHS)
    return None, metrics


_NET = threading.local()  # per-thread count of timeouts / dropped connections seen by _get (not 404s or policy refusals)
TRANSIENT_MARKERS = ("timed out", "timeout", "reset", "remote end closed", "remotedisconnected", "incompleteread", "temporarily")


def _is_transient(exc: BaseException | str) -> bool:
    text = (type(exc).__name__ + " " + str(exc)) if isinstance(exc, BaseException) else str(exc)
    return any(m in text.casefold() for m in TRANSIENT_MARKERS)


def _transient_count() -> int:
    return getattr(_NET, "count", 0)


def _get(url: str, timeout: float = 8.0, max_bytes: int = 2_000_000) -> tuple[bytes | None, str | None]:
    if url.endswith("/robots.txt"):
        cached_robots = _get_cached_robots_body(url)
        if cached_robots is not None:
            _NET.last_requests = 0
            return (cached_robots[:max_bytes] if cached_robots else None), url
    cached = _get_cached_page(url)
    if cached is not None:
        _NET.last_requests = 0
        return cached[0][:max_bytes], cached[1]
    r_req = 0
    try:
        assert_public_url(url)
        allowed, r_req = _robots_check(url, timeout)
        if not allowed:
            _NET.last_requests = r_req
            return None, None
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with SAFE_OPENER.open(req, timeout=timeout) as r:
            final = r.geturl()
            assert_public_url(final)
            raw = r.read(max_bytes)
            ctype = (r.headers.get("content-type", "") or "").lower()
        _NET.last_requests = r_req + 1
        if raw and ("html" in ctype or "xml" in ctype or "json" in ctype or "text" in ctype or not ctype):
            _cache_page(url, raw, final)
        return raw, final
    except urllib.error.HTTPError:
        _NET.last_requests = r_req + 1
        return None, None
    except Exception as exc:
        _NET.last_requests = r_req + 1
        if _is_transient(exc) or isinstance(exc, (TimeoutError, ConnectionError)):
            _NET.count = _transient_count() + 1
        return None, None


import urllib.error  # noqa: E402
import urllib.request  # noqa: E402  (kept after helpers to keep imports grouped above minimal)


NEWS_SEGMENTS = {
    "nyheter", "nyhet", "nytt", "siste-nytt", "news", "aktuelt", "blogg", "blog",
    "presse", "press", "pressemeldinger", "pressemelding", "artikler", "artikkel", "innlegg", "post", "posts",
}
NOT_NEWS_SEGMENTS = {
    "produkt", "produkter", "product", "products", "shop", "butikk",
    "kategori", "kategorier", "category", "categories", "collections", "brands", "merker",
    "tag", "tags", "post-tag", "post-tags", "author", "forfatter", "page", "side", "arkiv", "archive", "feed",
}
CAREER_SEGMENTS = {
    "karriere", "career", "careers", "ledige-stillinger", "ledig-stilling", "stilling-ledig", "stillinger-ledige", "stillinger",
    "ledige-jobber", "jobbe-hos-oss", "jobb-hos-oss", "work-with-us", "join-us",
    "rekruttering", "vacancies", "vacancy", "jobs", "job", "jobb",
}
CAREER_TEXT_RE = re.compile(
    r"\b(karriere|careers?|ledige?\s+stillinger?|ledige\s+jobber|jobbe?\s+hos\s+oss|rekruttering|vacanc(?:y|ies)|jobs?|jobb)\b",
    re.I,
)
NEWS_TEXT_RE = re.compile(r"\b(nyheter|siste\s+nytt|news|aktuelt|blogg|blog|presse|press)\b", re.I)
SITEMAP_NEWS_FILE_RE = re.compile(r"(nyhet|news|blogg?|aktuelt|presse|press|artikkel|innlegg|post)", re.I)
PHONE_LABEL_RE = re.compile(
    r"(?<!\w)(?:tlf\.?|tel\.?|telefon|phone|mobil|sentralbord|ring(?:\s+oss)?|kontakt(?:\s+oss)?(?:\s+p[aå])?)\b[\s.:]*((?:(?:\(\+47\)|\+47|0047)\s*)?[\s.\-]?(?:[2-9]\d[\s.\-]?\d{2}[\s.\-]?\d{2}[\s.\-]?\d{2}|[2-9]\d{2}[\s.\-]?\d{2}[\s.\-]?\d{3}|\d{8}))",
    re.I,
)
ATS_DOMAINS = (
    "finn.no", "teamtailor.com", "recman.no", "recman.biz", "jobbnorge.no",
    "webcruiter.no", "webcruiter.com", "reachmee.com", "easycruit.com",
    "varbi.com", "workbuster.com", "workbuster.se", "workbuster.no",
    "jobylon.com", "bamboohr.com", "myworkdayjobs.com", "workday.com",
    "lever.co", "greenhouse.io", "personio.de", "personio.com",
    "talentech.com", "karrierestart.no", "homerun.co",
)


def _segments(path: str) -> list[str]:
    return [s.casefold() for s in path.split("/") if s]


def _is_career(path: str, text: str = "") -> bool:
    """Careers entry point: a whole path segment or whole anchor word, never a substring ('faa-jobben-gjort')."""
    return any(s in CAREER_SEGMENTS for s in _segments(path)) or bool(CAREER_TEXT_RE.search(text))


def _is_news(path: str, text: str = "") -> bool:
    """News section: whole path segment or anchor word, and not a product/shop/tag/category/pagination page."""
    segs = _segments(path)
    if any(s in NOT_NEWS_SEGMENTS for s in segs):
        return False
    if len(segs) >= 2 and segs[-1].isdigit():
        return False
    if segs and re.fullmatch(r"[a-z]{2}(-[a-z]{2})?", segs[0]):
        segs = segs[1:]  # language prefix: /no/nyheter
    # A news section is top-level (/nyheter, /blogg/slug); '/tapet/fibertapet/nyheter' is a product listing.
    return bool(segs and segs[0] in NEWS_SEGMENTS) or (len(segs) <= 1 and bool(NEWS_TEXT_RE.search(text)))


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _clean_contact(value: str, limit: int) -> str:
    return re.sub(r"\s+", "", urllib.parse.unquote(value)).strip()[:limit]


def _norm_phone(value: str) -> str | None:
    """Canonical +47XXXXXXXX for a valid Norwegian number; anything else is not published."""
    digits = re.sub(r"\D", "", urllib.parse.unquote(value))
    if digits.startswith("0047"):
        digits = digits[4:]
    elif digits.startswith("47") and len(digits) == 10:
        digits = digits[2:]
    return "+47" + digits if len(digits) == 8 and digits[0] in "23456789" else None


EMAIL_TEXT_RE = re.compile(
    r"\b([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9\-]*[a-zA-Z0-9])?)*\.[a-zA-Z]{2,15})\b"
)
FREE_MAIL_DOMAINS = {
    "gmail.com", "hotmail.com", "outlook.com", "online.no",
    "yahoo.com", "live.no", "icloud.com",
}
EMAIL_LABEL_RE = re.compile(r"(?<!\w)(?:e-?post(?:adresse)?|e-?mail)\s*[:\-]?\s*", re.I)


def _phone_from_text(text: str) -> tuple[str, str] | None:
    """A Norwegian number printed next to a phone label or with explicit country code on a verified company page: (number, literal snippet)."""
    for m in PHONE_LABEL_RE.finditer(text):
        phone = _norm_phone(m.group(1))
        if phone:
            return phone, m.group(0).strip()[:160]
    for m in re.finditer(r"(?<!\w)(?:\(\+47\)|\+47)[\s.\-]*([2-9]\d[\s.\-]?\d{2}[\s.\-]?\d{2}[\s.\-]?\d{2}|[2-9]\d{2}[\s.\-]?\d{2}[\s.\-]?\d{3})\b", text):
        pre_ctx = text[max(0, m.start() - 60):m.start()].lower()
        if any(credit in pre_ctx for credit in ("levert av", "design av", "utviklet av", "nettside av", "webdesign", "produsert av")):
            continue
        phone = _norm_phone(m.group(0))
        if phone:
            return phone, m.group(0).strip()[:160]
    return None


def _email_from_text(text: str, site_domain: str) -> tuple[str, str] | None:
    """Plain-text email printed on a verified company page.

    Prefers site's own registered domain; accepts domain-stem freemail only when explicitly labeled.
    """
    if not site_domain:
        return None
    site_dom = site_domain.lower().removeprefix("www.")
    for m in EMAIL_TEXT_RE.finditer(text):
        addr = m.group(1).strip(".,;:()[]<>\"'").lower()
        if "@" not in addr:
            continue
        email_host = addr.rsplit("@", 1)[1]
        if email_host == site_dom or email_host.endswith("." + site_dom):
            start = max(0, m.start() - 30)
            end = min(len(text), m.end() + 30)
            return addr[:120], text[start:end].strip()[:160]

    stem = site_dom.split(".")[0].lower()
    clean_stem = re.sub(r"[._\-]", "", stem).lower()
    if len(clean_stem) >= 6:
        for m in EMAIL_TEXT_RE.finditer(text):
            addr = m.group(1).strip(".,;:()[]<>\"'").lower()
            if "@" not in addr:
                continue
            local_part, email_host = addr.rsplit("@", 1)
            if email_host in FREE_MAIL_DOMAINS:
                clean_local = re.sub(r"[._\-]", "", local_part).lower()
                if clean_local == clean_stem:
                    pre_text = text[max(0, m.start() - 40):m.start()]
                    label_match = list(EMAIL_LABEL_RE.finditer(pre_text))
                    if label_match and label_match[-1].end() == len(pre_text):
                        full_label = label_match[-1].group(0)
                        span = f"{full_label}{m.group(1).strip('.,;:()[]<>\"\'')}"
                        return addr[:120], span[:160]
    return None


CREDIT_CONTEXT = ("levert av", "design av", "utviklet av", "nettside av", "webdesign", "produsert av", "powered by", "laget av")
MAX_CONTACTS = 8


def page_contacts(html: str, base_domain: str, registry_email: str | None) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Every email and phone a verified company page prints: ([(email, span)], [(phone, span)]).

    Emails count only on the site's own domain (or the registry's own address); phones from tel: links or next to a
    phone label. Web-agency credits ("levert av", "design av", ...) are skipped. Spans are literal page text or markup."""
    soup = BeautifulSoup(html, "lxml")
    dom = (base_domain or "").casefold().removeprefix("www.")
    reg = (registry_email or "").strip().casefold()
    emails: dict[str, str] = {}
    phones: dict[str, str] = {}

    def own(addr: str) -> bool:
        host = addr.rsplit("@", 1)[-1]
        return bool(dom) and (host == dom or host.endswith("." + dom)) or (bool(reg) and addr == reg)

    def credited(context: str) -> bool:
        return any(c in context.casefold() for c in CREDIT_CONTEXT)

    for a in soup.select("a[href]"):
        href = str(a.get("href") or "").strip()
        ctx = " ".join(x.get_text(" ", strip=True) for x in (a.parent,) if x is not None)[:300]
        if href.lower().startswith("mailto:"):
            addr = _clean_contact(href[7:].split("?", 1)[0], 120).casefold()
            if "@" in addr and "@." not in addr and own(addr) and not credited(ctx):
                emails.setdefault(addr, str(a))
        elif href.lower().startswith("tel:"):
            phone = _norm_phone(href[4:])
            if phone and not credited(ctx):
                phones.setdefault(phone, str(a))
    text = soup.get_text(" ", strip=True)
    for m in EMAIL_TEXT_RE.finditer(text):
        addr = m.group(1).strip(".,;:()[]<>\"'").casefold()
        if own(addr) and not credited(text[max(0, m.start() - 60):m.start()]):
            emails.setdefault(addr, text[max(0, m.start() - 30):m.end() + 30].strip())
    for m in PHONE_LABEL_RE.finditer(text):
        phone = _norm_phone(m.group(1))
        if phone and not credited(text[max(0, m.start() - 60):m.start()]):
            phones.setdefault(phone, m.group(0).strip())
    # Role addresses (post@, info@, ...) before personal ones.
    order = sorted(emails.items(), key=lambda kv: (0 if kv[0].split("@")[0] in {"post", "info", "kontakt", "contact", "firmapost", "postmottak", "hello", "hei", "booking", "salg", "sales", "kundeservice", "service"} else 1, kv[0]))
    return [(k, " ".join(v.split())[:240]) for k, v in order][:MAX_CONTACTS], [(k, " ".join(v.split())[:240]) for k, v in sorted(phones.items())][:MAX_CONTACTS]


def _script_texts(soup: BeautifulSoup | None) -> str:
    if soup is None:
        return ""
    scripts = soup.find_all("script")
    return " ".join(s.get_text() for s in scripts if s.get_text())


def _find_social_span(key: str, text: str) -> str | None:
    """Find the literal span in HTML/JSON text for a normalized social URL key (handling escaped slashes, plugin URLs, and twitter.com)."""
    if not key or not text:
        return None
    keys = [key]
    if key.startswith("x.com/"):
        keys.append("twitter.com/" + key[6:])
    patterns: list[str] = []
    for k in keys:
        patterns.append(re.escape(k))
        patterns.append(re.escape(k).replace("/", r"\\?/"))
        patterns.append(re.escape(urllib.parse.quote(k, safe="")))
    for pat in patterns:
        hit = re.search(r"[^\"'\s<>]*" + pat + r"[^\"'\s<>]*", text, re.I)
        if hit:
            return hit.group(0)
    return None


def _same_site(url: str, host: str) -> bool:
    return urllib.parse.urlparse(url).netloc.lower().removeprefix("www.") == host.removeprefix("www.")


def _sitemap_entries(raw: bytes) -> tuple[str, list[tuple[str, str]]]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return "", []
    kind = root.tag.rsplit("}", 1)[-1]
    rows = []
    for node in root:
        loc = next((c.text.strip() for c in node if c.tag.rsplit("}", 1)[-1] == "loc" and c.text), "")
        mod = next((c.text.strip()[:10] for c in node if c.tag.rsplit("}", 1)[-1] == "lastmod" and c.text), "")
        if loc:
            rows.append((loc, mod))
    return kind, rows


def _sitemap_signals(final: str, host: str) -> tuple[list[str], list[str], list[dict[str, str]], int, dict[str, dict[str, str]]]:
    """Careers/news entry points and dated posts from the verified site's own sitemap (<= 4 fetches).

    The last value maps each returned URL to the sitemap file it was read from (url, sha256, literal <loc> span).
    """
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(final))
    _NET.last_requests = 1
    robots, _ = _get(origin + "/robots.txt", timeout=5.0)
    requests = getattr(_NET, "last_requests", 1)
    starts = [m for m in re.findall(r"(?im)^sitemap:\s*(\S+)", (robots or b"").decode("utf-8", "replace")) if _same_site(m, host)]
    queue = starts[:2] or [origin + "/sitemap.xml"]
    careers: list[str] = []
    news: list[str] = []
    dated: list[dict[str, str]] = []
    prov: dict[str, dict[str, str]] = {}
    fetched = 0
    while queue and fetched < 3:
        sm_url = queue.pop(0)
        _NET.last_requests = 1
        raw, sm_final = _get(sm_url, timeout=5.0)
        fetched += 1
        requests += getattr(_NET, "last_requests", 1)
        if not raw:
            continue
        kind, rows = _sitemap_entries(raw)
        if kind == "sitemapindex":
            wanted = [loc for loc, _ in rows if _same_site(loc, host) and SITEMAP_NEWS_FILE_RE.search(loc)]
            queue.extend(wanted[:2])
            continue
        for loc, mod in rows:
            if not _same_site(loc, host):
                continue
            path = urllib.parse.urlparse(loc).path
            src = {"source_url": sm_final or sm_url, "sha256": _sha(raw), "span": f"<loc>{loc}</loc>"[:240]}
            if _is_career(path) and loc not in careers:
                careers.append(loc)
                prov[loc] = src
            if _is_news(path) and len(_segments(path)) >= 2 and loc not in news:
                news.append(loc)
                prov[loc] = src
                slug = path.rstrip("/").rsplit("/", 1)[-1]
                if mod and re.match(r"\d{4}-\d{2}-\d{2}", mod) and slug and not slug.isdigit():
                    dated.append({"title": slug.replace("-", " ").replace("_", " ")[:200], "url": loc, "date": mod,
                                  "date_kind": "sitemap_lastmod", "title_source": "url_slug"})
    dated.sort(key=lambda d: d["date"], reverse=True)
    return careers[:4], news[:3], dated[:5], requests, prov


def _wp_posts(final: str) -> tuple[list[dict[str, str]], dict[str, str] | None]:
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(final))
    api = origin + "/wp-json/wp/v2/posts?per_page=5&_fields=date,link,title"
    raw, api_final = _get(api, timeout=5.0)
    try:
        data = json.loads(raw or b"[]")
    except ValueError:
        return [], None
    items = []
    for post in data if isinstance(data, list) else []:
        title = ((post.get("title") or {}).get("rendered") or "").strip()
        if title and post.get("link") and post.get("date"):
            items.append({"title": " ".join(BeautifulSoup(title, "lxml").get_text().split())[:200],
                          "url": str(post["link"]), "date": str(post["date"])[:10], "date_kind": "wp_rest_post_date"})
    return items, ({"source_url": api_final or api, "sha256": _sha(raw or b"")} if items else None)


def _parse_feed(raw: bytes, base: str) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return items
    for node in root.iter():
        tag = node.tag.rsplit("}", 1)[-1]
        if tag not in {"item", "entry"}:
            continue
        get = lambda *names: next((c for c in node if c.tag.rsplit("}", 1)[-1] in names), None)  # noqa: E731
        title, link, date = get("title"), get("link"), get("pubDate", "published", "updated", "date")
        href = (link.get("href") if link is not None and link.get("href") else (link.text if link is not None else "")) or ""
        when = ""
        if date is not None and date.text:
            try:
                when = parsedate_to_datetime(date.text).date().isoformat()
            except Exception:
                when = date.text.strip()[:10]
        if title is not None and title.text and href and when:
            items.append({"title": " ".join(title.text.split())[:200], "url": urllib.parse.urljoin(base, href.strip()), "date": when})
        if len(items) >= MAX_FEED_ITEMS:
            break
    return items


ARTICLE_TYPES = {"newsarticle", "article", "blogposting", "reportagenewsarticle", "pressrelease"}
NOT_ARTICLE_SEGMENTS = {
    "page", "side", "tag", "tags", "post-tag", "post-tags",
    "kategori", "kategorier", "category", "categories", "author", "forfatter", "feed", "arkiv", "archive",
}


def _iso_date(text: str) -> str:
    """YYYY-MM-DD from an ISO/RFC date string, or '' when it is not a plausible past publication date."""
    text = (text or "").strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not m:
        try:
            when = parsedate_to_datetime(text).date().isoformat()
        except Exception:
            return ""
    else:
        when = m.group(0)
    if not ("2000-01-01" <= when <= utc_now()[:10]):
        return ""
    return when


def _article_meta(raw: bytes) -> tuple[str, str, str, str] | None:
    """(title, publication date, date source, literal span) declared by the article page itself, else None.

    Only explicit publication markup counts (JSON-LD datePublished, article:published_time, <time datetime>
    inside the article); modification dates and sitemap lastmod are never used as a publication date.
    """
    soup = BeautifulSoup(raw.decode("utf-8", errors="replace"), "lxml")
    title, date, kind, span = "", "", "", ""
    for s in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(s.string or s.get_text() or "{}")
        except ValueError:
            continue
        nodes = data if isinstance(data, list) else (data.get("@graph") if isinstance(data, dict) and isinstance(data.get("@graph"), list) else [data])
        for node in nodes:
            if not isinstance(node, dict):
                continue
            types = node.get("@type")
            types = {str(t).casefold() for t in (types if isinstance(types, list) else [types])}
            if types & ARTICLE_TYPES and node.get("datePublished"):
                date = _iso_date(str(node["datePublished"]))
                if date:
                    title = str(node.get("headline") or node.get("name") or "")
                    kind, span = "jsonld_datePublished", f'"datePublished": "{node["datePublished"]}"'
                    break
        if date:
            break
    if not date:
        meta = soup.select_one('meta[property="article:published_time"], meta[itemprop="datePublished"]')
        if meta and _iso_date(str(meta.get("content") or "")):
            date, kind, span = _iso_date(str(meta.get("content"))), "meta_published_time", str(meta)
    if not date:
        t = soup.select_one('article time[datetime], time[itemprop="datePublished"][datetime], time.published[datetime], time.entry-date[datetime], main time[datetime]')
        if t and _iso_date(str(t.get("datetime") or "")):
            date, kind, span = _iso_date(str(t.get("datetime"))), "article_time_element", str(t)
    if not date:
        return None
    if not title:
        og = soup.select_one('meta[property="og:title"]')
        h1 = soup.find("h1")
        title = (str(og.get("content") or "") if og else "") or (h1.get_text(" ", strip=True) if h1 else "")
    title = " ".join(BeautifulSoup(title, "lxml").get_text().split())[:200] if title else ""
    return (title, date, kind, span) if title else None


def _article_links(raw: bytes, base: str, host: str, section: str) -> list[str]:
    """Links under a news section (same host, one level deeper, no pagination/tag/category pages)."""
    seg0 = _segments(urllib.parse.urlparse(section).path)
    out: list[str] = []
    for a in BeautifulSoup(raw.decode("utf-8", errors="replace"), "lxml").select("a[href]"):
        url = urllib.parse.urljoin(base, str(a.get("href") or "")).split("#", 1)[0]
        p = urllib.parse.urlparse(url)
        segs = _segments(p.path)
        if (p.scheme in {"http", "https"} and _same_site(url, host) and len(segs) > len(seg0) and segs[:len(seg0)] == seg0
                and not any(s in NOT_ARTICLE_SEGMENTS for s in segs) and not segs[-1].isdigit() and url not in out):
            out.append(url)
    return out


def _news_articles(sections: list[str], candidates: list[str], host: str, max_articles: int = 4) -> tuple[list[dict[str, str]], list[dict[str, str]], int]:
    """Article-level news items (title, direct URL, publication date) read from each article page itself.

    Returns (items, provenance per item, requests used). At most one section page and ``max_articles`` article pages.
    """
    requests = 0
    urls = [
        u for u in candidates
        if not any(s in NOT_ARTICLE_SEGMENTS for s in _segments(urllib.parse.urlparse(u).path))
        and not (_segments(urllib.parse.urlparse(u).path) or [""])[-1].isdigit()
    ]
    if sections and len(urls) < max_articles:
        _NET.last_requests = 1
        sraw, sfinal = _get(sections[0], timeout=5.0)
        requests += getattr(_NET, "last_requests", 1)
        if sraw:
            urls += [u for u in _article_links(sraw, sfinal or sections[0], host, sfinal or sections[0]) if u not in urls]
    items, prov = [], []
    for url in urls[:max_articles]:
        _NET.last_requests = 1
        araw, afinal = _get(url, timeout=5.0)
        requests += getattr(_NET, "last_requests", 1)
        meta = _article_meta(araw) if araw else None
        if not meta:
            continue
        title, date, kind, span = meta
        direct = afinal or url
        direct_segs = _segments(urllib.parse.urlparse(direct).path)
        if not _same_site(direct, host) or any(s in NOT_ARTICLE_SEGMENTS for s in direct_segs) or (direct_segs and direct_segs[-1].isdigit()) or any(i["url"] == direct for i in items):
            continue
        items.append({"title": title, "url": direct, "date": date, "date_kind": kind})
        prov.append({"item": direct, "source_url": direct, "sha256": _sha(araw), "span": f"{title} | {span}"})
    items.sort(key=lambda i: i["date"], reverse=True)
    return items, prov, requests


def extract_footprint(profile: dict[str, Any], website: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Dated activity, careers entry points, contact and company-owned profiles from a VERIFIED website only.

    ``item_sources`` records, per published item, the exact page it was read from (source_url, sha256 of the
    bytes fetched, literal span) so every claim can cite the page that really contains it.
    """
    metrics = {"requests": 0, "bytes": 0}
    source = website.get("source_url") or ""
    value = website.get("value") or {}
    assessment = value.get("identity_assessment") or {}
    if website.get("status") == "available" and assessment.get("publishable") is not True and assessment.get("score", 0) >= 0.3 and source:
        proof, pm = hard_identity_proof(profile, website, MAX_PROOF_PAGES, 8.0)
        metrics["requests"] += pm["requests"]
        if _accept_proof(proof, assessment, "registry_email_domain"):
            assessment["publishable"] = True
            assessment["score"] = 1.0
            assessment["status"] = "exact"
            assessment["reasons"] = list(assessment.get("reasons", [])) + [proof]
            value["identity_assessment"] = assessment
            if value.get("social_link_assessments") and not value.get("social_links"):
                value["social_links"] = [
                    {"platform": item["platform"], "url": item["url"]}
                    for item in value["social_link_assessments"]
                    if item.get("publishable")
                ]
    if website.get("status") != "available" or (value.get("identity_assessment") or {}).get("publishable") is not True:
        return evidence("external_footprint", "not_found", "company_owned_site", "https://data.brreg.no",
                        note="No identity-verified company website; nothing published."), metrics
    _NET.last_requests = 2
    raw, final = _get(source)
    metrics["requests"] += getattr(_NET, "last_requests", 2)
    careers, news_pages, feeds, contact, job_postings = [], [], [], {}, []
    contact_links = []
    item_sources: dict[str, list[dict[str, str]]] = {}

    def note(field: str, item: str, src_url: str, sha: str, span: str) -> None:
        item_sources.setdefault(field, []).append({"item": item, "source_url": src_url, "sha256": sha, "span": " ".join(span.split())[:240]})

    home_sha = _sha(raw) if raw else ""
    base_domain = _registered_domain(final) if final else ""
    page_text = raw.decode("utf-8", errors="replace") if raw else ""
    soup = BeautifulSoup(page_text, "lxml") if (raw and final) else None
    if raw and final and soup is not None:
        metrics["bytes"] += len(raw)
        host = urllib.parse.urlparse(final).netloc.lower()
        for a in soup.select("a[href]"):
            url = urllib.parse.urljoin(final, str(a.get("href") or ""))
            p = urllib.parse.urlparse(url)
            if p.scheme == "mailto" and "email" not in contact:
                cand_em = _clean_contact(p.path, 120)
                if "@" in cand_em and "@." not in cand_em:
                    contact["email"] = cand_em
                    note("contact_email", contact["email"], final, home_sha, str(a))
            elif p.scheme == "tel" and "phone" not in contact:
                phone = _norm_phone(p.path)
                if phone:
                    contact["phone"] = phone
                    note("contact_phone", phone, final, home_sha, str(a))
            if p.scheme not in {"http", "https"}:
                continue
            text = a.get_text(" ", strip=True)
            p_host = p.netloc.lower()
            is_same_domain = bool(base_domain and (p_host == base_domain or p_host.endswith("." + base_domain)))
            if is_same_domain:
                sub = p_host.removesuffix("." + base_domain) if p_host != base_domain else ""
                sub_career = any(s in CAREER_SEGMENTS for s in sub.split("."))
                if (sub_career or _is_career(p.path, text)) and url not in careers:
                    careers.append(url)
                    note("careers_urls", url, final, home_sha, str(a))
                if p_host == host:
                    if _is_news(p.path, text) and url not in news_pages:
                        news_pages.append(url)
                        note("news_pages", url, final, home_sha, str(a))
                    if any(t in (p.path + " " + text).casefold() for t in ("kontakt", "contact", "om-oss", "omoss")) and url not in contact_links and url.rstrip("/") != final.rstrip("/"):
                        contact_links.append(url)
            # finn.no also hosts car/property ads: only its job section counts ("finn" is also Norwegian for "find").
            elif any(d in p_host for d in ATS_DOMAINS) and (p_host.removeprefix("www.") != "finn.no" or "/job" in p.path) \
                    and (_is_career(p.path, text) or re.search(r"\b(stilling|stillinger|karriere|jobb|jobs?|careers?)\b", text, re.I)) and url not in careers:
                careers.append(url)
                note("careers_urls", url, final, home_sha, str(a))
        for link in soup.select('link[rel="alternate"][type*="rss"], link[rel="alternate"][type*="atom"]'):
            feed_url = urllib.parse.urljoin(final, str(link.get("href") or ""))
            if "comment" not in feed_url.casefold():
                feeds.append(feed_url)
        for s in soup.select('script[type="application/ld+json"]'):
            try:
                jtext = s.string or s.get_text() or "{}"
                jdata = json.loads(jtext)
                items = jdata if isinstance(jdata, list) else [jdata]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if "email" not in contact and item.get("email"):
                        cand_em = str(item["email"])[:120]
                        if "@" in cand_em and "@." not in cand_em:
                            contact["email"] = cand_em
                            note("contact_email", contact["email"], final, home_sha, jtext)
                    if "phone" not in contact and item.get("telephone"):
                        phone = _norm_phone(str(item["telephone"]))
                        if phone:
                            contact["phone"] = phone
                            note("contact_phone", phone, final, home_sha, jtext)
            except Exception:
                pass

    if raw and final and soup is not None:
        home_plain_text = soup.get_text(" ", strip=True)
        if "phone" not in contact:
            found_phone = _phone_from_text(home_plain_text) or _phone_from_text(_script_texts(soup))
            if found_phone:
                contact["phone"] = found_phone[0]
                note("contact_phone", found_phone[0], final, home_sha, found_phone[1])
        if "email" not in contact:
            found_email = _email_from_text(home_plain_text, base_domain) or _email_from_text(_script_texts(soup), base_domain)
            if found_email:
                contact["email"] = found_email[0]
                note("contact_email", found_email[0], final, home_sha, found_email[1])

    # Scan already-cached secondary pages from website.value["pages"] (zero extra HTTP requests)
    cached_page_blobs: list[tuple[str, str, str]] = []
    cached_social_candidates: list[tuple[dict[str, str], str, str, str]] = []
    if final:
        host = urllib.parse.urlparse(final).netloc.lower()
        for p_entry in (value.get("pages") or []):
            if not isinstance(p_entry, dict):
                continue
            p_url = str(p_entry.get("url") or "")
            if not p_url or p_url.rstrip("/").casefold() == final.rstrip("/").casefold():
                continue
            cached_p = _get_cached_page(p_url)
            if cached_p is None:
                continue
            p_raw, p_final = cached_p
            if not p_raw:
                continue
            p_src = p_final or p_url
            p_sha = _sha(p_raw)
            p_html = p_raw.decode("utf-8", errors="replace")
            cached_page_blobs.append((p_src, p_sha, p_html))
            p_soup = BeautifulSoup(p_html, "lxml")
            for pa in p_soup.select("a[href]"):
                purl = urllib.parse.urljoin(p_src, str(pa.get("href") or ""))
                pp = urllib.parse.urlparse(purl)
                if pp.scheme == "mailto" and "email" not in contact:
                    cand_em = _clean_contact(pp.path, 120)
                    em_host = cand_em.rsplit("@", 1)[-1].lower() if "@" in cand_em else ""
                    if "@" in cand_em and "@." not in cand_em and base_domain and (em_host == base_domain or em_host.endswith("." + base_domain)):
                        contact["email"] = cand_em
                        note("contact_email", contact["email"], p_src, p_sha, str(pa))
                elif pp.scheme == "tel" and "phone" not in contact:
                    phone = _norm_phone(pp.path)
                    if phone:
                        contact["phone"] = phone
                        note("contact_phone", phone, p_src, p_sha, str(pa))
                if pp.scheme not in {"http", "https"}:
                    continue
                ptext = pa.get_text(" ", strip=True)
                pp_host = pp.netloc.lower()
                is_same_dom = bool(base_domain and (pp_host == base_domain or pp_host.endswith("." + base_domain)))
                if is_same_dom:
                    sub = pp_host.removesuffix("." + base_domain) if pp_host != base_domain else ""
                    sub_career = any(s in CAREER_SEGMENTS for s in sub.split("."))
                    if (sub_career or _is_career(pp.path, ptext)) and purl not in careers:
                        careers.append(purl)
                        note("careers_urls", purl, p_src, p_sha, str(pa))
                    if pp_host == host and _is_news(pp.path, ptext) and purl not in news_pages:
                        news_pages.append(purl)
                        note("news_pages", purl, p_src, p_sha, str(pa))
                elif any(d in pp_host for d in ATS_DOMAINS) and (pp_host.removeprefix("www.") != "finn.no" or "/job" in pp.path) \
                        and (_is_career(pp.path, ptext) or re.search(r"\b(stilling|stillinger|karriere|jobb|jobs?|careers?)\b", ptext, re.I)) and purl not in careers:
                    careers.append(purl)
                    note("careers_urls", purl, p_src, p_sha, str(pa))
                norm = normalize_social_url(purl)
                if norm and not any(cand[0].get("url") == norm["url"] for cand in cached_social_candidates):
                    ass = assess_social_identity(profile, norm, base_domain)
                    if ass.get("publishable") and ass.get("identity_score", 0) >= 0.72 and ass.get("matched_tokens"):
                        cached_social_candidates.append((norm, p_src, p_sha, p_html))
            p_plain = p_soup.get_text(" ", strip=True)
            if "phone" not in contact:
                found_phone = _phone_from_text(p_plain) or _phone_from_text(_script_texts(p_soup))
                if found_phone:
                    contact["phone"] = found_phone[0]
                    note("contact_phone", found_phone[0], p_src, p_sha, found_phone[1])
            if "email" not in contact:
                found_email = _email_from_text(p_plain, base_domain) or _email_from_text(_script_texts(p_soup), base_domain)
                if found_email:
                    contact["email"] = found_email[0]
                    note("contact_email", found_email[0], p_src, p_sha, found_email[1])

    # Prioritize dedicated /kontakt or /contact pages ahead of /om-oss
    contact_links.sort(key=lambda u: 0 if any(k in u.casefold() for k in ("kontakt", "contact")) else 1)

    craw, cfinal, csha = None, None, None
    if contact_links:
        _NET.last_requests = 1
        craw, cfinal = _get(contact_links[0], timeout=5.0)
        metrics["requests"] += getattr(_NET, "last_requests", 1)
        if craw:
            metrics["bytes"] += len(craw)
            csha, csrc = _sha(craw), cfinal or contact_links[0]
            c_html = craw.decode("utf-8", errors="replace")
            csoup = BeautifulSoup(c_html, "lxml")
            for ca in csoup.select("a[href]"):
                curl = urllib.parse.urljoin(csrc, str(ca.get("href") or ""))
                cp = urllib.parse.urlparse(curl)
                if cp.scheme == "mailto" and "email" not in contact:
                    cand_em = _clean_contact(cp.path, 120)
                    if "@" in cand_em and "@." not in cand_em:
                        contact["email"] = cand_em
                        note("contact_email", contact["email"], csrc, csha, str(ca))
                elif cp.scheme == "tel" and "phone" not in contact:
                    phone = _norm_phone(cp.path)
                    if phone:
                        contact["phone"] = phone
                        note("contact_phone", phone, csrc, csha, str(ca))
            c_plain_text = csoup.get_text(" ", strip=True)
            if "phone" not in contact:
                found_phone = _phone_from_text(c_plain_text) or _phone_from_text(_script_texts(csoup))
                if found_phone:
                    contact["phone"] = found_phone[0]
                    note("contact_phone", found_phone[0], csrc, csha, found_phone[1])
            if "email" not in contact:
                found_email = _email_from_text(c_plain_text, base_domain) or _email_from_text(_script_texts(csoup), base_domain)
                if found_email:
                    contact["email"] = found_email[0]
                    note("contact_email", found_email[0], csrc, csha, found_email[1])

    activity = []
    for feed in feeds[:2]:
        _NET.last_requests = 1
        fraw, ffinal = _get(feed)
        metrics["requests"] += getattr(_NET, "last_requests", 1)
        if fraw:
            metrics["bytes"] += len(fraw)
            for item in _parse_feed(fraw, ffinal or feed):
                activity.append({**item, "date_kind": "feed_item_date", "source_feed": ffinal})
                note("dated_activity", item["url"], ffinal or feed, _sha(fraw), f"{item['title']} {item['date']}")
    if final:
        host = urllib.parse.urlparse(final).netloc.lower()
        sm_careers, sm_news, sm_dated, sm_requests, sm_prov = _sitemap_signals(final, host)
        metrics["requests"] += sm_requests
        for u in sm_careers:
            if u not in careers:
                careers.append(u)
                note("careers_urls", u, sm_prov[u]["source_url"], sm_prov[u]["sha256"], sm_prov[u]["span"])
        for u in sm_news:
            if u not in news_pages:
                news_pages.append(u)
                note("news_pages", u, sm_prov[u]["source_url"], sm_prov[u]["sha256"], sm_prov[u]["span"])
        if not activity and raw and b"/wp-json/" in raw:
            activity, wp_src = _wp_posts(final)
            metrics["requests"] += 1
            for item in activity:
                note("dated_activity", item["url"], wp_src["source_url"], wp_src["sha256"], f"{item['title']} {item['date']}")
        # A sitemap <lastmod> is not a publication date: sitemap posts are only candidates for the article step.
        if not activity:
            sections = [u for u in news_pages if (_segments(urllib.parse.urlparse(u).path) or [""])[-1] in NEWS_SEGMENTS]
            candidates = [d["url"] for d in sm_dated] + [u for u in sm_news if u not in sections]
            arts, art_prov, art_requests = _news_articles(sections, list(dict.fromkeys(candidates)), host)
            metrics["requests"] += art_requests
            activity = arts
            for p in art_prov:
                note("dated_activity", p["item"], p["source_url"], p["sha256"], p["span"])
        # Backfill news_pages (up to 3) from verified same-site dated_activity article URLs (zero extra HTTP requests)
        if len(news_pages) < 3 and activity:
            for act_item in activity:
                if act_item.get("date_kind") == "sitemap_lastmod":
                    continue
                act_url = str(act_item.get("url") or "")
                if act_url and _same_site(act_url, host) and act_url not in news_pages:
                    prov_hit = next((it for it in item_sources.get("dated_activity", []) if it.get("item") == act_url), None)
                    if prov_hit:
                        news_pages.append(act_url)
                        note("news_pages", act_url, prov_hit["source_url"], prov_hit["sha256"], prov_hit["span"])
                        if len(news_pages) >= 3:
                            break

    def _extract_jobs_from_soup(j_soup: BeautifulSoup, j_src: str, j_sha: str) -> None:
        for s in j_soup.select('script[type="application/ld+json"]'):
            try:
                jtext = s.string or s.get_text() or "{}"
                jdata = json.loads(jtext)
                items = jdata if isinstance(jdata, list) else [jdata]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if item.get("@type") == "JobPosting" and item.get("title"):
                        title_str = str(item["title"])[:200]
                        if not any(jp.get("title") == title_str for jp in job_postings):
                            job_postings.append({
                                "title": title_str,
                                "date_posted": str(item.get("datePosted") or "")[:20],
                                "employment_type": str(item.get("employmentType") or "")[:50],
                            })
                            note("job_postings", title_str, j_src, j_sha, jtext[:240])
            except Exception:
                pass
        if len(job_postings) >= 6:
            return
        generic_ctas = {"", "les mer", "søk nå", "søk her", "se stilling", "se stillingen", "read more", "apply", "apply now", "søk stilling"}
        for ja in j_soup.select("a[href]"):
            raw_jhref = urllib.parse.urljoin(j_src, str(ja.get("href") or "")).split("#", 1)[0]
            jp_full = urllib.parse.urlparse(raw_jhref)
            jp_host = jp_full.netloc.lower()
            if "finn.no" in jp_host and "/job" in jp_full.path and ("/ad.html" in jp_full.path or "finnkode=" in jp_full.query):
                jtitle = " ".join(ja.get_text(" ", strip=True).split())[:200]
                if 4 <= len(jtitle) <= 140 and jtitle.casefold() not in generic_ctas and "alle stillinger" not in jtitle.casefold():
                    if not any(jp_item.get("title") == jtitle for jp_item in job_postings):
                        job_postings.append({
                            "title": jtitle,
                            "date_posted": "",
                            "employment_type": "",
                            "url": raw_jhref,
                        })
                        note("job_postings", jtitle, j_src, j_sha, str(ja)[:240])
                        if len(job_postings) >= 6:
                            break
                continue
            jhref = raw_jhref.split("?", 1)[0]
            jp = urllib.parse.urlparse(jhref)
            jsegs = _segments(jp.path)
            if len(jsegs) >= 2 and jsegs[0] in {"jobs", "stillinger", "ledige-stillinger", "ledig-stilling", "stilling-ledig", "stillinger-ledige", "vacancies"} and not jsegs[1].isdigit() and jsegs[-1] not in {"register", "apply", "sok"}:
                jtitle = " ".join(ja.get_text(" ", strip=True).split())[:200]
                span_str = str(ja)
                if jtitle.casefold() in generic_ctas:
                    cur = ja.parent
                    for _ in range(4):
                        if cur is None or getattr(cur, "name", None) in {"body", "html", "main", "section"}:
                            break
                        headings = cur.find_all(["h1", "h2", "h3", "h4"])
                        if 1 <= len(headings) <= 2:
                            cand_h = " ".join(headings[0].get_text(" ", strip=True).split())[:200]
                            if 4 <= len(cand_h) <= 140 and cand_h.casefold() not in generic_ctas:
                                jtitle = cand_h
                                span_str = str(headings[0])
                                break
                        cur = cur.parent
                if 4 <= len(jtitle) <= 140 and jtitle.casefold() not in generic_ctas and not any(jp_item.get("title") == jtitle for jp_item in job_postings):
                    job_postings.append({
                        "title": jtitle,
                        "date_posted": "",
                        "employment_type": "",
                        "url": jhref,
                    })
                    note("job_postings", jtitle, j_src, j_sha, span_str[:240])
                    if len(job_postings) >= 6:
                        break

    if raw and final and soup is not None:
        _extract_jobs_from_soup(soup, final, home_sha)

    # Sort careers so path-matched CAREER_SEGMENTS come ahead of anchor-text-only career links
    careers.sort(key=lambda u: 0 if any(s in CAREER_SEGMENTS for s in _segments(urllib.parse.urlparse(u).path)) else 1)

    # If career entry points discovered (including via sitemap), fetch up to 2 career pages to find structured job postings
    if careers and len(job_postings) < 6:
        for career_target in careers[:2]:
            ct_p = urllib.parse.urlparse(career_target)
            ct_host = ct_p.netloc.lower()
            if (base_domain and (ct_host == base_domain or ct_host.endswith("." + base_domain))) or any(d in ct_host for d in ("teamtailor.com", "recman.no", "jobbnorge.no", "webcruiter.no")):
                _NET.last_requests = 1
                ct_raw, ct_final = _get(career_target, timeout=5.0)
                metrics["requests"] += getattr(_NET, "last_requests", 1)
                if ct_raw:
                    metrics["bytes"] += len(ct_raw)
                    ct_sha = _sha(ct_raw)
                    ct_soup = BeautifulSoup(ct_raw.decode("utf-8", errors="replace"), "lxml")
                    _extract_jobs_from_soup(ct_soup, ct_final or career_target, ct_sha)
                if len(job_postings) >= 6:
                    break

    social = []
    cpage_text = craw.decode("utf-8", errors="replace") if craw else ""
    all_social_candidates: list[tuple[dict[str, str], str, str, str]] = []
    for link in (value.get("social_links") or []):
        all_social_candidates.append((link, final, home_sha, page_text))
    for cand_tuple in cached_social_candidates:
        if not any(cand[0].get("url") == cand_tuple[0]["url"] for cand in all_social_candidates):
            all_social_candidates.append(cand_tuple)
    if craw and cfinal:
        csoup = BeautifulSoup(cpage_text, "lxml")
        for ca in csoup.select("a[href]"):
            curl = urllib.parse.urljoin(cfinal, str(ca.get("href") or ""))
            norm = normalize_social_url(curl)
            if norm and not any(cand[0].get("url") == norm["url"] for cand in all_social_candidates):
                ass = assess_social_identity(profile, norm, base_domain)
                if ass.get("publishable") and ass.get("identity_score", 0) >= 0.72 and ass.get("matched_tokens"):
                    all_social_candidates.append((norm, cfinal, csha, cpage_text))

    fallback_pages = [(final, home_sha, page_text), (cfinal, csha, cpage_text)] + cached_page_blobs
    for link, cand_src, cand_sha, cand_text in all_social_candidates:
        url = str(link.get("url") if isinstance(link, dict) else link)
        key = re.sub(r"^https?://(?:www\.)?", "", url, flags=re.I)
        span_hit = _find_social_span(key, cand_text) if (cand_src and key) else None
        if not span_hit and key:
            for fb_src, fb_sha, fb_text in fallback_pages:
                if fb_src and fb_text and fb_text is not cand_text:
                    span_hit = _find_social_span(key, fb_text)
                    if span_hit:
                        cand_src, cand_sha = fb_src, fb_sha
                        break
        if span_hit and not any(s.get("url") == link.get("url") for s in social):
            social.append(link)
            note("social_links", url, cand_src, cand_sha, span_hit)

    # Every own-domain email and every phone on the pages read above (home, contact page, cached secondary pages).
    registry_email = ((profile.get("evidence", {}).get("registry_live") or {}).get("value") or {}).get("email")
    contact_emails: list[str] = []
    contact_phones: list[str] = []
    for c_src, c_sha, c_html in [(final, home_sha, page_text), (cfinal or (contact_links[0] if contact_links else None), csha, cpage_text)] + cached_page_blobs:
        if not (c_src and c_sha and c_html):
            continue
        p_emails, p_phones = page_contacts(c_html, base_domain, registry_email)
        for addr, span in p_emails:
            if addr not in contact_emails and len(contact_emails) < MAX_CONTACTS:
                contact_emails.append(addr)
                note("contact_emails", addr, c_src, c_sha, span)
        for phone, span in p_phones:
            if phone not in contact_phones and len(contact_phones) < MAX_CONTACTS:
                contact_phones.append(phone)
                note("contact_phones", phone, c_src, c_sha, span)
    if contact_emails:
        contact["emails"] = contact_emails
    if contact_phones:
        contact["phones"] = contact_phones

    found = bool(careers or activity or contact or news_pages or job_postings or social)
    return evidence(
        "external_footprint", "available" if found else "not_found", "company_owned_site", final or source,
        value={"careers_urls": careers[:4], "news_pages": news_pages[:3], "dated_activity": activity[:MAX_FEED_ITEMS],
               "contact": contact, "social_links": social,
               "job_postings": job_postings[:6], "item_sources": item_sources},
        note="Extracted only from an identity-verified company-owned website; activity dates come from the site's own feed.",
        content_sha256=home_sha or None,
    ), metrics

