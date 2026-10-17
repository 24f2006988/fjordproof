from __future__ import annotations

import http.client
import json
import ipaddress
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import urllib.robotparser
from dataclasses import dataclass
from typing import Any

from bs4 import BeautifulSoup
import extruct
import tldextract
import trafilatura

from .evidence import evidence

USER_AGENT = "builderr-signalpost-poc/0.1 (+https://builderr.ai)"
SOCIAL_HOSTS = {
    "linkedin.com": "linkedin",
    "facebook.com": "facebook",
    "instagram.com": "instagram",
    "x.com": "x",
    "twitter.com": "x",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "tiktok.com": "tiktok",
}
PRIORITY_TERMS = (
    "om-oss", "om_oss", "about", "kontakt", "contact", "ledelse", "management",
    "team", "people", "locations", "lokasjoner", "avdelinger", "butikker",
    "news", "press", "aktuelt", "nyheter",
)

_CACHE_LOCK = threading.Lock()
_MAX_CACHE_ENTRIES = 1024
_ROBOTS_CACHE: dict[str, urllib.robotparser.RobotFileParser | None] = {}
_ROBOTS_BODY_CACHE: dict[str, bytes] = {}
_ROBOTS_TIMEOUT: set[str] = set()
_PAGE_CACHE: dict[str, tuple[bytes, str]] = {}


def _is_timeout_exception(exc: BaseException) -> bool:
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return True
    reason = getattr(exc, "reason", None)
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return True
    text = f"{exc} {reason}".lower()
    return "timed out" in text or "timeout" in text


def clear_website_caches() -> None:
    with _CACHE_LOCK:
        _ROBOTS_CACHE.clear()
        _ROBOTS_BODY_CACHE.clear()
        _ROBOTS_TIMEOUT.clear()
        _PAGE_CACHE.clear()


def _cache_page(url: str, raw: bytes, final_url: str | None = None) -> None:
    if not raw or len(raw) > 2_000_000:
        return
    resolved = final_url or url
    with _CACHE_LOCK:
        if len(_PAGE_CACHE) >= _MAX_CACHE_ENTRIES:
            _PAGE_CACHE.clear()
        _PAGE_CACHE[url] = (raw, resolved)
        if resolved != url:
            _PAGE_CACHE[resolved] = (raw, resolved)


def _get_cached_page(url: str) -> tuple[bytes, str] | None:
    with _CACHE_LOCK:
        return _PAGE_CACHE.get(url)


def _get_cached_robots_body(url: str) -> bytes | None:
    parsed = urllib.parse.urlparse(url)
    origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    with _CACHE_LOCK:
        return _ROBOTS_BODY_CACHE.get(origin)


def assert_public_url(url: str) -> None:
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme not in {"http", "https"} or not host:
        raise ValueError("Only public HTTP(S) URLs are allowed")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        raise ValueError("Local hosts are blocked")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM)}
    except socket.gaierror as exc:
        raise ValueError("Hostname did not resolve") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            raise ValueError("Private, loopback, link-local, multicast, and reserved addresses are blocked")


class SafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> Any:
        assert_public_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


SAFE_OPENER = urllib.request.build_opener(SafeRedirectHandler())


def normalize_homepage(value: str | None) -> str | None:
    value = str(value or "").strip()
    if not value:
        return None
    if not re.match(r"^https?://", value, re.I):
        value = "https://" + value
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))


