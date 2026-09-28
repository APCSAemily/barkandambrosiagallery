# Guards the N+1 and duplicate-count fixes from #223 and #252 (issue #256, phase 2).
from django.core.cache import cache
from django.db import connection
from django.test.utils import CaptureQueriesContext

from beetlesgallery.beetles_app.models import ImageLock
from beetlesgallery.beetles_app.testing import PageBehaviourCase, make_beetle, make_image, make_taxon

API = "/api/v1"


def add_image_with_mixed_taxa(n_specimens=2):
    image = make_image()
    for _ in range(n_specimens):
        make_beetle(image=image, taxon=make_taxon(), bbox="unvalidated")
    return image


def sql_of(queries):
    return [q["sql"] for q in queries]


class QueryCountCase(PageBehaviourCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        add_image_with_mixed_taxa()

    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)

    def cold_and_warm_queries(self, url, **extra):
        cache.clear()
        runs = []
        for _ in range(2):
            with CaptureQueriesContext(connection) as ctx:
                response = self.client.get(url, **extra)
            self.assertEqual(response.status_code, 200)
            runs.append(ctx.captured_queries)
        return runs

    def assertQueryCountIgnoresRowCount(self, url, **extra):
        _, before = self.cold_and_warm_queries(url, **extra)
        add_image_with_mixed_taxa()
        add_image_with_mixed_taxa(n_specimens=3)
        _, after = self.cold_and_warm_queries(url, **extra)
        self.assertEqual(len(before), len(after), "\n".join(sql_of(after)))


class NoNPlusOneTests(QueryCountCase):
    def test_gallery_page(self):
        self.assertQueryCountIgnoresRowCount("/beetles/?per_page=100")

    def test_gallery_ajax_page_flip(self):
        self.assertQueryCountIgnoresRowCount("/beetles/?per_page=100", HTTP_X_REQUESTED_WITH="XMLHttpRequest")

    def test_beetles_api_list(self):
        self.assertQueryCountIgnoresRowCount(f"{API}/beetles/")

    def test_annotation_feed(self):
        ImageLock.objects.create(image_asset=make_image(), locked_by=self.staff)
        self.assertQueryCountIgnoresRowCount(f"{API}/beetles/images-with-annotations/?page_size=50")


class CacheHitTests(QueryCountCase):
    def test_landing_counts_are_cached(self):
        cold, warm = self.cold_and_warm_queries("/")
        self.assertTrue(any("COUNT(" in sql for sql in sql_of(cold)))
        self.assertFalse(any("COUNT(" in sql for sql in sql_of(warm)), "\n".join(sql_of(warm)))

    def test_gallery_match_count_is_cached(self):
        cold, warm = self.cold_and_warm_queries("/beetles/")
        self.assertTrue(any("COUNT(" in sql for sql in sql_of(cold)))
        self.assertFalse(any("COUNT(" in sql for sql in sql_of(warm)), "\n".join(sql_of(warm)))

    def test_annotate_filter_options_are_cached(self):
        cold, warm = self.cold_and_warm_queries("/tools/annotate/")
        self.assertTrue(any('FROM "beetles"' in sql for sql in sql_of(cold)))
        self.assertFalse(any('FROM "beetles"' in sql for sql in sql_of(warm)), "\n".join(sql_of(warm)))


class AnnotationFeedCountTests(QueryCountCase):
    def test_total_is_counted_once(self):
        add_image_with_mixed_taxa()
        _, warm = self.cold_and_warm_queries(f"{API}/beetles/images-with-annotations/?page=2&page_size=1")
        counts = [sql for sql in sql_of(warm) if sql.startswith("SELECT COUNT(*)") and '"beetles_app_imageasset"' in sql]
        self.assertEqual(len(counts), 1, "\n".join(counts))
