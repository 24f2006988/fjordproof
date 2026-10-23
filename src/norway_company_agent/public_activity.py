"""Dated public activity from official sources keyed by the organisation number (no names, no search, no keys).

- TED (EU Tenders Electronic Daily, API v3): contract award notices that list this organisation number as a winner
  registered in Norway (winner-country NOR at the same position). One search per 50 companies per run.
- SkatteFUNN (Research Council of Norway): the published list of approved R&D tax-credit projects, one file per run,
  rows whose 'Organisasjonsnr' is this company.

Every item carries its own date (TED publication date / SkatteFUNN decision date), a title, a direct URL and the
literal source span, so it is a dated fact about this company only. Nothing is published from a name match.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import threading
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import date, timedelta
from typing import Any

from .evidence import evidence, utc_now

UA = "builderr-signalpost-poc/0.1 (+https://builderr.ai)"
TED_SEARCH = "https://api.ted.europa.eu/v3/notices/search"
TED_NOTICE = "https://ted.europa.eu/en/notice/-/detail/{pub}"
TED_FIELDS = ["publication-number", "publication-date", "notice-type", "winner-identifier", "winner-country", "buyer-name", "title-proc"]
TED_BATCH = 50
SKATTEFUNN_LIST = "https://www.forskningsradet.no/siteassets/skattefunn/tall/260902-webliste-skattefunn.xlsx"
SKATTEFUNN_PAGE = "https://www.forskningsradet.no/skattefunn/"
MAX_ITEMS_PER_SOURCE = 10
_NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}


def _digits(value: Any) -> str:
    return re.sub(r"\D", "", str(value or ""))


def _first_text(value: Any) -> str:
    """TED multilingual fields: {'eng': 'x'} or {'eng': ['x']}; English first, else any language."""
    if isinstance(value, dict):
        for key in ["eng", *sorted(value)]:
            if key in value:
                got = _first_text(value[key])
                if got:
                    return got
        return ""
    if isinstance(value, list):
        return next((t for t in (_first_text(v) for v in value) if t), "")
    return " ".join(str(value or "").split())


def _excel_date(serial: str) -> str:
    try:
        return (date(1899, 12, 30) + timedelta(days=int(float(serial)))).isoformat()
    except (TypeError, ValueError):
        return ""


def _xlsx_rows(raw: bytes) -> list[list[str]]:
    """First worksheet as rows of cell strings (stdlib only: an .xlsx file is a zip of XML parts)."""
    z = zipfile.ZipFile(io.BytesIO(raw))
    shared: list[str] = []
    if "xl/sharedStrings.xml" in z.namelist():
        shared = ["".join(t.text or "" for t in si.iter(f"{{{_NS['m']}}}t"))
                  for si in ET.fromstring(z.read("xl/sharedStrings.xml")).findall("m:si", _NS)]
    rows: list[list[str]] = []
    for row in ET.fromstring(z.read("xl/worksheets/sheet1.xml")).iter(f"{{{_NS['m']}}}row"):
        cells: dict[int, str] = {}
        for c in row.findall("m:c", _NS):
            ref = re.match(r"([A-Z]+)", c.get("r") or "")
            col = 0
            for ch in ref.group(1) if ref else "":
                col = col * 26 + ord(ch) - 64
            v = c.find("m:v", _NS)
            if c.get("t") == "s" and v is not None:
                text = shared[int(v.text)]
            elif v is not None:
                text = v.text or ""
            else:
                text = "".join(t.text or "" for t in c.iter(f"{{{_NS['m']}}}t"))
            cells[col - 1] = text
        rows.append([cells.get(i, "") for i in range(max(cells) + 1)] if cells else [])
    return rows


class PublicActivity:
    """Run-level load of both sources for all companies of the batch; per-company lookups afterwards are free."""

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout
        self.requests = 0
        self.errors: dict[str, str] = {}
        self.retrieved_at: dict[str, str] = {}
        self.sha256: dict[str, str] = {}
        self.ted: dict[str, list[dict[str, Any]]] = {}
        self.skattefunn: dict[str, list[dict[str, Any]]] = {}
        self.ready = threading.Event()

    def _post(self, url: str, body: dict[str, Any]) -> bytes:
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"User-Agent": UA, "Content-Type": "application/json", "Accept": "application/json"})
        self.requests += 1
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            return resp.read()

    def _get(self, url: str) -> bytes:
        self.requests += 1
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=self.timeout) as resp:
            return resp.read()

    def load(self, orgs: list[str]) -> None:
        try:
            self._load_ted([o for o in dict.fromkeys(_digits(o) for o in orgs) if len(o) == 9])
        except Exception as exc:  # noqa: BLE001  one source failing never stops the run
            self.errors["ted"] = type(exc).__name__
        try:
            self._load_skattefunn()
        except Exception as exc:  # noqa: BLE001
            self.errors["skattefunn"] = type(exc).__name__
        self.ready.set()

    def _load_ted(self, orgs: list[str]) -> None:
        self.retrieved_at["ted"] = utc_now()
        hashes = []
        for i in range(0, len(orgs), TED_BATCH):
            chunk = orgs[i:i + TED_BATCH]
            raw = self._post(TED_SEARCH, {"query": f"winner-identifier IN ({' '.join(chunk)})", "fields": TED_FIELDS,
                                          "limit": 250, "scope": "ALL"})
            hashes.append(hashlib.sha256(raw).hexdigest())
            wanted = set(chunk)
            for notice in json.loads(raw).get("notices") or []:
                ids = [_digits(w) for w in notice.get("winner-identifier") or []]
                countries = notice.get("winner-country") or []
                pub = str(notice.get("publication-number") or "")
                when = str(notice.get("publication-date") or "")[:10]
                title = _first_text(notice.get("title-proc"))
                if not (pub and re.fullmatch(r"\d{4}-\d{2}-\d{2}", when) and title):
                    continue
                for pos, org in enumerate(ids):
                    # A 9-digit identifier is also a Portuguese/Greek tax number: the winner at this position must be Norwegian.
                    country = countries[pos] if len(countries) == len(ids) else (countries[0] if len(set(countries)) == 1 else "")
                    if org in wanted and country == "NOR" and not any(it["notice"] == pub for it in self.ted.get(org, [])):
                        buyer = _first_text(notice.get("buyer-name"))
                        self.ted.setdefault(org, []).append({
                            "source": "TED contract award", "kind": "public_contract_award", "notice": pub,
                            "title": title[:200], "buyer": buyer[:200], "date": when, "notice_type": str(notice.get("notice-type") or ""),
                            "url": TED_NOTICE.format(pub=pub),
                            "span": f"TED {pub} ({when}): winner-identifier {org}, winner-country NOR; buyer {buyer}; {title}"[:240],
                        })
        self.sha256["ted"] = hashlib.sha256("".join(hashes).encode()).hexdigest()

    def _load_skattefunn(self) -> None:
        self.retrieved_at["skattefunn"] = utc_now()
        raw = self._get(SKATTEFUNN_LIST)
        self.sha256["skattefunn"] = hashlib.sha256(raw).hexdigest()
        rows = _xlsx_rows(raw)
        head = [h.strip().casefold() for h in rows[0]] if rows else []
        col = {name: head.index(name) for name in ("organisasjonsnr", "prosjekttittel", "vedtaksdato", "godkjent", "prosjektnummer",
                                                   "fra-år", "til-år") if name in head}
        if not {"organisasjonsnr", "prosjekttittel", "vedtaksdato"} <= set(col):
            raise ValueError("SkatteFUNN list columns changed")
        for line, row in enumerate(rows[1:], start=2):
            cell = lambda name: row[col[name]].strip() if name in col and col[name] < len(row) else ""  # noqa: E731
            org, title, decided = _digits(cell("organisasjonsnr")), " ".join(cell("prosjekttittel").split()), _excel_date(cell("vedtaksdato"))
            if len(org) != 9 or not title or not decided or (cell("godkjent") and cell("godkjent").upper() != "JA"):
                continue
            first, last = _excel_date(cell("fra-år"))[:4], _excel_date(cell("til-år"))[:4]
            self.skattefunn.setdefault(org, []).append({
                "source": "SkatteFUNN approved R&D project", "kind": "rd_tax_credit_project", "project": cell("prosjektnummer"),
                "title": title[:200], "date": decided, "period": f"{first}-{last}" if first and last else "", "url": SKATTEFUNN_LIST,
                "row": line, "span": f"SkatteFUNN list row {line}: Organisasjonsnr {org}; Prosjekttittel {title}; Vedtaksdato {decided}"[:240],
            })


def fetch_activity(profile: dict[str, Any], pa: PublicActivity) -> dict[str, Any]:
    """Evidence record 'public_activity' for one company: dated items whose source row names this organisation number."""
    org = _digits(profile.get("organisation_number"))
    items: list[dict[str, Any]] = []
    for source, table in (("ted", pa.ted), ("skattefunn", pa.skattefunn)):
        if source in pa.errors:
            continue
        for item in sorted(table.get(org, []), key=lambda i: i["date"], reverse=True)[:MAX_ITEMS_PER_SOURCE]:
            items.append({**item, "retrieved_at": pa.retrieved_at.get(source), "content_sha256": pa.sha256.get(source)})
    items.sort(key=lambda i: (i["date"], i["url"]), reverse=True)
    checked = [s for s in ("ted", "skattefunn") if s not in pa.errors]
    if items:
        return evidence("public_activity", "available", "official_public_activity", items[0]["url"], value={"items": items, "sources": checked},
                        retrieved_at=utc_now(), note="Dated items from TED / SkatteFUNN whose own row names this organisation number")
    if not checked:
        return evidence("public_activity", "source_error", "official_public_activity", TED_SEARCH,
                        note=f"TED and SkatteFUNN unavailable: {pa.errors}")
    return evidence("public_activity", "not_found", "official_public_activity", TED_SEARCH if "ted" in checked else SKATTEFUNN_LIST,
                    value={"items": [], "sources": checked}, note="No TED award or SkatteFUNN project names this organisation number")