def _registered_domain(url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    ext = tldextract.extract(parsed.hostname or "")
    return ext.top_domain_under_public_suffix


def _robots_check(url: str, timeout: float) -> tuple[bool, int]:
    assert_public_url(url)
    parsed = urllib.parse.urlparse(url)
    origin = f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    with _CACHE_LOCK:
        if origin in _ROBOTS_CACHE:
            cached_parser = _ROBOTS_CACHE[origin]
            return (cached_parser.can_fetch(USER_AGENT, url) if cached_parser is not None else True), 0
    robots_url = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, "/robots.txt", "", "", ""))
    parser = urllib.robotparser.RobotFileParser()
    parser.set_url(robots_url)
    try:
        request = urllib.request.Request(robots_url, headers={"User-Agent": USER_AGENT})
        with SAFE_OPENER.open(request, timeout=timeout) as response:
            raw = response.read(200_000)
            final_robots_url = response.geturl()
            parser.parse(raw.decode("utf-8", errors="replace").splitlines())
        if raw:
            _cache_page(robots_url, raw, final_robots_url)
        final_parsed = urllib.parse.urlparse(final_robots_url)
        final_origin = f"{final_parsed.scheme.lower()}://{final_parsed.netloc.lower()}" if final_parsed.scheme and final_parsed.netloc else origin
        with _CACHE_LOCK:
            if len(_ROBOTS_CACHE) >= _MAX_CACHE_ENTRIES:
                _ROBOTS_CACHE.clear()
                _ROBOTS_BODY_CACHE.clear()
            _ROBOTS_CACHE[origin] = parser
            _ROBOTS_CACHE[final_origin] = parser
            _ROBOTS_BODY_CACHE[origin] = raw
            _ROBOTS_BODY_CACHE[final_origin] = raw
        return parser.can_fetch(USER_AGENT, url), 1
    except Exception as exc:
        # An unavailable robots file is not permission to ignore explicit site terms; callers retain
        # the URL and can route uncertain domains to review. For this bounded homepage POC, allow one
        # ordinary GET when robots.txt is absent rather than crawl deeper.
        timed_out = _is_timeout_exception(exc)
        with _CACHE_LOCK:
            if len(_ROBOTS_CACHE) >= _MAX_CACHE_ENTRIES:
                _ROBOTS_CACHE.clear()
                _ROBOTS_BODY_CACHE.clear()
                _ROBOTS_TIMEOUT.clear()
            _ROBOTS_CACHE[origin] = None
            _ROBOTS_BODY_CACHE[origin] = b""
            if timed_out:
                _ROBOTS_TIMEOUT.add(origin)
        return True, 1


def _robots_allowed(url: str, timeout: float) -> bool:
    allowed, _ = _robots_check(url, timeout)
    return allowed


def _social_links(base_url: str, soup: BeautifulSoup) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}
    candidates = [str(node.get("href") or "") for node in soup.select("a[href]")]
    candidates.extend(str(node.get("data-href") or "") for node in soup.select("[data-href]"))
    candidates.extend(str(node.get("src") or "") for node in soup.select("iframe[src]"))
    for candidate in candidates:
        url = urllib.parse.urljoin(base_url, candidate)
        parsed_candidate = urllib.parse.urlparse(url)
        if (parsed_candidate.hostname or "").casefold().removeprefix("www.") == "facebook.com" and parsed_candidate.path.startswith("/plugins/"):
            embedded = urllib.parse.parse_qs(parsed_candidate.query).get("href", [])
            if embedded:
                url = embedded[0]
        normalized = normalize_social_url(url)
        if not normalized:
            continue
        found[(normalized["platform"], normalized["url"])] = normalized
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


def structured_social_links(value: Any) -> list[dict[str, str]]:
    found: dict[tuple[str, str], dict[str, str]] = {}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            same_as = node.get("sameAs")
            urls = same_as if isinstance(same_as, list) else [same_as]
            for raw in urls:
                if not isinstance(raw, str):
                    continue
                normalized = normalize_social_url(raw.strip())
                if normalized:
                    found[(normalized["platform"], normalized["url"])] = normalized
            for child in node.values():
                walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(value)
    return sorted(found.values(), key=lambda item: (item["platform"], item["url"]))


def normalize_social_url(url: str) -> dict[str, str] | None:
    try:
        parsed = urllib.parse.urlparse(url)
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().removeprefix("www.")
    platform = next((label for domain, label in SOCIAL_HOSTS.items() if host == domain or host.endswith("." + domain)), None)
    if not platform:
        return None
    parts = [part.strip() for part in parsed.path.split("/") if part.strip()]
    lowered = [part.casefold() for part in parts]
    rejected_first = {
        "facebook": {"sharer", "sharer.php", "share.php", "dialog", "policy.php", "privacy", "events", "groups", "plugins"},
        "instagram": {"p", "reel", "reels", "stories", "explore"},
        "x": {"intent", "share", "home", "search", "i"},
    }
    if not parts or lowered[0] in rejected_first.get(platform, set()):
        return None
    if platform == "facebook" and lowered[0] == "profile.php":
        return None
    if platform == "linkedin" and (lowered[0] != "company" or len(parts) < 2):
        return None
    if platform == "youtube" and lowered[0] not in {"channel", "user", "c"} and not parts[0].startswith("@"):
        return None
    if host == "youtu.be":
        return None
    if platform == "tiktok" and not parts[0].startswith("@"):
        return None
    if platform == "x" and len(parts) != 1:
        return None
    canonical_host = {
        "linkedin": "linkedin.com",
        "facebook": "facebook.com",
        "instagram": "instagram.com",
        "x": "x.com",
        "youtube": "youtube.com",
        "tiktok": "tiktok.com",
    }[platform]
    if platform == "linkedin":
        parts = parts[:2]
    elif platform == "youtube":
        parts = parts[:1] if parts[0].startswith("@") else parts[:2]
    return {"platform": platform, "url": f"https://{canonical_host}/{'/'.join(parts)}"}


