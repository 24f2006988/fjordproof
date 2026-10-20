"""Optional model-selected description of what a company does, quoted from its own verified website.

Off unless ``GEMINI_API_KEY`` is set (and ``SIGNALPOST_LLM`` is not ``off``); with no key it makes no request and the
envelopes are exactly as without this module, so the agent stays credential-free. ``GEMINI_MODEL`` overrides the model.

The model only chooses: it is asked to copy up to three sentences from the text of a page whose identity was already
proven for this exact organisation number. Every returned sentence is kept only if it occurs verbatim (whitespace
aside) in that page text, so the model cannot add a number, name, date or claim that the page does not state.
Nothing is sent but the page's own public text; nothing from the model is published except those checked quotes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any

from .evidence import evidence, utc_now

DEFAULT_MODEL = "gemini-2.5-flash-lite"
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# USD per 1M tokens (Gemini 2.5 Flash-Lite paid tier, text input / output), used for the declared run cost.
PRICE_PER_M_TOKENS = {"input": 0.10, "output": 0.40}
MAX_SENTENCES = 3
MIN_CHARS, MAX_CHARS = 25, 240  # a span longer than the evidence span limit could not be quoted in full
MAX_PAGE_CHARS = 5000

PROMPT = """You are given the visible text of the official website of the Norwegian company "{name}" (organisation number {org}).
Copy, word for word, at most {n} sentences from the text that best describe what this company does: its products,
services, customers or field. Copy them exactly as written, in the original language. Do not translate, shorten,
merge, correct or add anything. Skip cookie notices, menus, contact details, prices and sentences about other
companies. If no sentence describes the company's business, return an empty list.

TEXT:
\"\"\"
{text}
\"\"\""""


def model() -> str:
    return os.environ.get("GEMINI_MODEL", "").strip() or DEFAULT_MODEL


def configured() -> bool:
    return bool(os.environ.get("GEMINI_API_KEY", "").strip()) and os.environ.get("SIGNALPOST_LLM", "").strip().lower() != "off"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def verbatim_sentences(candidates: Any, page_text: str) -> list[str]:
    """The model's sentences that really occur in the page text, de-duplicated, in page order."""
    page = _norm(page_text)
    kept: list[tuple[int, str]] = []
    for s in candidates if isinstance(candidates, list) else []:
        s = _norm(str(s))
        pos = page.find(s) if MIN_CHARS <= len(s) <= MAX_CHARS else -1
        if pos >= 0 and all(s != k and s not in k and k not in s for _, k in kept):
            kept.append((pos, s))
    return [s for _, s in sorted(kept)][:MAX_SENTENCES]


def _call(prompt: str, timeout: float) -> tuple[list[str], dict[str, int]]:
    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {
            "temperature": 0, "maxOutputTokens": 400, "responseMimeType": "application/json",
            "responseSchema": {"type": "OBJECT", "properties": {"sentences": {"type": "ARRAY", "items": {"type": "STRING"}}},
                               "required": ["sentences"]},
        },
    }
    req = urllib.request.Request(ENDPOINT.format(model=model()), data=json.dumps(body).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "x-goog-api-key": os.environ["GEMINI_API_KEY"].strip()})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read())
    usage = payload.get("usageMetadata") or {}
    tokens = {"input": int(usage.get("promptTokenCount") or 0), "output": int(usage.get("candidatesTokenCount") or 0)}
    parts = (((payload.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [{}])
    text = "".join(str(p.get("text") or "") for p in parts)
    return (json.loads(text).get("sentences") or []) if text.strip() else [], tokens


def cost_usd(tokens: dict[str, int]) -> float:
    return sum(tokens.get(k, 0) * PRICE_PER_M_TOKENS[k] for k in PRICE_PER_M_TOKENS) / 1_000_000


def describe(profile: dict[str, Any], timeout: float = 20.0, caller=_call) -> tuple[dict[str, Any] | None, int, float]:
    """(site_description evidence record or None when not attempted, outbound requests, USD cost). Never raises."""
    site = profile.get("evidence", {}).get("website") or {}
    value = site.get("value") or {}
    if not configured() or site.get("status") != "available" or (value.get("identity_assessment") or {}).get("publishable") is not True:
        return None, 0, 0.0
    page_text = str(value.get("main_text_excerpt") or "")[:MAX_PAGE_CHARS]
    url = value.get("final_url") or site.get("source_url")
    if len(_norm(page_text)) < MIN_CHARS:
        return evidence("site_description", "not_found", "company_owned_site", url, note="Verified homepage has no readable text."), 0, 0.0
    prompt = PROMPT.format(name=profile.get("name") or "", org=profile.get("organisation_number") or "", n=MAX_SENTENCES, text=page_text)
    started = time.monotonic()
    try:
        candidates, tokens = caller(prompt, timeout)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, KeyError, IndexError) as exc:
        return evidence("site_description", "source_error", "company_owned_site", url, note=f"{model()}: {type(exc).__name__}"), 1, 0.0
    cost = cost_usd(tokens)
    sentences = verbatim_sentences(candidates, page_text)
    meta = {"model": model(), "method": "model-selected sentences, each checked verbatim against the fetched page text",
            "candidates": len(candidates) if isinstance(candidates, list) else 0, "tokens": tokens, "cost_usd": round(cost, 6),
            "elapsed_ms": int((time.monotonic() - started) * 1000)}
    if not sentences:
        return evidence("site_description", "not_found", "company_owned_site", url, value=meta,
                        note="No model-selected sentence occurs verbatim on the verified page; nothing published."), 1, cost
    return evidence("site_description", "available", "company_owned_site", url, value={**meta, "sentences": sentences},
                    retrieved_at=site.get("retrieved_at") or utc_now(),
                    content_sha256=site.get("content_sha256") or hashlib.sha256(page_text.encode("utf-8")).hexdigest()), 1, cost
