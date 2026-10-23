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

One command (Python 3.12; `uv sync` installs the pinned dependencies; no API key needed, USD 0 without keys):

    uv sync && uv run python scripts/run_signalpost.py --organisations batch.jsonl --run-id r1 --expected-count 100 --outdir out
    # optional: --bulk brreg-enheter.csv (otherwise each company is looked up live, ~1 extra request each)
    # refresh: add --previous out/envelopes.jsonl  (prior snapshot kept; material changes appear in envelope["changes"])

Adds to the starter (every external fact needs exact-entity proof; anything unproven is not published):
1. Website discovery for companies without a registry homepage: registry e-mail domain, name-slug guesses, NAV employer homepages and a
   declared Common Crawl candidate index (below). A site is published only with proof of the exact entity: the organisation number printed on
   its own pages, the registered street address and postcode, or the unique-name rule (the domain is the whole distinctive legal name, the page
   names the company as a company, no other organisation number is printed, the page has real content, and a Brreg name search finds no other
   entity carrying that name). Proof pages are read terms/legal first (Shopify shops: their policy pages). Name guesses with other endings
   (.io .se .dk .net .as .eu .co) and crawl-index candidates are published only on the organisation number printed live.
2. The homepage the company declares in the register is always published as `registry_website` (a registry fact). It stands in as
   `official_website` (method `registry_declared`, confidence 0.6) only when our check could not reach an existing site (timeout, 403, robots),
   never for a domain that does not resolve and never for housing co-operatives or condominiums (they list their manager's site).
3. From identity-verified sites only: contact emails and phones, social profiles, careers pages, structured job ads, dated news items and the
   site's own description, each with its page URL, SHA-256 and literal span.
4. NAV job ads (official public job feed, `pam-stilling-feed.nav.no`) whose employer number is the company or one of its registered sub-units.
5. Man-years (`workforce_fte`) read with OCR from the company's own filed annual-account copy (Brreg, scanned PDF): only the generated
   NOTEOPPLYSNINGER page, only when its header shows the organisation number, and only a confidently read two-decimal value.
6. Every fetched financial year and every registry field we fetch (activity text, statutory purpose, register dates, former names, secondary
   industries, ...) as separate claims, plus `external_observations` in the starter kit's observation schema.
7. Dated public activity (`public_activity`, one `public_activity_item` claim per item) from official sources keyed by the organisation number,
   never by name: EU TED contract award notices that list the company as a winner registered in Norway (TED API v3, one search per 50
   companies), and the Research Council of Norway's list of approved SkatteFUNN R&D projects (one file per run). Each item has its own date
   (TED publication date / SkatteFUNN decision date), title, direct URL and the literal row. Off: `SIGNALPOST_PUBLIC_ACTIVITY=off`.

### Output format
Each envelope keeps the starter's native fields (`state`, `modules`, `profile`) and also carries the `OUTPUT_CONTRACT.md` fields
(`run`, `claims`, `evidence`, `changes`, `errors`, `operations`), added by `src/norway_company_agent/contract.py`.
Every `available` claim cites `evidence_ids` and carries `raw_value` (the value as the source holds it) and `source_path` (where it sits,
e.g. `regnskap[<record id>].resultatregnskapResultat.driftsresultat.driftsresultat`). Missing data is `not_available` / `blocked` / `failed` /
`ambiguous`, never zero-filled.

- `synthesis` (built by `src/norway_company_agent/synthesis.py`, deterministic, no model): `headline`, `conclusion`, seven cited `sections`
  (Company brief, Latest financials, Who runs it?, Working here, Recent activity, What changed, What is unknown), `statements`, `signals` and
  `unknowns` (each with its reason and the source that was checked). "What changed" reports changes since the previous run (or that this is the
  first observation) and the changes visible inside the sources (year on year, role changes); every sentence cites the evidence it rests on.
- `news_items`: article-level news from the verified site only (`title`, `url`, `published_date`, `date_kind`). Feed, WordPress and sitemap
  entries are only candidates: an item is published when its own page is an article (NewsArticle/BlogPosting/PressRelease or
  `og:type=article`) with an explicit publication date (`datePublished` / `article:published_time`), at least 100 words of article text,
  no product/event/offer/listing markup, and text that names the company or speaks for it. A feed's `updated`, a sitemap `<lastmod>` or a
  bare `<time>` element is never used as a publication date.

### Submission facts
- Run command: `uv sync && uv run python scripts/run_signalpost.py --organisations <batch.jsonl> --run-id <id> --expected-count <n> --outdir out`
- Without keys (the default): no model, no paid API, USD 0. Sources: Brønnøysundregistrene open data (NLOD 2.0: Enhetsregisteret, roles,
  Regnskapsregisteret, annual-account copies), NAV's public job feed, company-owned websites (robots.txt respected, public-URL guard),
  EU TED notices (api.ted.europa.eu, free public API; EU open-data reuse, Commission Decision 2011/833/EU) and the Research Council of Norway's
  published SkatteFUNN project list (forskningsradet.no; public-sector information, reuse assumed under NLOD 2.0).
- Declared cache: `data/discovery_index.json.gz` (224 KB, built by `scripts/build_discovery_index.py` from Common Crawl CC-MAIN-2026-39 home,
  contact, about and legal pages of .no domains). It holds candidate (domain, organisation number) pairs only, no facts; a site from it is
  verified live like any other candidate. Off: `SIGNALPOST_DISCOVERY_CACHE=off`.
- Off switch for the unique-name website rule: `SIGNALPOST_NAME_DOMAIN_PROOF=off`.
- Libraries (pinned in `uv.lock`): rapidocr-onnxruntime (Apache-2.0), onnxruntime (MIT), pypdf (BSD-3-Clause), Pillow (MIT-CMU),
  opencv-python-headless (Apache-2.0), beautifulsoup4 (MIT), lxml (BSD-3-Clause), trafilatura (Apache-2.0), extruct (BSD-3-Clause),
  tldextract (BSD-3-Clause), pydantic (MIT).
- OCR uses half the CPU cores as workers (`--ocr-workers`), each with its own 2-thread engine; annual-account copies download during the
  website stage. A built-in budget guard stops new work before 40 minutes and 2,000 requests per run and still emits every envelope.
- Measured (smoke-100 `rel-smoke`, no keys, 12-core laptop): 100/100 envelopes, validation passed, 1,624 requests, 18 minutes, 0 uncited
  claims, 12 verified websites, man-years for 71 companies. A second set of 100 other companies (no keys): 1,604 requests, 20 minutes, valid. Limit per 100-company shard: 2,000 requests / 45 minutes / USD 10.
- 100-company smoke test: `smoke/smoke-envelopes.jsonl`, `smoke/smoke-report.json`, `smoke/site/index.html` (run `rel-smoke`, no keys).

### Optional keys (all off by default; with no key: no request, no cost, identical output)
If Builderr supplies a key for scoring, these steps switch on with no code change; each declares its cost per company in
`operations.third_party_cost_usd` and in the run report.
- `GEMINI_API_KEY`:
  (a) website suggestions: Gemini (`GEMINI_DISCOVERY_MODEL`, default `gemini-3.5-flash-lite`) is asked for the company's own domain from its
  public registry name, number, address and industry, with Google Search grounding when the key allows it (`SIGNALPOST_GEMINI_SEARCH=off`
  disables search). Suggestions are only candidates: each must pass the same proof as any guessed domain. Off: `SIGNALPOST_GEMINI_DISCOVERY=off`;
  `SIGNALPOST_GEMINI_MIN_INTERVAL=<seconds>` spaces calls for rate-limited keys. Declared cost: tokens plus USD 0.014 per Google Search query
  (estimate).
  (b) description: the visible text of each verified site is sent to Gemini (`GEMINI_MODEL`, default `gemini-3.5-flash-lite`), which picks up
  to three sentences; a sentence is published only if it occurs verbatim on that page (~USD 0.003 per 100 companies). Off: `SIGNALPOST_LLM=off`.
- `GOOGLE_PLACES_API_KEY` (or `GOOGLE_SEARCH_API_KEY` + `GOOGLE_CSE_ID`), `BRAVE_SEARCH_API_KEY`: website candidates from Places / Custom Search /
  Brave, same proof rule. Declared cost per call (estimates; override with `SIGNALPOST_SEARCH_COST_PER_CALL`): Places 0.035, Custom Search 0.005,
  Brave 0.005 USD. Measured: a Places key added no verified site on a random 100-company sample.

## Profile viewer (UX)
Every run also writes `<outdir>/site/index.html`: one static page (no server, no network) built from the envelopes by `scripts/build_site.py`.
It has a searchable, filterable company list, a coverage summary for the batch, side-by-side comparison of up to four companies (shareable
link `#/compare/<org>,<org>`, CSV export), and one profile per company (`#/<organisation number>`) that opens with the seven cited synthesis
sections, then a plain-language summary, unknowns with their reasons and checked sources, field-by-field changes since the previous run,
annual accounts, roles, sub-units, group structure, dated public activity, and a table of every fact with its source link, retrieval date and
SHA-256. Works on desktop and phone and follows the system light/dark setting. The page for the committed smoke test is `smoke/site/index.html`.
Rebuild by hand: `python scripts/build_site.py --envelopes out/envelopes.jsonl --output out/site/index.html`.
