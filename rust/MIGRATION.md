# Rust Migration — products-scraper

Status: **v1 milestone complete** — the telefon fiyat/teklif refresh pipeline's
generic-path fetch, parse, refresh orchestration and ingestion client run in
Rust with contract parity against the Python pipeline.

## Crate map

| Crate | Port of | Tests |
|---|---|---|
| `psr-core` | `retailer_offer_refresh` URL helpers (`_safe_url`, `_variant_query`, `_same_resource`, `public_source`, `pazarama_product_url`), Python-compatible `urlparse`/`parse_qsl`/`urljoin` (`purl.rs`), `offer_target_selection.select_offer_targets` | 21 |
| `psr-ingestion` | `offer_ingestion_client.OfferIngestionClient`: retry/backoff, committed+scores idempotent replay, 500-batch split, `require_all_accepted` atomicity, variant-rejection re-basing, `get_published_products`, `ingest_listing_offers`, `reconcile_scores` | 20 (wiremock) |
| `psr-parse` | `_product_offer` (JSON-LD walk), `_challenge_page`, `_is_conditional_price`, `schema_nodes` (2000-node cap), Python `str(float)` number formatting | 14 |
| `psr-fetch` | `_fetch_target` generic walk (lines 832-929), `HostCircuits` (`_blocked_hosts`/`_transport_failures`/`_transient_blocked_until`), `PublicRetailerSources` robots gate + `pazarama` PDP gate with CPython 3.15 `RobotFileParser` parity | 12 + 15 (wiremock) |
| `psr-refresh` | `refresh_existing_offer_targets`: selection, merchant buckets (4 parallel, sequential inside), `target_budget_skipped` | 2 (wiremock) |
| `psr-cli` | `psr` binary: `env-check`, `targets`, `probe`, `refresh`, `catalog-fetch`, `ingest` | — |

Run everything: `cargo test --workspace`. Release binary: `target/release/psr`.

## What runs in Rust today (v1)

- Generic-path hosts: `idefix.com`, `vatanbilgisayar.com` (plain GET — see
  deferred Vatan gate), `pttavm.com`, `amazon.com.tr`, `samsung.com`,
  `mi.com` + gate-routed `pt.com.tr`/`troyestore.com` (public robots gate)
  and `pazarama.com` (36 PDP/2 MB gate).
- The full fetch contract: manual bounded redirects (4 hops) with variant
  preservation, `unexpected_final_destination` checks, 5 MB body cap,
  challenge detection → permanent host block, 403/429 → `access_denied`/
  `rate_limited` → permanent block, 5xx → `retailer_unavailable` + transient
  circuit (2 failures → 5 min cooldown), per-URL cache and diagnostics with
  the exact Python reason strings.
- Ingestion over HTTPS to the existing Next.js API
  (`TEKNOSKOR_INGESTION_URL` + `SCRAPER_INGESTION_SECRET`), with the same
  fail-closed semantics as the Python client.

## Deferred (fail-closed), by phase

### Phase 2 — price-refresh orchestration + identity
- `TRPriceScraper.is_strict_match` (tr_price_scraper.py 571-709) and
  `has_observed_offer_evidence` (746-840). `psr-refresh::identity` defines
  the trait; the CLI refuses to ingest unverified observations, so the
  pipeline cannot ship a wrong variant/price until this lands.
- `update_prices.PriceUpdater` orchestration (INGESTION_PRODUCT_BATCH_SIZE
  = 10, MAX_SOURCE_OBSERVATION_AGE_SECONDS = 3h, dry-run phases,
  `live_offer_is_valid`, collision proofs).

### Phase 3 — dedicated source readers
- hepsiburada/trendyol/n11/teknosa/mediamarkt/apple/turktelekom/pasaj
  catalog readers — the engine answers `dedicated_reader_pending` for their
  hosts (never a generic crawl of an anti-bot storefront).
- Reviewed Vatan gate (`VatanReviewedSource.is_reviewed/prepare/browser_get`):
  v1 does plain GETs; `reviewed_url_redirect` and the 403 browser fallback
  are unimplemented.
- `_vatan_selected_variant`, `_pazarama_visible_cash_price`,
  `_public_retailer_product` (public SKU proofs): fail closed in psr-parse.
- `catalog_crawl` SQLite queue.

### Phase 4 — catalog crawl
- `PublicRetailerSources.discover` (root → family → SKU, max 6 colors),
  Pazarama category walker, sitemap audits.

### Phase 5 — stealth stores
- Stores requiring Playwright/patchright stay on Python. Decision pending;
  if they must move, keep a Python browser sidecar called from Rust.

## Known divergences (accepted)

1. **Body decoding** — Python `requests` uses charset detection
   (chardet/charset_normalizer); reqwest uses the Content-Type charset with
   UTF-8 fallback. For the targeted retailer pages both agree; non-UTF-8
   pages without a charset header may differ.
2. **Timeout tuple** — Python `timeout=(5, 15)` (connect, read); reqwest
   models `connect_timeout(5s)` + total `timeout(20s)`. A slow-drip response
   times out earlier in Rust than in Python.
3. **Gate serialization scope** — Python holds one RLock across all public
   gate fetches; Rust serializes per host. Same-host pacing is identical;
   different hosts no longer wait on each other.
4. **robots.txt percent-normalization** — CPython `normalize_pattern`
   percent-normalizes rule paths; skipped (identity for the ASCII paths the
   scraper targets). All other RobotFileParser semantics are ported exactly
   from CPython 3.15: group merging, longest-match allowance,
   Allow-wins-ties, `match`-anchored wildcard translation, `$` fullmatch,
   implicit `/robots.txt` allow.
5. **Float repr edges** — `py_number_string` covers `str(float)` shapes the
   JSON-LD pipeline meets; exotic values beyond 1e15 magnitudes can differ.
6. **User-Agent** — the Python generic walk sends `python-requests/*`;
   the Rust generic walk sends no UA (reqwest default). Retailers that gate
   on UA treat both as non-browsers; the gate-routed sources send the
   identical `TeknoskorPriceBot/1.0 (+https://teknoskor.com)`.
7. **Success-path parse count** — one HTML parse serves challenge detection
   and offer extraction (`product_offer_document`); the parse happens after
   the circuit-reset await so the parsed document never spans an await point.
   (Observable host state is identical to Python's ordering because a
   challenge still ends in a permanent block.)

## Parity test harness (plan)

- Keep `test_retailer_offer_refresh.py` as the oracle: run the Python suite
  against a recorded HTTP fixture set, replay the same fixtures through
  `psr probe --permissive`, diff the reason strings and observations.
- Phase 2 adds the identity parity harness: Python `is_strict_match` outputs
  recorded over the catalog title corpus, replayed against the Rust port
  before enabling ingestion from Rust.

## Rollout plan

1. Python pipeline stays production. Rust v1 runs read-only probes
   (`psr probe`, `psr refresh`) for parity sampling.
2. After phase-2 identity lands and parity harness is green, switch the
   generic-path hosts to Rust refresh + Rust ingestion (same Next.js API).
3. Decommission the Python generic-path code; keep Python for dedicated
   readers until phase 3/4 land.
