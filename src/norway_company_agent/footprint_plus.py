"""Free website discovery and company-owned footprint extraction with strict exact-entity gating.

Only candidates the registry itself points to (its e-mail domain) or that print the exact organisation
number are published. Everything else stays `not_found`. Uses the starter's SSRF-safe, robots-aware fetcher.
"""
from __future__ import annotations

import json
import os
import re
import socket
import urllib.parse
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from typing import Any

from bs4 import BeautifulSoup

from .evidence import evidence, utc_now
from .identity import _tokens, apply_website_identity_gate
from .website import SAFE_OPENER, USER_AGENT, _robots_allowed, assert_public_url, fetch_website

FREE_MAIL = {"gmail.com", "hotmail.com", "outlook.com", "live.com", "yahoo.com", "icloud.com", "online.no",
             "msn.com", "me.com", "hotmail.no", "outlook.no", "live.no", "yahoo.no", "getmail.no", "start.no",
             "broadpark.no", "frisurf.no", "c2i.net", "lyse.net", "powertech.no", "tele2.no", "altibox.no",
             "proton.me", "protonmail.com", "bbhosting.no"}
CAREER_TERMS = ("karriere", "career", "ledige-stillinger", "ledige stillinger", "jobs", "jobb", "rekruttering", "stilling")
NEWS_TERMS = ("nyheter", "news", "aktuelt", "blogg", "blog", "presse", "press")


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


def discover_website(profile: dict[str, Any], max_tested_hosts: int = 3) -> tuple[dict[str, Any] | None, dict[str, int]]:
    metrics = {"requests": 0, "bytes": 0}
    tested = 0
    for host, method in candidate_hosts(profile):
        if not _resolves(host):
            continue
        tested += 1
        record, m = fetch_website(host, timeout=5.0)
        metrics["requests"] += m["requests"]
        metrics["bytes"] += m["bytes"]
        if record.get("status") != "available":
            if tested >= max_tested_hosts:
                break
            continue
        gated = apply_website_identity_gate(profile, record)
        assessment = gated["assessment"] or {}
        ok = bool(assessment.get("publishable")) if method == "registry_email_domain" else assessment.get("score") == 1.0
        if not ok and assessment.get("score", 0) >= 0.3:
            weak = assessment.get("score", 0) < 0.5
            proof, pm = hard_identity_proof(profile, record, WEAK_CANDIDATE_PROOF_PAGES if weak else MAX_PROOF_PAGES)
            metrics["requests"] += pm["requests"]
            ok = _accept_proof(proof, assessment, method)
            if ok:
                exact = proof.startswith("exact organisation number")
                assessment["publishable"] = True
                assessment["score"] = 1.0 if exact else 0.9
                assessment["status"] = "exact" if exact else "review"
                assessment["reasons"] = list(assessment.get("reasons", [])) + [proof]
                record.setdefault("value", {})["identity_assessment"] = assessment
        if not ok:
            if tested >= max_tested_hosts:
                break
            continue
        record["source_type"] = record["source_class"] = "discovered_company_website"
        record["note"] = (f"Discovered via {method}; identity: {'; '.join(assessment.get('reasons', []))}. "
                          "Company-controlled claim layer; not an official registry fact.")
        return record, metrics
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
    for a in BeautifulSoup(html, "lxml").find_all("a", href=True):
        href = urllib.parse.urljoin(root + "/", a["href"].strip()).split("#")[0]
        parsed = urllib.parse.urlparse(href)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower().removeprefix("www.") != host:
            continue
        label = (parsed.path + " " + a.get_text(" ", strip=True)).casefold()
        if any(term in label for term in PROOF_LINK_TERMS) and href not in found:
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


def hard_identity_proof(profile: dict[str, Any], website: dict[str, Any], max_pages: int = MAX_PROOF_PAGES) -> tuple[str | None, dict[str, int]]:
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
    while queue and fetched < max_pages:
        url = queue.pop(0)
        if url in seen:
            continue
        seen.add(url)
        body, _ = _get(url)
        if fetched:
            metrics["requests"] += 1
        fetched += 1
        if not body:
            if first_html is None and url == base:
                queue.extend(root + path for path in PROOF_PATHS)
            continue
        html = body.decode("utf-8", errors="replace")
        text = BeautifulSoup(html, "lxml").get_text(" ", strip=True)
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


def _get(url: str, timeout: float = 6.0, max_bytes: int = 800_000) -> tuple[bytes | None, str | None]:
    try:
        assert_public_url(url)
        if not _robots_allowed(url, timeout):
            return None, None
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with SAFE_OPENER.open(req, timeout=timeout) as r:
            final = r.geturl()
            assert_public_url(final)
            return r.read(max_bytes), final
    except Exception:
        return None, None


import urllib.request  # noqa: E402  (kept after helpers to keep imports grouped above minimal)


