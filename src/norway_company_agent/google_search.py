"""Optional Google candidate discovery for companies without a registered website.

Off unless a key is set in the environment; with no key it returns no candidates and makes no request,
so the agent stays credential-free. Two providers, tried in this order:

* Custom Search JSON API: ``GOOGLE_SEARCH_API_KEY`` + ``GOOGLE_CSE_ID`` (a Programmable Search Engine
  that searches the whole web).
* Places API (New) Text Search: ``GOOGLE_PLACES_API_KEY`` (business listings with a ``websiteUri``).

Results are candidates only. Nothing from the provider is stored or published: callers receive bare
hostnames, crawl them themselves and publish a site only after exact-entity proof on its own pages.
"""
from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from typing import Any

from .discovery import BLOCKED_DISCOVERY_HOSTS, _tokens

CSE_ENDPOINT = "https://www.googleapis.com/customsearch/v1"
PLACES_ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
MAX_CANDIDATES = 3
# Directories, registries, marketplaces and platforms: never a company-owned website.
NOT_COMPANY_HOSTS = BLOCKED_DISCOVERY_HOSTS | {
    "brreg.no", "regnskapstall.no", "bedriftsdatabasen.no", "finn.no", "wikipedia.org", "google.com", "google.no",
    "kununu.com", "trustpilot.com", "mittanbud.no", "anbudstorget.no", "bygg.no", "krak.no", "eniro.no", "proff.dk",
    "allabolag.se", "hitta.se", "dnb.com", "opencorporates.com", "bisnode.no", "vainu.com", "infobel.com",
    "cylex.no", "yelp.com", "tripadvisor.com", "tripadvisor.no", "booking.com", "pinterest.com", "apple.com",
}


def configured() -> str | None:
    """Name of the provider that will be used, or None when no key is set."""
    if os.environ.get("GOOGLE_SEARCH_API_KEY", "").strip() and os.environ.get("GOOGLE_CSE_ID", "").strip():
        return "google_custom_search"
    if os.environ.get("GOOGLE_PLACES_API_KEY", "").strip():
        return "google_places"
    return None


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).hostname or "").casefold().removeprefix("www.")
    except ValueError:
        return ""


def _company_host(host: str) -> bool:
    return bool(host) and "." in host and not any(host == h or host.endswith("." + h) for h in NOT_COMPANY_HOSTS)


def _address(profile: dict[str, Any]) -> tuple[str, str]:
    live = (profile.get("evidence", {}).get("registry_live", {}).get("value") or {})
    addr = live.get("business_address") or live.get("postal_address") or {}
    raw = (profile.get("evidence", {}).get("registry", {}).get("value") or {})
    postcode = str(addr.get("postnummer") or raw.get("forretningsadresse.postnummer") or "")
    city = str(addr.get("poststed") or raw.get("forretningsadresse.poststed") or profile.get("municipality") or "")
    return postcode, city


def _rank(profile: dict[str, Any], items: list[tuple[str, str]]) -> list[str]:
    """items: (url, text shown with it). Company-looking hosts only; name-in-host and org-number hits first."""
    org = re.sub(r"\D", "", str(profile.get("organisation_number") or ""))
    name = "".join(t for t in _tokens(profile.get("name")) if t not in {"as", "asa", "ans", "da", "enk", "sa", "nuf"})
    scored: list[tuple[int, int, str]] = []
    seen: set[str] = set()
    for rank, (url, text) in enumerate(items):
        host = _host(url)
        if not _company_host(host) or host in seen:
            continue
        seen.add(host)
        core = "".join(_tokens(host.rsplit(".", 1)[0]))
        score = (2 if org and org in re.sub(r"\D", "", text) else 0) + (1 if name and core and (name in core or core in name) else 0)
        scored.append((-score, rank, host))
    return [h for _, _, h in sorted(scored)][:MAX_CANDIDATES]


def _post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout: float) -> Any:
    req = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "Accept": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _get_json(url: str, timeout: float) -> Any:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def google_candidate_hosts(profile: dict[str, Any], timeout: float = 8.0) -> tuple[list[str], int, str | None]:
    """(candidate hosts best first, outbound requests made, provider). Never raises; at most one request."""
    provider = configured()
    name = " ".join(str(profile.get("name") or "").split())
    if not provider or not name:
        return [], 0, provider
    postcode, city = _address(profile)
    try:
        if provider == "google_custom_search":
            org = re.sub(r"\D", "", str(profile.get("organisation_number") or ""))
            query = f'"{name}" {org} {city}'.strip()
            url = CSE_ENDPOINT + "?" + urllib.parse.urlencode({
                "key": os.environ["GOOGLE_SEARCH_API_KEY"].strip(), "cx": os.environ["GOOGLE_CSE_ID"].strip(),
                "q": query, "gl": "no", "num": 10, "safe": "active", "fields": "items(link,title,snippet)"})
            payload = _get_json(url, timeout)
            items = [(i.get("link") or "", f"{i.get('title', '')} {i.get('snippet', '')}") for i in (payload.get("items") or [])]
        else:
            payload = _post_json(PLACES_ENDPOINT, {"textQuery": f"{name} {postcode} {city}".strip(), "regionCode": "NO", "pageSize": 5},
                                 {"X-Goog-Api-Key": os.environ["GOOGLE_PLACES_API_KEY"].strip(),
                                  "X-Goog-FieldMask": "places.websiteUri,places.formattedAddress"}, timeout)
            places = payload.get("places") or []
            # Prefer listings at one of the company's own postcodes; others still need proof on the site.
            places.sort(key=lambda p: 0 if postcode and postcode in str(p.get("formattedAddress") or "") else 1)
            items = [(p.get("websiteUri") or "", "") for p in places if p.get("websiteUri")]
        return _rank(profile, items), 1, provider
    except Exception:  # quota, network, bad key: the run continues credential-free for this company
        return [], 1, provider
