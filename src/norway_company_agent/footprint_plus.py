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
import urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any

from bs4 import BeautifulSoup

from .evidence import evidence, utc_now
from .identity import _tokens, apply_website_identity_gate, assess_social_identity
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
}
BRAVE_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"


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


def _slug_variants(text: str) -> set[str]:
    """Generate Norwegian romanization variants: å -> a / aa, ø -> o / oe, æ -> ae."""
    v1 = text.translate(str.maketrans({"ø": "o", "å": "a", "æ": "ae"}))
    v2 = text.translate(str.maketrans({"ø": "oe", "å": "aa", "æ": "ae"}))
    v3 = text.translate(str.maketrans({"ø": "o", "å": "aa", "æ": "ae"}))
    return {v for v in (v1, v2, v3) if 3 <= len(v) <= 40}


def candidate_hosts(profile: dict[str, Any]) -> list[tuple[str, str]]:
    """(host, method) pairs, best first. Method decides how strict the identity gate must be."""
    out: list[tuple[str, str]] = []
    
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

    for em in emails:
        if "@" in em:
            d = em.rsplit("@", 1)[1].strip().lower()
            if d and d not in FREE_MAIL and "." in d:
                out.append((d, "registry_email_domain"))
                # If subdomain (e.g. mail.firma.no), also add root domain
                parts = d.split(".")
                if len(parts) > 2 and parts[-1] in {"no", "com", "org", "net"}:
                    root_d = ".".join(parts[-2:])
                    if root_d not in FREE_MAIL:
                        out.append((root_d, "registry_email_domain"))

    # 2. Brave Search candidate (if key present in env)
    brave_key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    if brave_key:
        for b_host in _brave_search_candidates(profile, brave_key):
            out.append((b_host, "brave_search_candidate"))

    # 3. Name slug candidates with Norwegian vowel digraphs and generic token stripping
    tokens = _tokens(profile.get("name"))
    if tokens:
        # Full name slug candidates
        joined = "".join(tokens)
        hyphen = "-".join(tokens)
        all_forms = _slug_variants(joined)
        if len(tokens) > 1:
            all_forms.update(_slug_variants(hyphen))
        for slug in sorted(all_forms, key=len):
            out.append((f"{slug}.no", "name_slug_guess"))
            out.append((f"{slug}.com", "name_slug_guess"))

        # Core tokens without generic corporate terms (e.g. 'holding', 'invest')
        core = [t for t in tokens if t not in GENERIC_SLUG_TOKENS]
        if core and len(core) < len(tokens) and len(core) >= 1:
            c_joined = "".join(core)
            c_hyphen = "-".join(core)
            c_forms = _slug_variants(c_joined)
            if len(core) > 1:
                c_forms.update(_slug_variants(c_hyphen))
            for slug in sorted(c_forms, key=len):
                out.append((f"{slug}.no", "name_slug_guess"))
                out.append((f"{slug}.com", "name_slug_guess"))

        # Distinctive single brand tokens (length >= 4 and not in generic words)
        for t in tokens:
            if len(t) >= 4 and t not in GENERIC_SLUG_TOKENS:
                for slug in sorted(_slug_variants(t), key=len):
                    out.append((f"{slug}.no", "brand_token_guess"))
                    out.append((f"{slug}.com", "brand_token_guess"))

    # 4. Subunit name slug candidates (operating brand distinct from legal entity)
    for loc in (profile.get("evidence", {}).get("locations", {}).get("value") or {}).get("locations", []):
        loc_name = loc.get("name")
        if loc_name and loc_name.strip().casefold() != str(profile.get("name") or "").strip().casefold():
            sub_tokens = _tokens(loc_name)
            if sub_tokens:
                sub_core = [t for t in sub_tokens if t not in GENERIC_SLUG_TOKENS]
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
            if method in {"registry_email_domain", "brave_search_candidate"} or host.endswith(".no"):
                no_hosts.append((host, method))
            else:
                other_hosts.append((host, method))
    return (no_hosts + other_hosts)[:16]