SITEMAP_NEWS_RE = re.compile(r"(nyhet|news|blogg?|aktuelt|presse|press|artikkel|innlegg|post)", re.I)
SITEMAP_CAREER_RE = re.compile(r"(karriere|career|ledige-stillinger|stillinger|jobb|jobs)", re.I)
PHONE_LABEL_RE = re.compile(r"(?:tlf|tel|telefon|mobil|ring(?:\s+oss)?)\b[\s.:]*((?:\+\s?47|0047)?[\s.]?(?:\d[\s.]?){8})", re.I)


def _clean_contact(value: str, limit: int) -> str:
    return re.sub(r"\s+", "", urllib.parse.unquote(value)).strip()[:limit]


def _phone_from_text(text: str) -> str | None:
    """A Norwegian number printed next to a phone label on a verified company page."""
    for m in PHONE_LABEL_RE.finditer(text):
        digits = re.sub(r"\D", "", m.group(1))
        if digits.startswith("0047"):
            digits = digits[4:]
        elif digits.startswith("47") and len(digits) == 10:
            digits = digits[2:]
        if len(digits) == 8 and digits[0] in "23456789":
            return "+47" + digits
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


def _sitemap_signals(final: str, host: str) -> tuple[list[str], list[str], list[dict[str, str]], int]:
    """Careers/news entry points and dated posts from the verified site's own sitemap (<= 4 fetches)."""
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(final))
    requests = 1
    robots, _ = _get(origin + "/robots.txt", timeout=5.0)
    starts = [m for m in re.findall(r"(?im)^sitemap:\s*(\S+)", (robots or b"").decode("utf-8", "replace")) if _same_site(m, host)]
    queue = starts[:2] or [origin + "/sitemap.xml"]
    careers: list[str] = []
    news: list[str] = []
    dated: list[dict[str, str]] = []
    fetched = 0
    while queue and fetched < 3:
        sm_url = queue.pop(0)
        raw, sm_final = _get(sm_url, timeout=5.0)
        fetched += 1
        requests += 1
        if not raw:
            continue
        kind, rows = _sitemap_entries(raw)
        if kind == "sitemapindex":
            wanted = [loc for loc, _ in rows if _same_site(loc, host) and SITEMAP_NEWS_RE.search(loc)]
            queue.extend(wanted[:2])
            continue
        for loc, mod in rows:
            if not _same_site(loc, host):
                continue
            path = urllib.parse.urlparse(loc).path
            if SITEMAP_CAREER_RE.search(path) and loc not in careers:
                careers.append(loc)
            if SITEMAP_NEWS_RE.search(path) and path.strip("/") and loc not in news:
                news.append(loc)
                slug = path.rstrip("/").rsplit("/", 1)[-1]
                if mod and re.match(r"\d{4}-\d{2}-\d{2}", mod) and slug and not slug.isdigit():
                    dated.append({"title": slug.replace("-", " ").replace("_", " ")[:200], "url": loc, "date": mod,
                                  "date_kind": "sitemap_lastmod", "title_source": "url_slug"})
    dated.sort(key=lambda d: d["date"], reverse=True)
    return careers[:4], news[:3], dated[:5], requests


def _wp_posts(final: str) -> list[dict[str, str]]:
    origin = "{0.scheme}://{0.netloc}".format(urllib.parse.urlparse(final))
    raw, _ = _get(origin + "/wp-json/wp/v2/posts?per_page=5&_fields=date,link,title", timeout=5.0)
    try:
        data = json.loads(raw or b"[]")
    except ValueError:
        return []
    items = []
    for post in data if isinstance(data, list) else []:
        title = ((post.get("title") or {}).get("rendered") or "").strip()
        if title and post.get("link") and post.get("date"):
            items.append({"title": " ".join(BeautifulSoup(title, "lxml").get_text().split())[:200],
                          "url": str(post["link"]), "date": str(post["date"])[:10], "date_kind": "wp_rest_post_date"})
    return items


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


