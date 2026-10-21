"""Workforce (full-time equivalents) read from the company's own filed annual accounts.

Brreg's copy of a filed annual account is a scanned PDF. Its generated NOTEOPPLYSNINGER page carries the
structured field "Antall årsverk i regnskapsåret" (printed as e.g. "2.00"). Only that page is OCR'd, and a
value is published only when the page header shows this company's organisation number, the label is read,
and the number sits on the same row in the field's fixed two-decimal format. Anything else is not_available.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from .evidence import evidence, utc_now
from .official import BRREG_ACCOUNT_PDF, _reserve_history_slot

LABEL = re.compile(r"(?i)antall\s*[aå]rs?verk\s*i\s*regnskaps[aå]ret")
FTE = re.compile(r"^(\d{1,5})[.,](\d{2})$")
MIN_CONFIDENCE = 0.9
ZERO_FTE_MAX_WAGES = 100_000  # NOK; at or above this a printed 0.00 man-years is treated as an unfilled form default

# One OCR engine per worker thread, each with its share of the CPU cores. A single shared engine runs one page at a time
# on its own thread pool, so parallel workers queued behind each other (measured: ~65 s per copy in a 4-worker run).
_local = threading.local()
_threads_per_engine = max(1, (os.cpu_count() or 4) // 4)


def configure(workers: int) -> None:
    """Split the machine's cores between the OCR workers (call before the OCR stage)."""
    global _threads_per_engine
    _threads_per_engine = max(1, (os.cpu_count() or 4) // max(1, workers))


def _ocr_engine():
    engine = getattr(_local, "engine", None)
    if engine is None:
        from rapidocr_onnxruntime import RapidOCR  # optional at import time: a missing engine is reported, never raised

        engine = _local.engine = RapidOCR(intra_op_num_threads=_threads_per_engine, inter_op_num_threads=1)
    return engine


def _ocr(img, box=None) -> list[tuple[list, str, float]]:
    if box:
        img = img.crop(box)
    buf = io.BytesIO()
    img.convert("L").save(buf, "PNG")
    result, _ = _ocr_engine()(buf.getvalue())
    return [(r[0], r[1], float(r[2])) for r in (result or [])]


def read_fte(pdf: bytes, org: str) -> dict[str, Any]:
    """Return {"value", "page", "span", "confidence"} or {"value": None, "reason"}."""
    from PIL import Image
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf))
    org_digits = re.sub(r"\D", "", org)
    for index, page in enumerate(reader.pages[:9]):
        if index < 3 or not page.images:  # pages 0-2 are always general information, income statement, balance
            continue
        img = Image.open(io.BytesIO(page.images[0].data))
        width, height = img.size
        head = " ".join(text for _, text, _ in _ocr(img, (0, 0, width, int(height * 0.2))))
        if "NOTEOPPLYSNINGER" not in head.upper().replace(" ", ""):
            continue
        if org_digits not in re.sub(r"\D", "", head):
            return {"value": None, "reason": "notes page header does not show this organisation number"}
        lines = _ocr(img)
        for box, text, conf in lines:
            if not LABEL.search(text):
                continue
            mid = (box[0][1] + box[2][1]) / 2
            # The field value is printed either right of the label on the same row or directly below it in the same column.
            row = sorted((b[0][0], t, c) for b, t, c in lines if abs((b[0][1] + b[2][1]) / 2 - mid) < 25 and b[0][0] >= box[0][0])
            below = sorted(((b[0][1] + b[2][1]) / 2, t, c) for b, t, c in lines
                           if 10 < (b[0][1] + b[2][1]) / 2 - mid < 60 and abs(b[0][0] - box[0][0]) < 40)[:1]
            for _, cand, cand_conf in [(0, LABEL.split(text)[-1].strip(), conf)] + row + below:
                match = FTE.match(cand.replace(" ", ""))
                if match and min(conf, cand_conf) >= MIN_CONFIDENCE:
                    value = float(f"{match.group(1)}.{match.group(2)}")
                    return {"value": int(value) if value.is_integer() else value, "page": index + 1,
                            "span": f"{LABEL.search(text).group(0)} {cand}", "confidence": round(min(conf, cand_conf), 3)}
            return {"value": None, "reason": "label found but no confidently read value"}
        return {"value": None, "reason": "notes page has no man-year field"}
    return {"value": None, "reason": "no generated notes page in the first pages"}


