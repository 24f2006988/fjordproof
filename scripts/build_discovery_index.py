"""Build the declared website discovery index from a Common Crawl organisation-number scan.

Offline, run once per crawl. Input: the scan output (one JSON line per crawled page:
{dom, url, kind, title, orgs: {orgnr: {labelled, ctx}}}) and the public company universe file.
Output: data/discovery_index.json.gz = {orgnr: [[domain, page_url], ...]}.

The index only names CANDIDATE hosts. At run time every candidate must print the exact organisation number
live (footprint_plus.discover_website); nothing from the crawl is ever published as evidence.

Usage: python scripts/build_discovery_index.py <scan.jsonl> <universe.jsonl> [out.json.gz] [--crawl CC-MAIN-2026-39]
"""
from __future__ import annotations

import argparse
import collections
import datetime as dt
import gzip
import json
import re
import unicodedata
from pathlib import Path

WEIGHTS = (3, 2, 7, 6, 5, 4, 3, 2)
MAX_ORGS_PER_DOMAIN = 2  # more labelled universe org numbers on one domain = directory, agency or manager site
MAX_DOMAINS_PER_ORG = 3  # one org number on many domains = web agency or host footer credit
CREDIT_RE = re.compile(
    r"levert av|utviklet av|laget av|design(?:et)? av|webdesign|nettside av|nettsted av|powered by|produsert av|"
    r"drift av|driftet av|hostet av|webbyr|reklamebyr|developed by|made by|built by",
    re.I,
)


def checksum_ok(orgnr: str) -> bool:
    if not re.fullmatch(r"\d{9}", orgnr):
        return False
    rest = 11 - sum(int(d) * w for d, w in zip(orgnr[:8], WEIGHTS)) % 11
    rest = 0 if rest == 11 else rest
    return rest != 10 and rest == int(orgnr[8])


GENERIC_NAME_TOKENS = {"holding", "invest", "eiendom", "eiendommer", "gruppen", "group", "norge", "norway", "service",
                       "services", "partner", "consulting", "solutions", "stiftelsen", "sameiet", "borettslag"}


def _fold(text: str) -> str:
    text = text.casefold().translate(str.maketrans({"ø": "o", "å": "a", "æ": "ae"}))
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()


def name_on_site(name: str, domain: str, titles: list[str]) -> bool:
    """True if a distinctive token (4+ letters) of the legal name appears in the domain or a page title."""
    tokens = [t for t in re.findall(r"[a-z0-9]+", _fold(name)) if len(t) >= 4 and t not in GENERIC_NAME_TOKENS]
    haystack = _fold(domain) + " " + " ".join(_fold(t) for t in titles)
    return any(t in haystack for t in tokens)


def build(scan_path: Path, universe_path: Path) -> tuple[dict[str, list[list[str]]], dict[str, int]]:
    universe: dict[str, str] = {}
    with universe_path.open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                row = json.loads(line)
                universe[row["organisation_number"]] = row.get("name") or ""

    stats: collections.Counter[str] = collections.Counter()
    # domain -> orgnr -> first page URL where the number is printed under an org-number label
    labelled: dict[str, dict[str, str]] = collections.defaultdict(dict)
    titles: dict[str, list[str]] = collections.defaultdict(list)
    with scan_path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                page = json.loads(line)
            except ValueError:
                stats["broken_lines"] += 1
                continue
            stats["pages"] += 1
            if page.get("err"):
                stats["page_errors"] += 1
                continue
            if page.get("title"):
                titles[page["dom"]].append(page["title"])
            for orgnr, hit in (page.get("orgs") or {}).items():
                if not hit.get("labelled") or not checksum_ok(orgnr) or orgnr not in universe:
                    continue
                if CREDIT_RE.search(hit.get("ctx") or ""):
                    stats["dropped_credit_context"] += 1
                    continue
                labelled[page["dom"]].setdefault(orgnr, page["url"])

    org_domains: dict[str, list[tuple[str, str]]] = collections.defaultdict(list)
    for dom, orgs in labelled.items():
        if len(orgs) > MAX_ORGS_PER_DOMAIN:
            stats["dropped_multi_org_domains"] += 1
            continue
        for orgnr, url in orgs.items():
            # Two org numbers on one site: the second is often a web agency or data processor in the footer or
            # privacy policy, so each must also carry its name in the domain or a page title.
            if len(orgs) > 1 and not name_on_site(universe[orgnr], dom, titles[dom]):
                stats["dropped_second_org_without_name"] += 1
                continue
            org_domains[orgnr].append((dom, url))

    pairs: dict[str, list[list[str]]] = {}
    for orgnr, doms in sorted(org_domains.items()):
        if len(doms) > MAX_DOMAINS_PER_ORG:
            stats["dropped_multi_domain_orgs"] += 1
            continue
        pairs[orgnr] = [[d, u] for d, u in sorted(doms)]
    stats["domains_with_labelled_universe_org"] = len(labelled)
    stats["orgs"] = len(pairs)
    stats["pairs"] = sum(len(v) for v in pairs.values())
    return pairs, dict(stats)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("scan", type=Path)
    ap.add_argument("universe", type=Path)
    ap.add_argument("out", type=Path, nargs="?", default=Path("data/discovery_index.json.gz"))
    ap.add_argument("--crawl", default="CC-MAIN-2026-39")
    args = ap.parse_args()
    pairs, stats = build(args.scan, args.universe)
    doc = {
        "source": f"Common Crawl {args.crawl} (home, contact, about and legal pages of .no domains)",
        "licence": "Common Crawl terms of use (https://commoncrawl.org/terms-of-use)",
        "built_at": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat(),
        "build_script": "scripts/build_discovery_index.py",
        "use": "candidate hosts only; every website is verified live by the exact organisation number before use",
        "rules": {
            "labelled_org_number": True,
            "in_company_universe": True,
            "max_orgs_per_domain": MAX_ORGS_PER_DOMAIN,
            "max_domains_per_org": MAX_DOMAINS_PER_ORG,
            "credit_context_dropped": CREDIT_RE.pattern,
        },
        "stats": stats,
        "pairs": pairs,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    # mtime=0 keeps the gzip bytes identical for identical input
    with gzip.GzipFile(args.out, "wb", mtime=0) as fh:
        fh.write(json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    print(json.dumps(stats, indent=1))


if __name__ == "__main__":
    main()