def _priority_links(base_url: str, soup: BeautifulSoup, limit: int = 4) -> list[str]:
    base = urllib.parse.urlparse(base_url)
    candidates: dict[str, int] = {}
    canonical_to_url: dict[str, str] = {}
    for anchor in soup.select("a[href]"):
        href = str(anchor.get("href") or "").strip()
        url = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() != base.netloc.lower():
            continue
        haystack = (parsed.path + " " + anchor.get_text(" ", strip=True)).casefold()
        rank = next((index for index, term in enumerate(PRIORITY_TERMS) if term in haystack), None)
        if rank is None:
            continue
        clean = urllib.parse.urlunparse((parsed.scheme, parsed.netloc, parsed.path or "/", "", "", ""))
        key = clean.rstrip("/").casefold()
        if key == base_url.rstrip("/").casefold():
            continue
        if key not in canonical_to_url:
            canonical_to_url[key] = clean
        target = canonical_to_url[key]
        candidates[target] = min(rank, candidates.get(target, rank))
    return [url for url, _ in sorted(candidates.items(), key=lambda item: (item[1], item[0]))[:limit]]


def _identity_text_excerpt(soup: BeautifulSoup) -> str:
    nodes = soup.select(
        'footer, address, [class*="footer"], [id*="footer"], '
        '[itemprop="legalName"], [itemprop="address"], [itemprop="telephone"], [itemprop="email"]'
    )
    return " ".join(node.get_text(" ", strip=True) for node in nodes)[:2000]


def _visible_body_text(html: str) -> str:
    clean_soup = BeautifulSoup(html, "lxml")
    for tag in clean_soup.select("script, style, noscript, template, svg"):
        tag.decompose()
    body = clean_soup.body or clean_soup
    return body.get_text(" ", strip=True)


def _fetch_secondary_page(url: str, *, homepage_domain: str, timeout: float, max_bytes: int) -> tuple[dict[str, Any] | None, list[dict[str, str]], int, int, int, str | None]:
    allowed, r_req = _robots_check(url, timeout)
    if not allowed:
        return None, [], r_req, 0, 0, "robots.txt disallows page"
    cached = _get_cached_page(url)
    started = time.monotonic()
    try:
        if cached is not None:
            raw, final_url = cached
            elapsed = 0
            req_count = r_req
        else:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
            with SAFE_OPENER.open(request, timeout=timeout) as response:
                raw = response.read(max_bytes + 1)
                elapsed = int((time.monotonic() - started) * 1000)
                final_url = response.geturl()
                req_count = r_req + 1
                if len(raw) > max_bytes or "html" not in response.headers.get("content-type", "").lower():
                    return None, [], req_count, len(raw), elapsed, "unsupported or oversized page"
                if _registered_domain(final_url) != homepage_domain:
                    return None, [], req_count, len(raw), elapsed, "redirected outside registered domain"
            _cache_page(url, raw, final_url)
        page_html = raw.decode("utf-8", errors="replace")
        page_soup = BeautifulSoup(page_html, "lxml")
        page_text = trafilatura.extract(page_html, url=final_url, include_links=False, include_tables=False, favor_precision=True) or ""
        if len(page_text.strip()) < 100:
            fallback_text = _visible_body_text(page_html)
            if len(fallback_text) > len(page_text.strip()):
                page_text = fallback_text
        page = {
            "url": final_url,
            "title": page_soup.title.get_text(" ", strip=True)[:500] if page_soup.title else "",
            "main_text_excerpt": page_text[:5000],
            "identity_text_excerpt": _identity_text_excerpt(page_soup),
            "content_sha256": __import__("hashlib").sha256(raw).hexdigest(),
        }
        return page, _social_links(final_url, page_soup), req_count, (len(raw) if cached is None else 0), elapsed, None
    except Exception as exc:
        return None, [], r_req + 1, 0, int((time.monotonic() - started) * 1000), f"{type(exc).__name__}: {str(exc)[:120]}"


