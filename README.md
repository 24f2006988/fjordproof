# FjordProof — Signalpost entry

Exact-entity, evidence-backed Norwegian company profiles, built on the Builderr starter kit (see "Signalpost extension" and "Submission facts" below).

---

# Original starter README: Signalpost reference agent

This is a runnable starting point for the Signalpost company-research challenge. It is intentionally a solid baseline, not a winning submission.

The public universe contains 411,160 eligible companies. Run the starter on 100 companies before submitting. Larger local tests, including 1,000 or more companies, are encouraged but their precomputed profiles are not submitted or scored.

## What it already does

- reads a batch of Norwegian organisation numbers;
- anchors identity in the Brønnøysund bulk registry;
- fetches official financials, roles, group links and registered workplaces;
- visits the registry-listed website and rejects weak entity matches;
- emits one terminal JSONL envelope per input;
- records sources, retrieval times, content hashes, request counts and latency;
- supports checkpoint/resume and a deterministic refresh replay;
- includes examples for external-footprint discovery and an evidence-bounded research agent.

## First run: try one saved example

Requires Python 3.12+. Open a terminal inside this extracted folder.

Before downloading company data or running a full crawl, try the bundled public
sample. It uses saved responses: no API key, registry download or live web requests.

```bash
python3 scripts/run_refresh_replay.py \
  --manifest tests/fixtures/refresh-snapshots.json \
  --output out/refresh-demo.json
```

Open `out/refresh-demo.json`. The `events` list shows what changed between two
versions of one company profile and the source evidence for each change. The sample
should find two expected changes, no false changes, and no extra changes when the
same data is checked again.

The report's `qualification_passed` field refers only to this public sample check.
It does not qualify an entry for the competition or prove live information coverage.
The printed request counts are reads from saved responses, not network calls.

## Next: research live companies

Requires Python 3.12+ and `uv`. This step downloads data and makes live requests.
The manifest selector can create a local test batch of any size. Use 100 rows for the recommended smoke test before trying a larger batch.

```bash
uv sync
curl -L 'https://data.brreg.no/enhetsregisteret/api/enheter/lastned/csv' -o brreg-enheter.csv
curl -L 'https://builderr.ai/signalpost-company-universe-2025.jsonl.gz' -o signalpost-universe.jsonl.gz

uv run python select_entry_batch.py \
  --universe signalpost-universe.jsonl.gz \
  --count 100 \
  --output entry-companies.jsonl

# Use the 100-company batch as your smoke test.
cp entry-companies.jsonl smoke-companies.jsonl

uv run python scripts/run_competition_batch.py \
  --organisations smoke-companies.jsonl \
  --bulk brreg-enheter.csv \
  --profiles-output out/smoke-profiles.jsonl \
  --output out/smoke-envelopes.jsonl \
  --report out/smoke-report.json \
  --run-id smoke-001 \
  --expected-count 100

# You may test at larger scale locally, but Builderr supplies the official batch for scoring.
uv run python scripts/run_competition_batch.py \
  --organisations entry-companies.jsonl \
  --bulk brreg-enheter.csv \
  --profiles-output out/profiles.jsonl \
  --output out/envelopes.jsonl \
  --report out/run-report.json \
  --run-id local-001 \
  --expected-count 1000

uv run --with pytest pytest -q
```

The published archive was clean-room verified on August 24, 2026: 104 tests and 5 subtests passed, followed by a one-company live BRREG smoke run with one terminal envelope, five requests and zero silent drops.

Increase `--count` and `--expected-count` together for a larger local test. The 100-row smoke test above is practice only; Builderr supplies the companies for every official run.

## The improvement loop

1. Treat the organisation number as the anchor.
2. Generate site/profile candidates from official data, the company site, lawful search providers and named people.
3. Save every candidate and the evidence for or against it.
4. Publish only exact-entity matches. Parent, brand, franchise and similarly named companies are not exact.
5. Crawl static HTML first. Escalate to a browser only when a deterministic completeness check fails.
6. Measure added supported coverage, wrong-company claims, runtime, requests and cost.
7. Promote a strategy only when it improves coverage without weakening the accuracy gates.
8. Freeze strategies and thresholds before the daily evaluation run.

The strongest differentiator is external evidence that remains exact and auditable: official company pages, company-owned profiles, jobs, dated activity, ratings/reviews and permitted public signals. Do not trade accuracy for volume.

## Important source rule

Open-source code does not grant permission to scrape a platform. Follow each source's terms, robots policy, rate limits and licence. LinkedIn, Meta and Indeed are useful identity/discovery targets, but direct automated collection may be restricted. Use permitted APIs, licensed providers, company-owned outbound links, or return `blocked`/`not_available`.

Read `docs/competition-control-loop.md`, `docs/external-connectors.md` and the public source policy before adding connectors.

## Submission contract

Submit a repository with:

- a 100-company smoke-test result or report;
- one documented command that accepts a JSONL batch of organisation numbers;
- exactly one terminal envelope per input;
- pinned dependencies and reproducible setup;
- a previous-snapshot input and material-change output;
- a machine-readable run report with runtime, request count and third-party cost;
- declared models, APIs, licences and source-rights assumptions.

Email the repository URL, run command, models/APIs and expected cost per 100-company run to `submit@builderr.ai`.

## Signalpost extension (this entry)

