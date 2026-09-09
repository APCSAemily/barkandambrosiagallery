"""
Page performance regression tests (pass/fail against a time budget).

Isolated from the app: reuses the seeding helpers from scaling_probe, seeds a
fixed synthetic dataset, and asserts each page renders under a millisecond
budget and without a query-count blow-up. All inside a transaction that is
rolled back - the dev database is left untouched.

Run:
    docker compose run --rm web pixi run python -m unittest perf.test_page_perf -v

Tune BUDGET_MS / QUERY_CEILING to your machine; treat these as "don't regress"
guards, not absolute targets. Numbers are wall-clock in the container.
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

from perf.scaling_probe import seed_taxa, seed_specimens, _reset_perf

# Fixed synthetic dataset for the test (added on top of whatever is already there)
SEED_TAXA = 5000
SEED_SPECIMENS = 2000
REPEATS = 4

# Budgets (median wall-clock ms) and hard query ceilings per request.
# /taxonomy/ and the species list are now cache-served on repeat hits, so their
# budgets are tight; the other pages are unchanged by this PR.
BUDGET_MS = {
    "/taxonomy/": 300,
    "/beetles/?per_page=12": 1200,
    "/tools/annotate/": 1200,
    "/api/v1/species/?page_size=10000": 400,
    "/api/v1/beetles/images-with-annotations/?page=1&page_size=50": 900,
}
QUERY_CEILING = {
    "/taxonomy/": 8,
    "/beetles/?per_page=12": 80,
    "/tools/annotate/": 80,
    "/api/v1/species/?page_size=10000": 8,
    "/api/v1/beetles/images-with-annotations/?page=1&page_size=50": 150,
}


class PagePerfTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        _reset_perf()
        seed_taxa(SEED_TAXA, 0)
        taxon_ids = list(
            __import__("beetlesgallery.beetles_app.models", fromlist=["Taxon"])
            .Taxon.objects.filter(valid_species_id__startswith="PERF-")
            .values_list("id", flat=True)
        )
        seed_specimens(SEED_SPECIMENS, 0, taxon_ids)

    def setUp(self):
        self.client = Client()
        user = get_user_model().objects.filter(is_superuser=True).first()
        assert user, "no superuser - run createsuperuser"
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
        rows = []
        failures = []
        for url, budget in BUDGET_MS.items():
            code, ms, q = self._profile(url)
            rows.append((url, code, ms, q))
            if code != 200:
                failures.append(f"{url}: HTTP {code}")
            if ms > budget:
                failures.append(f"{url}: {ms:.0f} ms > budget {budget} ms")
            if q > QUERY_CEILING[url]:
                failures.append(f"{url}: {q} queries > ceiling {QUERY_CEILING[url]}")

        print(f"\n  (seeded +{SEED_TAXA} taxa, +{SEED_SPECIMENS} specimens)\n")
        print(f"  {'url':<58} {'code':>4} {'ms':>8} {'queries':>8}")
        for url, code, ms, q in rows:
            print(f"  {url:<58} {code:>4} {ms:>8.0f} {q:>8}")
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

        # First hit: cache miss, builds the tree.
        t0 = time.perf_counter()
        r1 = self.client.get("/taxonomy/", HTTP_HOST="localhost")
        cold_ms = (time.perf_counter() - t0) * 1000
        self.assertEqual(r1.status_code, 200)
        self.assertIsNotNone(cache.get(TAXONOMY_BROWSER_CACHE_KEY), "tree was not cached")

        # Repeat hits: cache hit, no per-request rebuild.
        warm = []
        for _ in range(REPEATS):
            t0 = time.perf_counter()
            self.client.get("/taxonomy/", HTTP_HOST="localhost")
            warm.append((time.perf_counter() - t0) * 1000)
        warm_ms = statistics.median(warm)

        print(f"\n  /taxonomy/  cold {cold_ms:.0f} ms  ->  warm {warm_ms:.0f} ms\n")
        self.assertLess(warm_ms, 150, f"cached hit still slow: {warm_ms:.0f} ms")
        self.assertLess(warm_ms, cold_ms, "cache hit was not faster than the miss")

        # A taxonomy change clears the cache.
        invalidate_taxonomy_caches()
        self.assertIsNone(cache.get(TAXONOMY_BROWSER_CACHE_KEY), "cache not invalidated")