def zero_contradiction(profile: dict[str, Any], year: str) -> str | None:
    """A printed 0.00 is often an unfilled form default. Publish it only when nothing in the official record contradicts it:
    the same year's wage costs are zero, or (without same-year accounts) the registry reports no employees."""
    records = ((profile.get("evidence", {}).get("financials") or {}).get("value") or {}).get("records") or []
    same_year = [r for r in records if str((r.get("period") or {}).get("tilDato") or "")[:4] == str(year)]
    wages = [r.get("wage_costs") for r in same_year if r.get("wage_costs") is not None]
    if wages:
        # Board fees and small one-off payments are booked as wage costs without any man-year; a real position is not.
        return None if max(wages) < ZERO_FTE_MAX_WAGES else f"filed man-years 0.00 but {year} wage costs are {max(wages):.0f}"
    if profile.get("employees"):
        return f"filed man-years 0.00 but the registry reports {profile.get('employees')} employees"
    return None


_downloads: dict[tuple[str, str], tuple[bytes | None, str | None, str | None, str]] = {}
_downloads_lock = threading.Lock()


def download_copy(org: str, year: str) -> tuple[tuple[bytes | None, str | None, str | None, str], int]:
    """((pdf bytes, failed status, note, retrieved_at), requests made now) for one filed annual-account copy, downloaded once per run.

    The runner calls this ahead of time, while the website stage is busy, so the OCR stage does not wait on the network."""
    key = (org, str(year))
    with _downloads_lock:
        if key in _downloads:
            return _downloads[key], 0
    url = BRREG_ACCOUNT_PDF.format(org=org, year=year)
    _reserve_history_slot()  # same Brreg endpoint family as the filings list: ~30 request starts per minute
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "builderr-signalpost-poc/0.1 (+https://builderr.ai)"})
        with urllib.request.urlopen(req, timeout=40) as response:
            result = (response.read(), None, None, utc_now())
    except urllib.error.HTTPError as exc:
        result = (None, "not_found" if exc.code in {404, 410} else "source_error", f"HTTP {exc.code}", utc_now())
    except Exception as exc:  # noqa: BLE001  network failure is reported per company
        result = (None, "source_error", type(exc).__name__, utc_now())
    with _downloads_lock:
        _downloads[key] = result
    return result, 1


def fetch_workforce(org: str, year: str | None, profile: dict[str, Any] | None = None) -> tuple[dict[str, Any], int]:
    """Evidence record for the latest filed year's man-years, and the number of requests made."""
    if not year:
        return evidence("workforce", "not_found", "official_annual_account_copy", BRREG_ACCOUNT_PDF.format(org=org, year=""),
                        note="No filed annual-account year to read."), 0
    url = BRREG_ACCOUNT_PDF.format(org=org, year=year)
    started = time.monotonic()
    (raw, failed, note, retrieved), used = download_copy(org, year)  # used = 0 when the copy was prefetched (counted then)
    if raw is None:
        return evidence("workforce", failed or "source_error", "official_annual_account_copy", url, note=note), used
    sha = hashlib.sha256(raw).hexdigest()
    try:
        result = read_fte(raw, org)
    except ImportError as exc:
        return evidence("workforce", "source_error", "official_annual_account_copy", url, note=f"OCR engine unavailable: {exc}",
                        content_sha256=sha, retrieved_at=retrieved), used
    except Exception as exc:  # noqa: BLE001  an unreadable PDF is not a fact
        return evidence("workforce", "not_found", "official_annual_account_copy", url, note=f"unreadable copy: {type(exc).__name__}",
                        content_sha256=sha, retrieved_at=retrieved), used
    if result.get("value") == 0 and profile is not None:
        conflict = zero_contradiction(profile, str(year))
        if conflict:
            result = {"value": None, "reason": conflict}
    if result.get("value") is None:
        return evidence("workforce", "not_found", "official_annual_account_copy", url, note=result.get("reason"),
                        content_sha256=sha, retrieved_at=retrieved), used
    value = {"full_time_equivalents": result["value"], "accounts_year": str(year), "page": result["page"],
             "span": result["span"], "ocr_confidence": result["confidence"],
             "elapsed_ms": int((time.monotonic() - started) * 1000)}
    return evidence("workforce", "available", "official_annual_account_copy", url, value=value, content_sha256=sha,
                    retrieved_at=retrieved), used
