"""Job postings from NAV's official job feed (pam-stilling-feed.nav.no, public token, the feed behind arbeidsplassen.nav.no).

Each ad's detail record names the employer's organisation number, usually the sub-unit (the workplace). An ad is
published for a company only when that number is the company itself or one of its own Brreg sub-units. Names only
choose which detail records to read; they never decide the match. One feed load per run is shared by all companies.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from typing import Any

from .evidence import evidence, utc_now

FEED = "https://pam-stilling-feed.nav.no"
PUBLIC_AD = "https://arbeidsplassen.nav.no/stillinger/stilling/{uuid}"
BRREG_SUBUNIT = "https://data.brreg.no/enhetsregisteret/api/underenheter/{org}"
UA = "builderr-signalpost-poc/0.1 (+https://builderr.ai)"
MAX_DETAILS_PER_COMPANY = 12
MAX_JOBS_PER_COMPANY = 10

LEGAL_SUFFIX = re.compile(r"\b(ASA|AS|SA|BA|DA|ANS|ENK|NUF|STI|HF|KF|IKS|SF|BRL|AVD|AVDELING)\b")
# Words too common in employer names to pick candidates on their own.
GENERIC = {
    "NORGE", "NORSK", "NORSKE", "NORWAY", "NORWEGIAN", "OG", "I", "PA", "PÅ", "AV", "FOR", "THE", "OF", "AND", "TIL",
    "BARNEHAGE", "BARNEHAGER", "SERVICE", "SERVICES", "HOLDING", "EIENDOM", "GRUPPEN", "GROUP", "KOMMUNE", "SYKEHUS",
    "SYKEHUSET", "SENTER", "SENTERET", "TRANSPORT", "BYGG", "ELEKTRO", "REGNSKAP", "CONSULTING", "INVEST", "DRIFT",
    "VEST", "OST", "ØST", "NORD", "SOR", "SØR", "MIDT", "OSLO", "BERGEN", "TRONDHEIM", "STAVANGER", "KLINIKK",
    "OMSORG", "ASSISTANSE", "SKOLE", "HOTEL", "HOTELL", "AUTO", "BIL", "MASKIN", "ENTREPRENOR", "ENTREPRENØR",
}


def norm(name: Any) -> str:
    text = unicodedata.normalize("NFKC", str(name or "")).upper()
    text = LEGAL_SUFFIX.sub(" ", text)
    return re.sub(r"[^0-9A-ZÆØÅ]+", " ", text).strip()


def tokens(name: Any) -> set[str]:
    return {t for t in norm(name).split() if len(t) >= 3 and t not in GENERIC}


def _get(url: str, headers: dict[str, str], timeout: float) -> tuple[Any, bytes]:
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw), raw


class JobFeed:
    """Active ads from the last `days` days of the feed, indexed by employer name; detail records cached per ad."""

    def __init__(self, days: int = 60, timeout: float = 30.0) -> None:
        self.days, self.timeout = days, timeout
        self.requests = 0
        self.error: str | None = None
        self.retrieved_at: str | None = None
        self.ads: list[dict[str, Any]] = []
        self._exact: dict[str, list[int]] = {}
        self._by_token: dict[str, set[int]] = {}
        self._token: str | None = None
        self._lock = threading.Lock()
        self._details: dict[str, tuple[dict[str, Any] | None, str | None, str | None]] = {}
        self._parents: dict[str, str | None] = {}
        self.ready = threading.Event()

    def _count(self) -> None:
        with self._lock:
            self.requests += 1

    def _auth(self) -> dict[str, str]:
        if self._token is None:
            with urllib.request.urlopen(urllib.request.Request(FEED + "/api/publicToken", headers={"User-Agent": UA}), timeout=self.timeout) as r:
                text = r.read().decode("utf-8", "replace")
            self._count()
            match = re.search(r"eyJ[A-Za-z0-9._-]+", text)
            if not match:
                raise RuntimeError("no public token")
            self._token = match.group(0)
        return {"Authorization": f"Bearer {self._token}"}

    def _call(self, path: str, extra: dict[str, str] | None = None) -> tuple[Any, bytes]:
        last: Exception | None = None
        for attempt in range(3):
            try:
                self._count()
                return _get(FEED + path, {**self._auth(), **(extra or {})}, self.timeout)
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    self._token = None  # token rotated: fetch a new public token once
                elif exc.code == 404:
                    raise
                last = exc
            except Exception as exc:  # noqa: BLE001  network failures are retried, then reported
                last = exc
            time.sleep(1.5 * (attempt + 1))
        raise last or RuntimeError("feed request failed")

    def load(self, deadline: float | None = None) -> None:
        """Page the feed once; on any failure the feed is marked failed and every company reports source_error."""
        try:
            since = format_datetime(datetime.now(timezone.utc) - timedelta(days=self.days), usegmt=True)
            latest: dict[str, dict[str, Any]] = {}
            path, headers = "/api/v1/feed", {"If-Modified-Since": since}
            while path:
                if deadline is not None and time.monotonic() > deadline:
                    raise TimeoutError("feed paging stopped by the time budget")
                body, _ = self._call(path, headers)
                headers = None
                for item in body.get("items", []):
                    entry = item.get("_feed_entry") or {}
                    uuid = entry.get("uuid") or item.get("id")
                    if uuid:
                        latest[uuid] = entry
                path = body.get("next_url")
            self.ads = [e for e in latest.values() if e.get("status") == "ACTIVE" and e.get("businessName")]
            for i, ad in enumerate(self.ads):
                self._exact.setdefault(norm(ad["businessName"]), []).append(i)
                for t in tokens(ad["businessName"]):
                    self._by_token.setdefault(t, set()).add(i)
            self.retrieved_at = utc_now()
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {str(exc)[:120]}"
        finally:
            self.ready.set()

    def candidates(self, names: list[str]) -> list[dict[str, Any]]:
        """Ads whose employer name equals one of the names, or shares its distinctive words (all of the shorter side's, at least one)."""
        picked: dict[int, None] = {}
        for name in names:
            for i in self._exact.get(norm(name), []):
                picked[i] = None
        for name in names:
            mine = tokens(name)
            if not mine:
                continue
            pool: set[int] = set()
            for t in mine:
                pool |= self._by_token.get(t, set())
            for i in pool:
                theirs = tokens(self.ads[i]["businessName"])
                shared = mine & theirs
                if shared and (shared == theirs or shared == mine or len(shared) >= 2):
                    picked[i] = None
        return [self.ads[i] for i in picked]

    def detail(self, uuid: str) -> tuple[dict[str, Any] | None, str | None, str | None]:
        """(ad_content, content sha256, retrieved_at) for one ad, cached; (None, None, None) when unreadable."""
        with self._lock:
            if uuid in self._details:
                return self._details[uuid]
        try:
            body, raw = self._call(f"/api/v1/feedentry/{uuid}")
            result = (body, hashlib.sha256(raw).hexdigest(), utc_now())
        except Exception:  # noqa: BLE001  one unreadable ad is skipped, never guessed
            result = (None, None, None)
        with self._lock:
            self._details[uuid] = result
        return result

    def parent_of(self, org: str) -> str | None:
        """Brreg parent (overordnetEnhet) of a sub-unit number; cached, one request per unknown number."""
        with self._lock:
            if org in self._parents:
                return self._parents[org]
        parent = None
        try:
            self._count()
            body, _ = _get(BRREG_SUBUNIT.format(org=org), {}, self.timeout)
            parent = str(body.get("overordnetEnhet") or "") or None
        except Exception:  # noqa: BLE001  unknown parent = no match
            parent = None
        with self._lock:
            self._parents[org] = parent
        return parent


def _subunits(profile: dict[str, Any]) -> tuple[set[str], list[str], bool]:
    """Own sub-unit numbers and names from the Brreg locations record, and whether that list is complete."""
    rec = (profile.get("evidence") or {}).get("locations") or {}
    rows = ((rec.get("value") or {}).get("locations") or []) if rec.get("status") == "available" else []
    numbers = {str(r.get("organisation_number")) for r in rows if r.get("organisation_number")}
    names = [r.get("name") for r in rows if r.get("name")]
    complete = rec.get("status") in {"available", "not_found"} and len(rows) < 1000
    return numbers, names, complete


def _active(ad: dict[str, Any], now: datetime) -> bool:
    expires = ad.get("expires")
    if not expires:
        return True
    try:
        return datetime.fromisoformat(str(expires)) >= now
    except ValueError:
        return True


def fetch_jobs(profile: dict[str, Any], feed: JobFeed) -> dict[str, Any]:
    """Evidence record with this company's open NAV ads. Requests are counted once per run on the shared feed."""
    org = str(profile["organisation_number"])
    feed.ready.wait()
    if feed.error:
        return evidence("nav_jobs", "source_error", "official_job_feed", FEED + "/api/v1/feed", note=f"NAV job feed unavailable: {feed.error}")
    own, sub_names, complete = _subunits(profile)
    names = [n for n in [profile.get("name"), *sub_names] if n]
    now = datetime.now(timezone.utc)
    jobs: list[dict[str, Any]] = []
    for ad in feed.candidates(names)[:MAX_DETAILS_PER_COMPANY]:
        content, sha, retrieved = feed.detail(ad["uuid"])
        if not content:
            continue
        body = content.get("ad_content") or {}
        employer = body.get("employer") or {}
        emp_org = re.sub(r"\D", "", str(employer.get("orgnr") or ""))
        if not emp_org or not body.get("title") or not _active(body, now):
            continue
        if emp_org == org or emp_org in own:
            link = "own" if emp_org == org else "sub-unit"
        elif not complete and feed.parent_of(emp_org) == org:
            link = "sub-unit"
        else:
            continue  # another employer with a similar name
        uuid = body.get("uuid") or ad["uuid"]
        city = ", ".join(sorted({w.get("city") for w in body.get("workLocations") or [] if w.get("city")}))
        jobs.append({
            "title": body["title"].strip(),
            "url": body.get("link") or PUBLIC_AD.format(uuid=uuid),
            "published": body.get("published"),
            "expires": body.get("expires"),
            "employer_name": employer.get("name"),
            "employer_organisation_number": emp_org,
            "employer_link": link,
            "work_location": city or None,
            "source": "NAV arbeidsplassen (pam-stilling-feed)",
            "source_record": f"{FEED}/api/v1/feedentry/{uuid}",
            "content_sha256": sha,
            "retrieved_at": retrieved,
            "span": (f"NAV feedentry {uuid}: employer.orgnr={emp_org}, employer.name={employer.get('name')}, "
                     f"title={body['title'].strip()}, published={body.get('published')}, expires={body.get('expires')}")[:240],
        })
        if len(jobs) >= MAX_JOBS_PER_COMPANY:
            break
    jobs.sort(key=lambda j: str(j.get("published") or ""), reverse=True)
    if not jobs:
        return evidence("nav_jobs", "not_found", "official_job_feed", FEED + "/api/v1/feed", retrieved_at=feed.retrieved_at,
                        note=f"No active NAV ad names organisation number {org} or one of its sub-units.")
    value = {"job_postings": jobs, "ads_checked": len(feed.ads)}
    return evidence("nav_jobs", "available", "official_job_feed", FEED + "/api/v1/feed", value=value, retrieved_at=feed.retrieved_at,
                    content_sha256=hashlib.sha256(json.dumps(jobs, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest(),
                    note=f"{len(jobs)} active NAV ad(s) naming {org} or its sub-unit as employer")