One command (Python 3.12, `pip install -e .` deps from pyproject, no API keys, USD 0):

    python scripts/run_signalpost.py --organisations batch.jsonl --bulk brreg-enheter.csv --run-id r1 --expected-count 100 --outdir out
    # refresh: add --previous out/envelopes.jsonl  (prior snapshot kept; material changes appear in envelope["changes"])

Adds to the starter: (1) free website discovery for companies with no registry homepage, from the registry e-mail
domain or name-slug guesses, published only with hard proof of the exact entity (organisation number, or registered
street+postcode printed on the company's own pages); (2) `external_footprint` module from verified sites only:
careers links, news pages, dated feed items, contact and company-owned social links.

### Output format
Each envelope keeps the starter's native fields (`state`, `modules`, `profile`) and also carries the `OUTPUT_CONTRACT.md` fields
(`run`, `claims`, `evidence`, `changes`, `errors`, `operations`), added by `src/norway_company_agent/contract.py`.
Every `available` claim cites `evidence_ids`; missing data is `not_available`/`blocked`/`failed`, never zero-filled.

- `news_items` claim: article-level company news from the verified company-owned site only. Each item has `title`, direct `url`,
  `published_date` and `date_kind` (where the date came from: the site's RSS/Atom feed, WordPress REST post date, JSON-LD
  `datePublished`, `article:published_time`, or an `<article><time datetime>`). A sitemap `<lastmod>` is never used as a
  publication date. Each item's evidence record points at the page or feed that contains it, with its SHA-256 and the literal span.
- `synthesis` (top-level, per envelope, built by `src/norway_company_agent/synthesis.py`): `headline`, `statements` (identity, activity,
  size, latest accounts and year-on-year trend, leadership, group, website, news, hiring), `signals` (e.g. `loss_making`,
  `negative_equity`, `revenue_growth`, `revenue_decline`, `recent_news`, `hiring`), `unknowns` (field, state and the reason it is
  unknown) and `changes` (summary of material changes since the previous run). Every statement and signal cites the `evidence_ids`
  of the claims it is built from. It is generated deterministically from the claims; no model is used and nothing is inferred
  beyond the claims.

### Submission facts
- Run command (`--bulk` is optional; without it each company is looked up live in Brreg, about 1 extra request each): `uv sync && uv run python scripts/run_signalpost.py --organisations <batch.jsonl> [--bulk brreg-enheter.csv] --run-id <id> --expected-count <n> --outdir out`
- Models/APIs: none (no LLM, no paid API). Sources: Brønnøysundregistrene open data (NLOD 2.0) and company-owned websites (robots.txt respected, public-URL guard).
- Optional model step, off by default (no key = no request, no cost, envelopes unchanged): with `GEMINI_API_KEY` set (Builderr-supplied key;
  model `gemini-2.5-flash-lite`, override with `GEMINI_MODEL`; `SIGNALPOST_LLM=off` disables it), the visible text of each identity-verified
  company website is sent to the Gemini API, which picks up to three sentences describing what the company does. A sentence is published
  (claim `company_description`) only if it occurs verbatim on that page; the model never supplies numbers, names or dates. One request per
  verified site (~15 per 100 companies), about 2,000 input tokens each: roughly USD 0.003 per 100 companies, reported in `third_party_cost_usd`.
- Registry claims also include every former name (`former_name`, from `historiskeNavn`, with the dates each was held) and
  secondary industry codes (`secondary_industry`, `naeringskode2/3`), from the record already fetched (no extra request).
- From identity-verified company websites only: every email on the site's own domain (or the registry's own address) and every
  phone number (`contact_email`, `contact_phone`, one claim each with its page and literal span; web-agency credits skipped;
  the contact page is always read, one request per verified site); social profiles whose handle spells the legal name or the
  verified site's own domain name; and `company_description`, the site's own words (a homepage paragraph naming the company,
  else its schema.org or meta description; with the optional Gemini step, model-selected sentences checked verbatim).
- Every available claim carries `raw_value` (the value exactly as the source holds it) and `source_path` (where it sits in the source,
  e.g. `regnskap[<record id>].resultatregnskapResultat.driftsresultat.driftsresultat` or `enheter[<org>].naeringskode1`).
- Expected third-party cost: USD 0. Measured (smoke-023): 1,745 requests and ~8.4 min per 100 companies (limit per 100-company shard: 2,000 requests / 45 min).
- 100-company smoke test: `smoke/smoke-envelopes.jsonl`, `smoke/smoke-report.json`, `smoke/site/index.html` (run smoke-023, contract format, 100/100 envelopes, validation passed, 141 tests pass).

## Profile viewer (UX)
Every run also writes `<outdir>/site/index.html`: one static page (no server, no network) built from the envelopes by `scripts/build_site.py`.
It has a searchable, filterable company list, a coverage summary for the batch, side-by-side comparison of up to four companies, and one
profile per company (`#/<organisation number>`) with a plain-language summary, what is unknown and why, what changed since the previous run,
annual accounts, roles, sub-units, group structure, dated public activity, and a table of every fact with its source link, retrieval date and
SHA-256. Works on desktop and phone and follows the system light/dark setting. The page for the committed smoke test is `smoke/site/index.html`.
Rebuild by hand: `python scripts/build_site.py --envelopes out/envelopes.jsonl --output out/site/index.html`.