def extract_footprint(profile: dict[str, Any], website: dict[str, Any]) -> tuple[dict[str, Any], dict[str, int]]:
    """Dated activity, careers entry points, contact and company-owned profiles from a VERIFIED website only."""
    metrics = {"requests": 0, "bytes": 0}
    source = website.get("source_url") or ""
    value = website.get("value") or {}
    if website.get("status") != "available" or (value.get("identity_assessment") or {}).get("publishable") is not True:
        return evidence("external_footprint", "not_found", "company_owned_site", "https://data.brreg.no",
                        note="No identity-verified company website; nothing published."), metrics
    raw, final = _get(source)
    metrics["requests"] += 2
    careers, news_pages, feeds, contact, job_postings = [], [], [], {}, []
    contact_links = []
    if raw and final:
        metrics["bytes"] += len(raw)
        soup = BeautifulSoup(raw.decode("utf-8", errors="replace"), "lxml")
        host = urllib.parse.urlparse(final).netloc.lower()
        for a in soup.select("a[href]"):
            url = urllib.parse.urljoin(final, str(a.get("href") or ""))
            p = urllib.parse.urlparse(url)
            if p.scheme == "mailto" and "email" not in contact:
                contact["email"] = _clean_contact(p.path, 120)
            elif p.scheme == "tel" and "phone" not in contact:
                contact["phone"] = _clean_contact(p.path, 40)
            if p.scheme not in {"http", "https"}:
                continue
            hay = (p.path + " " + a.get_text(" ", strip=True)).casefold()
            if p.netloc.lower() == host:
                if any(t in hay for t in CAREER_TERMS) and url not in careers:
                    careers.append(url)
                if any(t in hay for t in NEWS_TERMS) and url not in news_pages:
                    news_pages.append(url)
                if any(t in hay for t in ("kontakt", "contact", "om-oss", "omoss")) and url not in contact_links and url.rstrip("/") != final.rstrip("/"):
                    contact_links.append(url)
            elif any(d in p.netloc.lower() for d in ("finn.no", "webcruiter", "easycruit", "teamtailor", "recman", "varbi", "lever.co", "greenhouse.io")) \
                    and any(t in hay for t in CAREER_TERMS + ("stilling", "finn")) and url not in careers:
                careers.append(url)
        for link in soup.select('link[rel="alternate"][type*="rss"], link[rel="alternate"][type*="atom"]'):
            feeds.append(urllib.parse.urljoin(final, str(link.get("href") or "")))
        for s in soup.select('script[type="application/ld+json"]'):
            try:
                jdata = json.loads(s.string or s.get_text() or "{}")
                items = jdata if isinstance(jdata, list) else [jdata]
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if "email" not in contact and item.get("email"):
                        contact["email"] = str(item["email"])[:120]
                    if "phone" not in contact and item.get("telephone"):
                        contact["phone"] = str(item["telephone"])[:40]
                    if item.get("@type") == "JobPosting" and item.get("title"):
                        job_postings.append({
                            "title": str(item["title"])[:200],
                            "date_posted": str(item.get("datePosted") or "")[:20],
                            "employment_type": str(item.get("employmentType") or "")[:50],
                        })
            except Exception:
                pass

    if raw and final and "phone" not in contact:
        text_phone = _phone_from_text(BeautifulSoup(raw.decode("utf-8", errors="replace"), "lxml").get_text(" ", strip=True))
        if text_phone:
            contact["phone"] = text_phone

    if contact_links and (not contact.get("email") or not contact.get("phone")):
        craw, cfinal = _get(contact_links[0], timeout=5.0)
        metrics["requests"] += 1
        if craw:
            csoup = BeautifulSoup(craw.decode("utf-8", errors="replace"), "lxml")
            for ca in csoup.select("a[href]"):
                curl = urllib.parse.urljoin(cfinal or contact_links[0], str(ca.get("href") or ""))
                cp = urllib.parse.urlparse(curl)
                if cp.scheme == "mailto" and "email" not in contact:
                    contact["email"] = _clean_contact(cp.path, 120)
                elif cp.scheme == "tel" and "phone" not in contact:
                    contact["phone"] = _clean_contact(cp.path, 40)
            if "phone" not in contact:
                text_phone = _phone_from_text(csoup.get_text(" ", strip=True))
                if text_phone:
                    contact["phone"] = text_phone

    activity = []
    for feed in feeds[:2]:
        fraw, ffinal = _get(feed)
        metrics["requests"] += 2
        if fraw:
            metrics["bytes"] += len(fraw)
            activity += [{**item, "source_feed": ffinal} for item in _parse_feed(fraw, ffinal or feed)]
    if final:
        host = urllib.parse.urlparse(final).netloc.lower()
        sm_careers, sm_news, sm_dated, sm_requests = _sitemap_signals(final, host)
        metrics["requests"] += sm_requests
        careers += [u for u in sm_careers if u not in careers]
        news_pages += [u for u in sm_news if u not in news_pages]
        if not activity and raw and b"/wp-json/" in raw:
            activity = _wp_posts(final)
            metrics["requests"] += 1
        if not activity:
            activity = sm_dated
    found = bool(careers or activity or contact or news_pages or job_postings)
    return evidence(
        "external_footprint", "available" if found else "not_found", "company_owned_site", final or source,
        value={"careers_urls": careers[:4], "news_pages": news_pages[:3], "dated_activity": activity[:8],
               "contact": contact, "social_links": (value.get("social_links") or []),
               "job_postings": job_postings[:6]},
        note="Extracted only from an identity-verified company-owned website; activity dates come from the site's own feed.",
    ), metrics