def _accept_proof(proof: str | None, assessment: dict[str, Any], method: str) -> bool:
    """Exact org number always proves the entity. Address+postcode alone does not: managers, landlords and
    federations share a registered address, so it is accepted only on a guessed/search domain whose page also
    carries most of the legal name, never on a registry e-mail domain (often the manager's own site)."""
    if not proof:
        return False
    if proof.startswith("exact organisation number"):
        return True
    return method != "registry_email_domain" and assessment.get("score", 0) >= 0.85


def discover_website(profile: dict[str, Any], max_tested_hosts: int = 3, timeout: float = 8.0) -> tuple[dict[str, Any] | None, dict[str, int]]:
    """First candidate host with hard proof of the exact entity, else None.

    ``metrics["transient"]`` counts resolving candidates whose homepage fetch failed with a network error or
    timeout, so the caller can retry only those companies once, more slowly, instead of losing a site to load.
    """
    metrics = {"requests": 0, "bytes": 0, "transient": 0}
    net_errors_before = _transient_count()
    tested = 0
    for host, method in candidate_hosts(profile):
        if not _resolves(host):
            continue
        tested += 1
        record, m = fetch_website(host, timeout=timeout, crawl_secondary=False)
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
        ok = bool(assessment.get("publishable")) if method == "registry_email_domain" else assessment.get("score") == 1.0
        if not ok and assessment.get("score", 0) >= 0.3:
            weak = assessment.get("score", 0) < 0.5
            proof, pm = hard_identity_proof(profile, record, WEAK_CANDIDATE_PROOF_PAGES if weak else MAX_PROOF_PAGES, timeout)
            metrics["requests"] += pm["requests"]
            if (record.get("value") or {}).get("pages"):
                gated = apply_website_identity_gate(profile, record)
                assessment = gated["assessment"] or {}
            ok = _accept_proof(proof, assessment, method)
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
        record["note"] = (f"Discovered via {method}; identity: {'; '.join(assessment.get('reasons', []))}. "
                          "Company-controlled claim layer; not an official registry fact.")
        return record, metrics
    metrics["transient"] += _transient_count() - net_errors_before
    return None, metrics


PROOF_PATHS = ("/kontakt", "/kontakt-oss", "/om-oss", "/om", "/contact", "/about", "/about-us", "/personvern", "/personvernerklaering",
               "/privacy", "/privacy-policy", "/vilkar", "/vilkaar", "/handlebetingelser", "/salgsbetingelser", "/impressum", "/cookies")
PROOF_LINK_TERMS = ("kontakt", "contact", "om-oss", "omoss", "om oss", "about", "personvern", "privacy", "vilk", "terms", "betingelser",
                    "impressum", "cookie", "fakturering", "selskap", "company")
MAX_PROOF_PAGES = 4
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
    targets: dict[str, Any] = {
        "org": re.sub(r"\D", "", str(profile.get("organisation_number") or "")),
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
) -> tuple[str | None, dict[str, int]]:
    """Exact org number, or registered street address + postcode. Phone/leader matches are NOT proof (sister companies share them).

    Looks at the homepage, then contact/about/legal pages linked from it, then a short list of conventional paths.
    The org number must stand alone (not be part of a longer digit string) so a phone number cannot match.
    """
    metrics = {"requests": 0}
    targets = _extract_company_proof_targets(profile)
    org = targets["org"]
    zipc = targets["zipc"]
    base = website.get("source_url") or ""
    parsed = urllib.parse.urlparse(base)
    root = f"{parsed.scheme}://{parsed.netloc}"
    org_re = re.compile(rf"(?<!\d){org}(?!\d)") if len(org) == 9 else None
    seen: set[str] = set()
    queue = [base]
    fetched = 0
    first_html = None
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
        if zipc and any(len(s) >= 6 and s in text.casefold() for s in targets["streets"]) and re.search(rf"(?<!\d){zipc}(?!\d)", text):
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


def _get(url: str, timeout: float = 8.0, max_bytes: int = 800_000) -> tuple[bytes | None, str | None]:
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


