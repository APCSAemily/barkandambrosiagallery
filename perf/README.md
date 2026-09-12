# `perf/` — standalone page performance tooling

Self-contained. Does **not** modify the app. Everything it inserts runs inside a
transaction that is rolled back, so the dev database is left exactly as it was.

Two tools:

| File | Purpose | Run |
|---|---|---|
| `scaling_probe.py` | Measure render time + query count at growing dataset sizes, and estimate empirical time complexity (`time ~ n^b`) | `docker compose run --rm web pixi run python perf/scaling_probe.py` |
| `test_page_perf.py` | Regression guard: hard-fails on a query-count rise, reports (doesn't fail on) render time, at a fixed synthetic size | `docker compose run --rm web pixi run python manage.py test perf` |

## Safety: this only runs against a local dev database

Both tools write to whatever database `settings.DATABASES['default']` points
at. `scaling_probe.py` inserts up to tens of thousands of rows (undone by a
rolled-back transaction); `--cleanup` deletes rows **outside** any transaction.
Neither must ever run against a real database.

Every entry point (`scaling_probe.py`, and `test_page_perf.py`'s
`setUpTestData`) calls `_ensure_safe_to_run()` first, which refuses to
continue — printing a clear message and exiting cleanly, no traceback — unless
**both**:

- `settings.DEBUG` is `True`, and
- the configured DB host is `db`, `localhost`, `127.0.0.1`, or unset

`scaling_probe.py` has an escape hatch if you're certain the check is wrong for
your setup: `--i-know-this-is-not-prod`. There is deliberately no environment
variable for this — a flag has to be typed each time, so it can't get set once
and forgotten. `test_page_perf.py` has no escape hatch; there's never a
legitimate reason to run it against anything but a throwaway database.

This is also why the CI workflow (below) sets `DJANGO_DEBUG=True` and points
`POSTGRES_HOST` at `localhost` — that's what lets it pass the same check a
local run does.

## scaling_probe.py

```
docker compose run --rm web pixi run python perf/scaling_probe.py            # full
docker compose run --rm web pixi run python perf/scaling_probe.py --quick    # fast
docker compose run --rm web pixi run python perf/scaling_probe.py --only taxonomy
docker compose run --rm web pixi run python perf/scaling_probe.py --repeats 9
docker compose run --rm web pixi run python perf/scaling_probe.py --cleanup  # paranoia sweep
```

Output per target: a table of `dataset rows | status | ms cold | ms (median) |
ms (p95) | queries | db ms`, then a log-log fit giving the exponent `b`.
`ms cold` is the first (unwarmed) request and, for a cache-backed page, is the
cache-miss cost; `ms (median)` is the steady state a real user sees.

- `b ~ 1.0` → linear, cost grows with the dataset (needs pagination / caching)
- `b ~ 0.1` → flat, cost is fixed overhead (fine, or already page-limited)
- `b ~ 2.0` → quadratic, investigate

It also fits the **query count** vs size — a rising exponent there means an N+1.

### Targets

| key | URL | scales with |
|---|---|---|
| `taxonomy` | `/taxonomy/` | taxa |
| `image_browser` | `/beetles/?per_page=12` | specimens |
| `image_browser_large_page` | `/beetles/?per_page=100` | specimens |
| `annotate` | `/tools/annotate/` | specimens |
| `api_species` | `/api/v1/species/?page_size=10000` | taxa |
| `api_images` | `/api/v1/beetles/images-with-annotations/...` | specimens |

`image_browser` and `image_browser_large_page` are the same page at two page
sizes. If their query counts match, that cost (currently 47 — almost certainly
the filter-dropdown options, rebuilt from ~1 query per filter per request) is
fixed overhead, unrelated to how many images are on the page; if the larger
page size runs more queries, the cost is per-row instead. Different problem,
different fix.

**Taxa-scaling sizes are capped around 8,000**, not tens of thousands — the
project doesn't expect the real species count to exceed roughly that, ever, so
testing well past it wastes a run without telling us anything we'll see in
production. Specimen/image counts don't have that ceiling and can grow much
larger, hence the image-browser targets going up to 12,000+.

### Safety

All inserts happen inside one `transaction.atomic()` with
`transaction.set_rollback(True)` at the end. A hard kill mid-run is also safe:
Postgres discards the uncommitted transaction when the connection drops. Probe
rows are tagged (`Taxon.valid_species_id` starts `PERF-`,
`ImageAsset.full_path_at_import` starts `perf/`); `--cleanup` deletes any that
somehow survived — see the safety-check section above, it's gated the same way.

## test_page_perf.py

A `django.test.TestCase` (transaction rolled back). Seeds `SEED_TAXA` +
`SEED_SPECIMENS` rows, then for every URL in `BUDGET_MS` / `QUERY_CEILING`:

- **query count over `QUERY_CEILING`** → hard failure. Query counts are
  deterministic, so this is the reliable signal — it's what catches an N+1
  getting reintroduced.
- **render time over `BUDGET_MS`** → printed as a `WARN` in the test output,
  **not** a failure. Wall-clock timing is noisy, especially on a shared CI
  runner, and a flaky assertion just teaches everyone to ignore red CI.

There's also a dedicated cache test
(`test_taxonomy_cache_hit_is_fast_and_invalidates`) that asserts a repeat hit
runs fewer queries than the first (cache-miss) hit, and that
`invalidate_taxonomy_caches()` actually clears it.

Run either way — both build their own environment from `manage.py migrate`,
so a fresh clone works with no manual setup:

```
docker compose run --rm web pixi run python manage.py test perf
docker compose run --rm web pixi run python -m unittest perf.test_page_perf -v
```

`manage.py test` builds a real throwaway test database (needs migration
`0010_1_enable_pg_trgm`, which creates the `pg_trgm` extension migration
`0011`'s trigram indexes depend on). Plain `unittest` instead reuses whatever
dev database `docker compose up` already migrated.

### Adding a new page

1. Add a URL entry to **both** `BUDGET_MS` and `QUERY_CEILING` in
   `test_page_perf.py` — the test does `QUERY_CEILING[url]` for every key in
   `BUDGET_MS`, so a URL in one but not the other fails with a `KeyError`, not
   a useful assertion message.
2. If you also want it in the scaling probe, add an entry to `TARGETS` in
   `scaling_probe.py`: `url`, `scale` (`"taxa"` picks `seed_taxa` and measures
   against the Taxon count; `"specimens"` picks `seed_specimens`/`seed_taxa`
   together and measures against the Beetles count), and `sizes` /
   `sizes_quick` (cumulative row counts to seed up to, added on top of
   whatever's already in the dev database).

## CI

`.github/workflows/perf.yml` runs `test_page_perf.py` on every pull request.
Deliberately narrow, on purpose:

- **`pull_request` only** — no `push` trigger yet. Once it's proven reliable
  across a few PRs, a `push: branches: [main]` trigger is a reasonable next
  step.
- **No repository secrets, no SSH step, no reference to the deploy server.**
  Postgres and Redis are `services:` containers that live only for the job and
  disappear when it ends. A workflow that needs no secrets can't reach
  production even by accident — that's the property that matters most here.
- **Not wired into `deploy.yml`** — not a `needs:` of the deploy job, and not
  (yet) a required status check on `main`. A flaky wall-clock timing must never
  block a deploy; once the workflow's been watched for a while, tightening
  that is a separate, deliberate decision.
- Query-count failures are real failures; render-time is reported only (see
  above) — the same reasoning applies doubly on a shared runner.

## What the first run found (sample data, before the caching PR)

| page | median ms | queries | scaling | note |
|---|---:|---:|---|---|
| `/taxonomy/` | ~860 | 4 | **O(n) in taxa** | loads every Taxon, builds whole tree in Python each request, no cache |
| `/api/v1/species/?page_size=10000` | ~1060 | 3 | **O(n) in taxa** | DRF serializes 10k rows one object at a time; the annotate tab calls this on load |
| `/beetles/` | ~825 | **47** | sub-linear time | filter-dropdown options rebuilt with ~1 query per filter per request |
| `/tools/annotate/` | ~720 | 41 | flat | same filter-dropdown pattern |
| `/api/v1/beetles/images-with-annotations/` | ~430 | **109** | flat in total data | ~2 queries per row on a 50-row page — N+1 |

DB time was a small fraction of wall time on every page → the bottleneck was
**Python** (tree building, serialization), not the queries themselves. This
branch caches `/taxonomy/` and `/api/v1/species/`, bringing both from ~O(n)
to near-flat, and caches the Image Browser's default-view filter dropdowns
(47 queries → 9 on a cache hit, confirmed fixed cost — see
`test_gallery_default_filters_cache_hit_and_invalidates`). `/tools/annotate/`
has the identical dropdown-building pattern but is not cached here — same
fix, not yet applied there, since it wasn't the page reported as slow.
See the commit history for full before/after numbers.
