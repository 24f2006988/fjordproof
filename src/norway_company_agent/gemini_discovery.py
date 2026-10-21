"""Optional website candidates suggested by Gemini, for companies the registry gives no homepage.

Off unless ``GEMINI_API_KEY`` is set (and ``SIGNALPOST_GEMINI_DISCOVERY`` is not ``off``); with no key it makes no
request and discovery is exactly as without it. Gemini is asked for the company's own website domain, using Google
Search grounding when the key's quota allows it (``SIGNALPOST_GEMINI_SEARCH=off`` disables search), else from the
model alone. Its answer is only a candidate list: every host is fetched and must pass the same exact-entity proof as
any guessed domain (org number, registered address, or the unique-name rule), so a wrong suggestion is never published.
Only the company's public registry name, number, address and industry are sent. Cost is declared per company.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
DEFAULT_MODEL = "gemini-3.5-flash-lite"
MAX_HOSTS = 3
# Declared cost estimates (USD): tokens at Flash-Lite list price, plus each Google Search query the model runs.
PRICE_PER_M_TOKENS = {"input": 0.10, "output": 0.40}
SEARCH_QUERY_USD = 0.014
NOT_COMPANY = ("brreg.no", "proff.no", "purehelp.no", "gulesider.no", "1881.no", "finn.no", "facebook.com", "linkedin.com",
               "instagram.com", "regnskapstall.no", "forvalt.no", "bedriftsdatabasen.no", "vertexaisearch.cloud.google.com",
               "google.com", "wikipedia.org", "youtube.com", "kununu.com", "allabolag.se", "dnb.com", "opencorporates.com")

PROMPT = """Find the official website of this Norwegian company.
Name: {name}
Organisation number: {org}
Registered address: {address}
Industry: {industry}
Return the domain of the company's OWN website only (for example "example.no"). Do not return registries, directories,
social media, news sites, or the website of a different company with a similar name. If you are not sure, return none."""

_lock = threading.Lock()
_last_call = 0.0
_search_blocked = False  # set when the key has no search quota; later calls go without the search tool


def configured() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY", "").strip()) and os.environ.get("SIGNALPOST_GEMINI_DISCOVERY", "").strip().lower() != "off"


def model() -> str:
    return os.environ.get("GEMINI_DISCOVERY_MODEL", "").strip() or DEFAULT_MODEL


def _throttle() -> None:
    """Spacing between calls (SIGNALPOST_GEMINI_MIN_INTERVAL seconds; free-tier keys allow only a few calls a minute)."""
    global _last_call
    gap = float(os.environ.get("SIGNALPOST_GEMINI_MIN_INTERVAL", "0") or 0)
    with _lock:
        wait = _last_call + gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_call = time.monotonic()


def _post(body: dict[str, Any], timeout: float) -> dict[str, Any]:
    req = urllib.request.Request(ENDPOINT.format(model=model()), data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"].strip()})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _host(text: str) -> str:
    text = text.strip().strip(".,;:'\"()[]<>").casefold()
    if "//" not in text:
        text = "http://" + text
    host = (urllib.parse.urlparse(text).hostname or "").removeprefix("www.")
    return host if re.fullmatch(r"[a-z0-9æøå-]+(\.[a-z0-9æøå-]+)+", host or "") else ""


def candidate_hosts(profile: dict[str, Any], timeout: float = 30.0) -> tuple[list[str], int, float, str | None]:
    """(hosts best first, requests made, declared cost in USD, mode "search" / "model" / None). Never raises."""
    global _search_blocked
    name = " ".join(str(profile.get("name") or "").split())
    if not configured() or not name:
        return [], 0, 0.0, None
    live = ((profile.get("evidence") or {}).get("registry_live") or {}).get("value") or {}
    addr = live.get("business_address") or {}
    prompt = PROMPT.format(name=name, org=profile.get("organisation_number"),
                           address=" ".join([*(addr.get("adresse") or []), str(addr.get("postnummer") or ""), str(addr.get("poststed") or "")]).strip() or "unknown",
                           industry=profile.get("industry_label") or "unknown")
    use_search = not _search_blocked and os.environ.get("SIGNALPOST_GEMINI_SEARCH", "").strip().lower() != "off"
    requests = 0
    for attempt in range(2):
        body: dict[str, Any] = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                                "generationConfig": {"temperature": 0, "maxOutputTokens": 200}}
        if use_search:
            body["tools"] = [{"google_search": {}}]
        _throttle()
        requests += 1
        try:
            data = _post(body, timeout)
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and use_search:
                _search_blocked, use_search = True, False  # no search quota on this key: model-only from now on
                continue
            return [], requests, 0.0, None
        except Exception:  # noqa: BLE001  network failure: discovery continues without this candidate source
            return [], requests, 0.0, None
        cand = (data.get("candidates") or [{}])[0]
        text = "".join(p.get("text", "") for p in (cand.get("content") or {}).get("parts") or [])
        grounding = cand.get("groundingMetadata") or {}
        found: list[str] = []
        for token in re.findall(r"[A-Za-z0-9æøåÆØÅ.:/-]+\.[A-Za-z]{2,}[A-Za-z0-9/._-]*", text):
            host = _host(token)
            if host and not any(host == d or host.endswith("." + d) for d in NOT_COMPANY) and host not in found:
                found.append(host)
        for chunk in grounding.get("groundingChunks") or []:  # source titles are the result sites' domains
            host = _host(str((chunk.get("web") or {}).get("title") or ""))
            if host and not any(host == d or host.endswith("." + d) for d in NOT_COMPANY) and host not in found:
                found.append(host)
        usage = data.get("usageMetadata") or {}
        cost = (usage.get("promptTokenCount", 0) * PRICE_PER_M_TOKENS["input"]
                + (usage.get("candidatesTokenCount", 0) + usage.get("thoughtsTokenCount", 0)) * PRICE_PER_M_TOKENS["output"]) / 1_000_000
        cost += SEARCH_QUERY_USD * len(grounding.get("webSearchQueries") or [])
        return found[:MAX_HOSTS], requests, round(cost, 6), "search" if use_search else "model"
    return [], requests, 0.0, None