NEWS_SEGMENTS = {"nyheter", "news", "aktuelt", "blogg", "blog", "presse", "press", "pressemeldinger", "artikler", "innlegg", "post", "posts"}
NOT_NEWS_SEGMENTS = {"produkt", "produkter", "product", "products", "shop", "butikk", "kategori", "category", "collections", "brands", "merker"}
CAREER_SEGMENTS = {"karriere", "career", "careers", "ledige-stillinger", "stillinger", "rekruttering", "jobs", "job", "jobb"}
CAREER_TEXT_RE = re.compile(r"\b(karriere|careers?|ledige stillinger|rekruttering|jobs?|jobb)\b", re.I)
NEWS_TEXT_RE = re.compile(r"\b(nyheter|news|aktuelt|blogg|blog|presse|press)\b", re.I)
SITEMAP_NEWS_FILE_RE = re.compile(r"(nyhet|news|blogg?|aktuelt|presse|press|artikkel|innlegg|post)", re.I)
PHONE_LABEL_RE = re.compile(r"(?:tlf|tel|telefon|mobil|ring(?:\s+oss)?)\b[\s.:]*((?:\+\s?47|0047)?[\s.]?(?:\d[\s.]?){8})", re.I)
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
    """News section: whole path segment or anchor word, and not a product/shop category called 'nyheter'."""
    segs = _segments(path)
    if any(s in NOT_NEWS_SEGMENTS for s in segs):
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


def _phone_from_text(text: str) -> tuple[str, str] | None:
    """A Norwegian number printed next to a phone label on a verified company page: (number, literal snippet)."""
    for m in PHONE_LABEL_RE.finditer(text):
        phone = _norm_phone(m.group(1))
        if phone:
            return phone, m.group(0).strip()[:160]
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
        if len(items) >= 8:
            break
    return items


