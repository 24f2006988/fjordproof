"""Free website discovery and company-owned footprint extraction with strict exact-entity gating.

Only candidates the registry itself points to (its e-mail domain) or that print the exact organisation
number are published. Everything else stays `not_found`. Uses the starter's SSRF-safe, robots-aware fetcher.
"""
from __future__ import annotations

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


def _resolves(host: str) -> bool:
    try:
        socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
        return True
    except OSError:
        return False


def candidate_hosts(profile: dict[str, Any]) -> list[tuple[str, str]]:
    """(host, method) pairs, best first. Method decides how strict the identity gate must be."""
    out: list[tuple[str, str]] = []
    raw = (profile.get("evidence", {}).get("registry", {}).get("value") or {})
    email = str(raw.get("epostadresse") or "")
    if "@" in email:
        domain = email.rsplit("@", 1)[1].strip().lower()
        if domain and domain not in FREE_MAIL and "." in domain:
            out.append((domain, "registry_email_domain"))
    tokens = _tokens(profile.get("name"))
    if tokens:
        slugs = {"".join(tokens), "-".join(tokens)}
        for slug in sorted(slugs, key=len):
            if 4 <= len(slug) <= 40:
                for tld in ("no", "com"):
                    out.append((f"{slug}.{tld}", "name_slug_guess"))
    seen, unique = set(), []
    for host, method in out:
        if host not in seen:
            seen.add(host)
            unique.append((host, method))
    return unique[:5]


def discover_website(profile: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, int]]:
    metrics = {"requests": 0, "bytes": 0}
    for host, method in candidate_hosts(profile):
        if not _resolves(host):
            continue
        record, m = fetch_website(host)
        metrics["requests"] += m["requests"]
        metrics["bytes"] += m["bytes"]
        if record.get("status") != "available":
            continue
        gated = apply_website_identity_gate(profile, record)
        assessment = gated["assessment"] or {}
        org_proof = assessment.get("score") == 1.0
        if method == "registry_email_domain":
            ok = assessment.get("publishable")
        else:  # guessed domain: demand hard proof of the exact legal entity
            ok = org_proof
            if not ok and assessment.get("publishable"):
                proof, pm = hard_identity_proof(profile, record)
                metrics["requests"] += pm["requests"]
                ok = proof is not None
                if ok:
                    assessment["reasons"] = list(assessment.get("reasons", [])) + [proof]
        if not ok:
            continue
        record["source_type"] = record["source_class"] = "discovered_company_website"
        record["note"] = (f"Discovered via {method}; identity: {'; '.join(assessment.get('reasons', []))}. "
                          "Company-controlled claim layer; not an official registry fact.")
        return record, metrics
    return None, metrics


PROOF_PATHS = ("/kontakt", "/kontakt-oss", "/om-oss", "/contact", "/about", "/personvern", "/privacy", "/vilkar", "/handlebetingelser")


def hard_identity_proof(profile: dict[str, Any], website: dict[str, Any]) -> tuple[str | None, dict[str, int]]:
    """Exact org number, or registered street address + postcode, printed on the company's own pages."""
    metrics = {"requests": 0}
    org = re.sub(r"\D", "", str(profile.get("organisation_number") or ""))
    raw = (profile.get("evidence", {}).get("registry", {}).get("value") or {})
    street = str(raw.get("forretningsadresse.adresse") or "").strip().casefold()
    zipc = str(raw.get("forretningsadresse.postnummer") or "").strip()
    base = website.get("source_url") or ""
    parsed = urllib.parse.urlparse(base)
    root = f"{parsed.scheme}://{parsed.netloc}"
    pages = [base] + [root + path for path in PROOF_PATHS]
    for i, url in enumerate(pages):
        body, _ = _get(url)
        metrics["requests"] += 1 if i else 0
        if not body:
            continue
        text = BeautifulSoup(body.decode("utf-8", errors="replace"), "lxml").get_text(" ", strip=True)
        if org and org in re.sub(r"(?<=\d)[ .](?=\d)", "", text):
            return f"exact organisation number printed on {url}", metrics
        if street and len(street) >= 6 and zipc and street in text.casefold() and zipc in text:
            return f"registered street address and postcode printed on {url}", metrics
    return None, metrics


def _get(url: str, timeout: float = 12.0, max_bytes: int = 800_000) -> tuple[bytes | None, str | None]:
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
        return evidence("external_footprint", "not_found", "company_owned_site", source or "https://data.brreg.no",
                        note="No identity-verified company website; nothing published."), metrics
    raw, final = _get(source)
    metrics["requests"] += 2
    careers, news_pages, feeds, contact = [], [], [], {}
    if raw and final:
        metrics["bytes"] += len(raw)
        soup = BeautifulSoup(raw.decode("utf-8", errors="replace"), "lxml")
        host = urllib.parse.urlparse(final).netloc.lower()
        for a in soup.select("a[href]"):
            url = urllib.parse.urljoin(final, str(a.get("href") or ""))
            p = urllib.parse.urlparse(url)
            if p.scheme == "mailto" and "email" not in contact:
                contact["email"] = p.path[:120]
            elif p.scheme == "tel" and "phone" not in contact:
                contact["phone"] = p.path[:40]
            if p.scheme not in {"http", "https"}:
                continue
            hay = (p.path + " " + a.get_text(" ", strip=True)).casefold()
            if p.netloc.lower() == host:
                if any(t in hay for t in CAREER_TERMS) and url not in careers:
                    careers.append(url)
                if any(t in hay for t in NEWS_TERMS) and url not in news_pages:
                    news_pages.append(url)
            elif any(d in p.netloc.lower() for d in ("finn.no", "webcruiter", "easycruit", "teamtailor", "recman", "varbi", "lever.co", "greenhouse.io")) \
                    and any(t in hay for t in CAREER_TERMS + ("stilling", "finn")) and url not in careers:
                careers.append(url)
        for link in soup.select('link[rel="alternate"][type*="rss"], link[rel="alternate"][type*="atom"]'):
            feeds.append(urllib.parse.urljoin(final, str(link.get("href") or "")))
    activity = []
    for feed in feeds[:2]:
        fraw, ffinal = _get(feed)
        metrics["requests"] += 2
        if fraw:
            metrics["bytes"] += len(fraw)
            activity += [{**item, "source_feed": ffinal} for item in _parse_feed(fraw, ffinal or feed)]
    found = bool(careers or activity or contact or news_pages)
    return evidence(
        "external_footprint", "available" if found else "not_found", "company_owned_site", final or source,
        value={"careers_urls": careers[:4], "news_pages": news_pages[:3], "dated_activity": activity[:8],
               "contact": contact, "social_links": (value.get("social_links") or [])},
        note="Extracted only from an identity-verified company-owned website; activity dates come from the site's own feed.",
    ), metrics