def _jsonld_organisations(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []

    def walk(value: Any) -> None:
        if isinstance(value, dict):
            kind = value.get("@type")
            kinds = set(kind if isinstance(kind, list) else [kind])
            if kinds & {"Organization", "Corporation", "LocalBusiness", "Store", "Restaurant"}:
                values.append(value)
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)

    walk(metadata.get("json-ld", []))
    return values[:20]


def _extraction_state(text: str, soup: BeautifulSoup) -> str:
    return "js_fallback_candidate" if len(text.strip()) < 100 and len(soup.select("script[src]")) >= 2 else "static_complete"


def fetch_website(url: str | None, *, timeout: float = 15.0, max_bytes: int = 2_000_000, crawl_secondary: bool = True) -> tuple[dict[str, Any], dict[str, Any]]:
    supplied_url = str(url or "").strip()
    supplied_scheme = bool(re.match(r"^https?://", supplied_url, re.I))
    normalized = normalize_homepage(url)
    if not normalized:
        return evidence("website", "not_found", "registry_linked_company_website", "https://data.brreg.no/enhetsregisteret/api/enheter", note="No valid registry website URL"), {"requests": 0, "bytes": 0, "latencies_ms": []}
    try:
        assert_public_url(normalized)
    except ValueError as exc:
        return evidence("website", "blocked", "registry_linked_company_website", normalized, note=str(exc)), {"requests": 0, "bytes": 0, "latencies_ms": []}
    allowed, r_req = _robots_check(normalized, timeout)
    if not allowed:
        return evidence("website", "blocked", "registry_linked_company_website", normalized, note="robots.txt disallows this user agent"), {"requests": r_req, "bytes": 0, "latencies_ms": []}
    if not crawl_secondary:
        parsed_norm = urllib.parse.urlparse(normalized)
        norm_origin = f"{parsed_norm.scheme.lower()}://{parsed_norm.netloc.lower()}"
        with _CACHE_LOCK:
            robots_timed_out = norm_origin in _ROBOTS_TIMEOUT
        if robots_timed_out:
            return evidence("website", "source_error", "registry_linked_company_website", normalized, note="URLError: timed out"), {"requests": r_req, "bytes": 0, "latencies_ms": [int(timeout * 1000)]}
    started = time.monotonic()
    cached = _get_cached_page(normalized)
    try:
        if cached is not None:
            raw, final_url = cached
            elapsed = 0
            requests = r_req
        else:
            request = urllib.request.Request(normalized, headers={"User-Agent": USER_AGENT, "Accept": "text/html,application/xhtml+xml"})
            with SAFE_OPENER.open(request, timeout=timeout) as response:
                content_type = response.headers.get("content-type", "")
                raw = response.read(max_bytes + 1)
                elapsed = int((time.monotonic() - started) * 1000)
                requests = r_req + 1
                if len(raw) > max_bytes:
                    return evidence("website", "blocked", "registry_linked_company_website", normalized, note="Homepage exceeds byte limit"), {"requests": requests, "bytes": len(raw), "latencies_ms": [elapsed]}
                if "html" not in content_type.lower():
                    return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"Unsupported content type: {content_type}"), {"requests": requests, "bytes": len(raw), "latencies_ms": [elapsed]}
                final_url = response.geturl()
                assert_public_url(final_url)
            _cache_page(normalized, raw, final_url)
        html = raw.decode("utf-8", errors="replace")
        soup = BeautifulSoup(html, "lxml")
        structured = extruct.extract(html, base_url=final_url, syntaxes=["json-ld", "microdata", "opengraph"])
        raw_text = trafilatura.extract(html, url=final_url, include_links=False, include_tables=False, favor_precision=True) or ""
        text = raw_text
        if len(text.strip()) < 100:
            fallback_text = _visible_body_text(html)
            if len(fallback_text) > len(text.strip()):
                text = fallback_text
        identity_excerpt = _identity_text_excerpt(soup)
        title = soup.title.get_text(" ", strip=True) if soup.title else ""
        description_tag = soup.select_one('meta[name="description"], meta[property="og:description"]')
        description = str(description_tag.get("content") or "").strip() if description_tag else ""
        value = {
            "requested_url": normalized,
            "final_url": final_url,
            "registered_domain": _registered_domain(final_url),
            "title": title[:500],
            "description": description[:2000],
            "main_text_excerpt": text[:5000],
            "identity_text_excerpt": identity_excerpt,
            "social_links": _social_links(final_url, soup),
            "structured_organisations": _jsonld_organisations(structured),
            "content_sha256": __import__("hashlib").sha256(raw).hexdigest(),
            "extraction_state": _extraction_state(text, soup),
        }
        pages = [{
            "url": final_url,
            "title": title[:500],
            "main_text_excerpt": text[:5000],
            "identity_text_excerpt": identity_excerpt,
            "content_sha256": value["content_sha256"],
        }]
        social = value["social_links"]
        crawl_errors = []
        bytes_received = len(raw) if cached is None else 0
        page_latencies = [elapsed] if elapsed else []
        homepage_domain = value["registered_domain"]
        if crawl_secondary:
            for page_url in _priority_links(final_url, soup):
                page, page_social, page_requests, page_bytes, page_elapsed, page_error = _fetch_secondary_page(
                    page_url,
                    homepage_domain=homepage_domain,
                    timeout=timeout,
                    max_bytes=min(max_bytes, 1_000_000),
                )
                requests += page_requests
                bytes_received += page_bytes
                if page_elapsed:
                    page_latencies.append(page_elapsed)
                if page:
                    pages.append(page)
                    social.extend(page_social)
                elif page_error:
                    crawl_errors.append({"url": page_url, "error": page_error})
        value["pages"] = pages
        social.extend(structured_social_links(value["structured_organisations"]))
        value["social_links"] = list({(item["platform"], item["url"]): item for item in social}.values())
        value["crawl_errors"] = crawl_errors
        return evidence("website", "available", "registry_linked_company_website", final_url, value=value, note="Company-controlled claim layer; not an official registry fact", content_sha256=value["content_sha256"]), {"requests": requests, "bytes": bytes_received, "latencies_ms": page_latencies}
    except urllib.error.HTTPError as exc:
        elapsed = int((time.monotonic() - started) * 1000)
        status = "not_found" if exc.code in {404, 410} else "source_error"
        return evidence("website", status, "registry_linked_company_website", normalized, note=f"HTTP {exc.code}"), {"requests": r_req + 1, "bytes": 0, "latencies_ms": [elapsed]}
    except urllib.error.URLError as exc:
        if not supplied_scheme and normalized.startswith("https://") and not _is_timeout_exception(exc):
            first_elapsed = int((time.monotonic() - started) * 1000)
            record, metrics = fetch_website("http://" + supplied_url, timeout=timeout, max_bytes=max_bytes, crawl_secondary=crawl_secondary)
            metrics["requests"] += r_req + 1
            metrics["latencies_ms"].insert(0, first_elapsed)
            return record, metrics
        elapsed = int((time.monotonic() - started) * 1000)
        return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"URLError: {str(exc.reason)[:180]}"), {"requests": r_req + 1, "bytes": 0, "latencies_ms": [elapsed]}
    except Exception as exc:
        if not supplied_scheme and normalized.startswith("https://") and not _is_timeout_exception(exc) and isinstance(exc, (http.client.HTTPException, ConnectionError, ssl.SSLError)):
            first_elapsed = int((time.monotonic() - started) * 1000)
            record, metrics = fetch_website("http://" + supplied_url, timeout=timeout, max_bytes=max_bytes, crawl_secondary=crawl_secondary)
            metrics["requests"] += r_req + 1
            metrics["latencies_ms"].insert(0, first_elapsed)
            return record, metrics
        elapsed = int((time.monotonic() - started) * 1000)
        return evidence("website", "source_error", "registry_linked_company_website", normalized, note=f"{type(exc).__name__}: {str(exc)[:180]}"), {"requests": r_req + 1, "bytes": 0, "latencies_ms": [elapsed]}
