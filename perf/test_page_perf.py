"""
Page performance regression tests.

Isolated from the app: reuses the seeding helpers from scaling_probe, seeds a
fixed synthetic dataset, and checks each page's query count and render time.
Query counts are deterministic and asserted as hard failures -- that's what
catches an N+1 getting reintroduced. Millisecond timings are noisy on shared
CI runners, so they're only reported (printed), never asserted. All DB writes
happen inside a transaction that is rolled back - the dev database is left
untouched.

Run (either works once migration 0010_1 has created the pg_trgm extension in
the test database -- see perf/README.md):
    docker compose run --rm web pixi run python -m unittest perf.test_page_perf -v
    docker compose run --rm web pixi run python manage.py test perf

Tune BUDGET_MS / QUERY_CEILING to your machine and data volume.
"""
import os
import statistics
import time

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "beetlesgallery.settings")
django.setup()

from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.test import Client

from beetlesgallery.beetles_app.models import Taxon
from perf.scaling_probe import seed_taxa, seed_specimens, _reset_perf, _ensure_safe_to_run

# Fixed synthetic dataset for the test (added on top of whatever is already there)
SEED_TAXA = 5000
SEED_SPECIMENS = 2000
REPEATS = 4

# QUERY_CEILING is a hard failure if exceeded; BUDGET_MS is reported only (see
# test_pages_within_budget). /taxonomy/, the species list, and the default-view
# gallery filters are all cache-served on repeat hits, so their budgets/
# ceilings are tight; /tools/annotate/ and the images API are unaffected by
# either caching change (see cache_keys.py for why the gallery filter cache
# doesn't cover /tools/annotate/'s identical-looking dropdown pattern too).
BUDGET_MS = {
    "/taxonomy/": 300,
    "/beetles/?per_page=12": 800,
    "/beetles/?per_page=100": 1200,
    "/tools/annotate/": 1200,
    "/api/v1/species/?page_size=10000": 400,
    "/api/v1/beetles/images-with-annotations/?page=1&page_size=50": 900,
}
QUERY_CEILING = {
    "/taxonomy/": 8,
    # was 80 (47 queries/request, fixed cost); cached default view runs 9 -- see
    # test_gallery_default_filters_cache_hit_and_invalidates
    "/beetles/?per_page=12": 20,
    "/beetles/?per_page=100": 20,
    "/tools/annotate/": 80,
    "/api/v1/species/?page_size=10000": 8,
    "/api/v1/beetles/images-with-annotations/?page=1&page_size=50": 150,
}


class PagePerfTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        # No escape hatch here (unlike the script) -- there is never a
        # legitimate reason to run this suite against a non-local database.
        _ensure_safe_to_run(force_unsafe=False)

        _reset_perf()
        seed_taxa(SEED_TAXA, 0)
        taxon_ids = list(
            Taxon.objects.filter(valid_species_id__startswith="PERF-")
            .values_list("id", flat=True)
        )
        seed_specimens(SEED_SPECIMENS, 0, taxon_ids)

    def setUp(self):
        self.client = Client()
        User = get_user_model()
        user = User.objects.filter(is_superuser=True).first()
        if user is None:
            # A throwaway manage.py test database has no dev admin account.
            user = User.objects.create_superuser("perf-test-admin", "", "not-a-real-password")
        self.client.force_login(user)

    def _profile(self, url):
        self.client.get(url, HTTP_HOST="localhost")  # warm
        times, qcounts = [], []
        for _ in range(REPEATS):
            with CaptureQueriesContext(connection) as ctx:
                t0 = time.perf_counter()
                resp = self.client.get(url, HTTP_HOST="localhost")
                times.append((time.perf_counter() - t0) * 1000)
            qcounts.append(len(ctx.captured_queries))
        return resp.status_code, statistics.median(times), max(qcounts)

    def test_pages_within_budget(self):
        """Query counts are deterministic and asserted as hard failures -- a
        rising count is exactly what catches an N+1 getting reintroduced.
        Millisecond budgets are wall-clock and noisy (especially on shared CI
        runners), so they're only printed as PASS/WARN, never asserted."""
        rows = []
        failures = []
        for url, budget in BUDGET_MS.items():
            code, ms, q = self._profile(url)
            over_budget = ms > budget
            rows.append((url, code, ms, budget, over_budget, q))
            if code != 200:
                failures.append(f"{url}: HTTP {code}")
            if q > QUERY_CEILING[url]:
                failures.append(f"{url}: {q} queries > ceiling {QUERY_CEILING[url]}")

        print(f"\n  (seeded +{SEED_TAXA} taxa, +{SEED_SPECIMENS} specimens)\n")
        print(f"  {'url':<58} {'code':>4} {'ms':>8} {'budget':>8} {'':<6} {'queries':>7}")
        for url, code, ms, budget, over_budget, q in rows:
            flag = "WARN" if over_budget else "ok"
            print(f"  {url:<58} {code:>4} {ms:>8.0f} {budget:>8} {flag:<6} {q:>7}")
        print()

        self.assertEqual(failures, [], "\n  - " + "\n  - ".join(failures) if failures else "")

    def test_taxonomy_cache_hit_is_fast_and_invalidates(self):
        """The Taxonomy Browser must be served from cache on repeat hits, and a
        taxonomy change must invalidate that cache."""
        from django.core.cache import cache
        from beetlesgallery.beetles_app.cache_keys import (
            TAXONOMY_BROWSER_CACHE_KEY,
            invalidate_taxonomy_caches,
        )

        invalidate_taxonomy_caches()
        cache.delete(TAXONOMY_BROWSER_CACHE_KEY)

        # First hit: cache miss, builds the tree (queries the Taxon table).
        with CaptureQueriesContext(connection) as ctx:
            t0 = time.perf_counter()
            r1 = self.client.get("/taxonomy/", HTTP_HOST="localhost")
            cold_ms = (time.perf_counter() - t0) * 1000
        cold_queries = len(ctx.captured_queries)
        self.assertEqual(r1.status_code, 200)
        self.assertIsNotNone(cache.get(TAXONOMY_BROWSER_CACHE_KEY), "tree was not cached")

        # Repeat hits: cache hit, no per-request rebuild.
        warm, warm_queries = [], []
        for _ in range(REPEATS):
            with CaptureQueriesContext(connection) as ctx:
                t0 = time.perf_counter()
                self.client.get("/taxonomy/", HTTP_HOST="localhost")
                warm.append((time.perf_counter() - t0) * 1000)
            warm_queries.append(len(ctx.captured_queries))
        warm_ms = statistics.median(warm)
        warm_q = max(warm_queries)

        print(f"\n  /taxonomy/  cold {cold_ms:.0f} ms / {cold_queries} queries  ->  "
              f"warm {warm_ms:.0f} ms / {warm_q} queries\n")
        # Deterministic, hard-asserted: a cache hit must not re-run the
        # taxon-table query the miss did. Timing is reported above, not asserted.
        self.assertLess(warm_q, cold_queries,
                         "cache hit ran as many queries as the miss - is caching working?")

        # A taxonomy change clears the cache.
        invalidate_taxonomy_caches()
        self.assertIsNone(cache.get(TAXONOMY_BROWSER_CACHE_KEY), "cache not invalidated")

    def test_gallery_default_filters_cache_hit_and_invalidates(self):
        """The Image Browser's default (no search/filter) view must serve its
        filter dropdowns from cache on repeat hits, a filtered/searched
        request must NOT use that cache, and invalidation must clear it."""
        from django.core.cache import cache
        from beetlesgallery.beetles_app.cache_keys import (
            GALLERY_DEFAULT_FILTERS_CACHE_KEY,
            invalidate_gallery_filter_cache,
        )

        invalidate_gallery_filter_cache()

        # First hit: cache miss, builds all 22 filters' options live.
        with CaptureQueriesContext(connection) as ctx:
            r1 = self.client.get("/beetles/", HTTP_HOST="localhost")
        cold_queries = len(ctx.captured_queries)
        self.assertEqual(r1.status_code, 200)
        self.assertIsNotNone(cache.get(GALLERY_DEFAULT_FILTERS_CACHE_KEY),
                              "default-view filter options were not cached")

        # Repeat hit: cache hit, no per-request rebuild.
        with CaptureQueriesContext(connection) as ctx:
            self.client.get("/beetles/", HTTP_HOST="localhost")
        warm_queries = len(ctx.captured_queries)

        print(f"\n  /beetles/ (default)  cold {cold_queries} queries  ->  warm {warm_queries} queries\n")
        self.assertLess(warm_queries, cold_queries,
                         "cache hit ran as many queries as the miss - is caching working?")

        # A filtered/searched request must stay live, not read (or write) the
        # default-view cache -- otherwise it would serve wrong (too-broad or
        # stale) options, or clobber the cached default view with a narrower
        # one. Comparable query count to the cold miss, not to the cache hit.
        with CaptureQueriesContext(connection) as ctx:
            r2 = self.client.get("/beetles/?q=ips", HTTP_HOST="localhost")
        self.assertEqual(r2.status_code, 200)
        self.assertGreater(len(ctx.captured_queries), warm_queries,
                            "a search request appears to have used the default-view cache")

        # Invalidation clears it.
        invalidate_gallery_filter_cache()
        self.assertIsNone(cache.get(GALLERY_DEFAULT_FILTERS_CACHE_KEY), "cache not invalidated")