ARTICLE_TYPES = {"newsarticle", "article", "blogposting", "reportagenewsarticle", "pressrelease"}
NOT_ARTICLE_SEGMENTS = {"page", "side", "tag", "tags", "kategori", "category", "author", "forfatter", "feed", "arkiv", "archive"}


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
        t = soup.select_one("article time[datetime]")
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
    urls = list(candidates)
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
        if not _same_site(direct, host) or any(i["url"] == direct for i in items):
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
    if raw and final:
        metrics["bytes"] += len(raw)
        soup = BeautifulSoup(raw.decode("utf-8", errors="replace"), "lxml")
        host = urllib.parse.urlparse(final).netloc.lower()
        base_domain = _registered_domain(final)
        for a in soup.select("a[href]"):
            url = urllib.parse.urljoin(final, str(a.get("href") or ""))
            p = urllib.parse.urlparse(url)
            if p.scheme == "mailto" and "email" not in contact:
                contact["email"] = _clean_contact(p.path, 120)
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
                        contact["email"] = str(item["email"])[:120]
                        note("contact_email", contact["email"], final, home_sha, jtext)
                    if "phone" not in contact and item.get("telephone"):
                        phone = _norm_phone(str(item["telephone"]))
                        if phone:
                            contact["phone"] = phone
                            note("contact_phone", phone, final, home_sha, jtext)
                    if item.get("@type") == "JobPosting" and item.get("title"):
                        job_postings.append({
                            "title": str(item["title"])[:200],
                            "date_posted": str(item.get("datePosted") or "")[:20],
                            "employment_type": str(item.get("employmentType") or "")[:50],
                        })
                        note("job_postings", str(item["title"])[:200], final, home_sha, jtext)
            except Exception:
                pass

    if raw and final and "phone" not in contact:
        found_phone = _phone_from_text(BeautifulSoup(raw.decode("utf-8", errors="replace"), "lxml").get_text(" ", strip=True))
        if found_phone:
            contact["phone"] = found_phone[0]
            note("contact_phone", found_phone[0], final, home_sha, found_phone[1])

    craw, cfinal, csha = None, None, None
    if contact_links and (not contact.get("email") or not contact.get("phone") or not value.get("social_links")):
        _NET.last_requests = 1
        craw, cfinal = _get(contact_links[0], timeout=5.0)
        metrics["requests"] += getattr(_NET, "last_requests", 1)
        if craw:
            metrics["bytes"] += len(craw)
            csha, csrc = _sha(craw), cfinal or contact_links[0]
            csoup = BeautifulSoup(craw.decode("utf-8", errors="replace"), "lxml")
            for ca in csoup.select("a[href]"):
                curl = urllib.parse.urljoin(csrc, str(ca.get("href") or ""))
                cp = urllib.parse.urlparse(curl)
                if cp.scheme == "mailto" and "email" not in contact:
                    contact["email"] = _clean_contact(cp.path, 120)
                    note("contact_email", contact["email"], csrc, csha, str(ca))
                elif cp.scheme == "tel" and "phone" not in contact:
                    phone = _norm_phone(cp.path)
                    if phone:
                        contact["phone"] = phone
                        note("contact_phone", phone, csrc, csha, str(ca))
            if "phone" not in contact:
                found_phone = _phone_from_text(csoup.get_text(" ", strip=True))
                if found_phone:
                    contact["phone"] = found_phone[0]
                    note("contact_phone", found_phone[0], csrc, csha, found_phone[1])

    # If career entry points discovered, fetch the primary career page to find structured job postings
    if careers and not job_postings:
        career_target = careers[0]
        ct_p = urllib.parse.urlparse(career_target)
        ct_host = ct_p.netloc.lower()
        base_domain = _registered_domain(final) if final else ""
        if (base_domain and (ct_host == base_domain or ct_host.endswith("." + base_domain))) or any(d in ct_host for d in ("teamtailor.com", "recman.no", "jobbnorge.no", "webcruiter.no")):
            _NET.last_requests = 1
            ct_raw, ct_final = _get(career_target, timeout=5.0)
            metrics["requests"] += getattr(_NET, "last_requests", 1)
            if ct_raw:
                metrics["bytes"] += len(ct_raw)
                ct_sha = _sha(ct_raw)
                ct_soup = BeautifulSoup(ct_raw.decode("utf-8", errors="replace"), "lxml")
                for s in ct_soup.select('script[type="application/ld+json"]'):
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
                                    note("job_postings", title_str, ct_final or career_target, ct_sha, jtext[:240])
                    except Exception:
                        pass

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

    social = []
    page_text = raw.decode("utf-8", errors="replace") if raw else ""
    cpage_text = craw.decode("utf-8", errors="replace") if craw else ""
    all_social_candidates: list[tuple[dict[str, str], str, str, str]] = []
    for link in (value.get("social_links") or []):
        all_social_candidates.append((link, final, home_sha, page_text))
    if craw and cfinal:
        csoup = BeautifulSoup(cpage_text, "lxml")
        for ca in csoup.select("a[href]"):
            curl = urllib.parse.urljoin(cfinal, str(ca.get("href") or ""))
            norm = normalize_social_url(curl)
            if norm and not any(cand[0].get("url") == norm["url"] for cand in all_social_candidates):
                ass = assess_social_identity(profile, norm)
                if ass.get("publishable"):
                    all_social_candidates.append((norm, cfinal, csha, cpage_text))

    for link, cand_src, cand_sha, cand_text in all_social_candidates:
        url = str(link.get("url") if isinstance(link, dict) else link)
        key = re.sub(r"^https?://(?:www\.)?", "", url, flags=re.I)
        hit = re.search(r"[^\"'\s<>]*" + re.escape(key) + r"[^\"'\s<>]*", cand_text, re.I) if (cand_src and key) else None
        if not hit and cand_src and key:
            other_text = cpage_text if cand_text is page_text else page_text
            other_src = cfinal if cand_text is page_text else final
            other_sha = csha if cand_text is page_text else home_sha
            if other_text and other_src:
                hit = re.search(r"[^\"'\s<>]*" + re.escape(key) + r"[^\"'\s<>]*", other_text, re.I)
                if hit:
                    cand_src, cand_sha = other_src, other_sha
        if hit and not any(s.get("url") == link.get("url") for s in social):
            social.append(link)
            note("social_links", url, cand_src, cand_sha, hit.group(0))

    found = bool(careers or activity or contact or news_pages or job_postings)
    return evidence(
        "external_footprint", "available" if found else "not_found", "company_owned_site", final or source,
        value={"careers_urls": careers[:4], "news_pages": news_pages[:3], "dated_activity": activity[:8],
               "contact": contact, "social_links": social,
               "job_postings": job_postings[:6], "item_sources": item_sources},
        note="Extracted only from an identity-verified company-owned website; activity dates come from the site's own feed.",
        content_sha256=home_sha or None,
    ), metrics

