"""
Behaviour tests for the REST API used by the annotation tool (/api/v1/): lists
and filters, soft delete, image locks, bulk updates and the species search
(issue #206, part 3). Who may call the API at all is covered in test_pages.py.
"""
from datetime import timedelta
from unittest import mock

from django.utils import timezone

from beetlesgallery.beetles_app.api.views import BeetlesViewSet
from beetlesgallery.beetles_app.models import Beetles, ImageAsset, ImageLock
from beetlesgallery.beetles_app.testing import PageBehaviourCase, make_beetle, make_image, make_taxon

API = "/api/v1"


class ApiTestCase(PageBehaviourCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)

    def patch(self, url, payload):
        return self.client.patch(url, payload, content_type="application/json")

    def post(self, url, payload=None):
        return self.client.post(url, payload or {}, content_type="application/json")

    @staticmethod
    def ids(response):
        data = response.json()
        rows = data["results"] if isinstance(data, dict) and "results" in data else data
        return {row["id"] for row in rows}


class BeetlesListTests(ApiTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        scolytinae = make_taxon("T-S", subfamily="Scolytinae")
        platypodinae = make_taxon("T-P", subfamily="Platypodinae")
        cls.image = make_image()
        cls.boxed = make_beetle(image=cls.image, taxon=scolytinae, bbox="validated")
        cls.ghost = make_beetle(image=cls.image, taxon=scolytinae)  # no box yet
        cls.other = make_beetle(taxon=platypodinae)

    def list(self, query=""):
        return self.client.get(f"{API}/beetles/?{query}")

    def test_lists_specimens(self):
        response = self.list()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.ids(response), {str(self.boxed.id), str(self.ghost.id), str(self.other.id)})

    def test_soft_deleted_specimens_are_not_listed(self):
        self.other.delete()
        self.assertNotIn(str(self.other.id), self.ids(self.list()))

    def test_filter_by_subfamily_ignores_case(self):
        self.assertEqual(self.ids(self.list("subfamily=scolytinae")), {str(self.boxed.id), str(self.ghost.id)})

    def test_filter_by_image(self):
        self.assertEqual(
            self.ids(self.list(f"image_asset={self.other.image_asset_id}")), {str(self.other.id)}
        )

    def test_filter_by_has_bbox(self):
        self.assertEqual(self.ids(self.list("has_bbox=true")), {str(self.boxed.id)})
        self.assertEqual(self.ids(self.list("has_bbox=false")), {str(self.ghost.id), str(self.other.id)})

    def test_retrieve_one_specimen_includes_its_taxonomy(self):
        data = self.client.get(f"{API}/beetles/{self.boxed.id}/").json()
        self.assertEqual(data["subfamily"], "Scolytinae")
        self.assertEqual(data["image_asset"]["id"], str(self.image.id))


class BeetlesWriteTests(ApiTestCase):
    def test_delete_is_a_soft_delete_by_the_current_user(self):
        beetle = make_beetle()
        response = self.client.delete(f"{API}/beetles/{beetle.id}/")

        self.assertEqual(response.status_code, 204)
        beetle.refresh_from_db()
        self.assertTrue(beetle.is_deleted)
        self.assertEqual(beetle.last_updated_by, self.staff)
        self.assertEqual(self.client.get(f"{API}/beetles/{beetle.id}/").status_code, 404)

    def test_validating_a_box_records_who_and_when(self):
        beetle = make_beetle(bbox="unvalidated")
        response = self.patch(f"{API}/beetles/{beetle.id}/", {"bbox_is_validated": True})

        self.assertEqual(response.status_code, 200)
        beetle.refresh_from_db()
        self.assertTrue(beetle.bbox_is_validated)
        self.assertEqual(beetle.bbox_validated_by, self.staff)
        self.assertIsNotNone(beetle.bbox_validated_at)
        beetle.image_asset.refresh_from_db()
        self.assertTrue(beetle.image_asset.is_validated)

    def test_clearing_the_box_keeps_the_specimen_but_resets_the_box(self):
        beetle = make_beetle(bbox="validated")
        response = self.patch(f"{API}/beetles/{beetle.id}/", {"bbox_x": None})

        self.assertEqual(response.status_code, 200)
        beetle.refresh_from_db()
        self.assertFalse(beetle.is_deleted)
        self.assertIsNone(beetle.bbox_x)
        self.assertFalse(beetle.bbox_is_validated)
        self.assertIsNone(beetle.bbox_validated_by)

    def test_box_outside_the_image_is_rejected(self):
        beetle = make_beetle(bbox="unvalidated")
        for payload in ({"bbox_x": 1.5}, {"bbox_x": 0.9, "bbox_width": 0.5}, {"bbox_width": 0}):
            with self.subTest(payload=payload):
                self.assertEqual(self.patch(f"{API}/beetles/{beetle.id}/", payload).status_code, 400)

    def test_creating_a_box_fills_in_the_ghost_specimen_on_that_image(self):
        ghost = make_beetle(collection_country="USA")  # metadata, no box yet
        response = self.post(f"{API}/beetles/", {
            "image_asset_id": str(ghost.image_asset_id),
            "bbox_x": 0.1, "bbox_y": 0.2, "bbox_width": 0.3, "bbox_height": 0.4,
        })

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Beetles.objects.filter(image_asset=ghost.image_asset).count(), 1)
        ghost.refresh_from_db()
        self.assertEqual(ghost.bbox_x, 0.1)
        self.assertEqual(ghost.bbox_created_by, self.staff)

    def test_an_extra_box_copies_the_metadata_of_the_existing_one(self):
        first = make_beetle(bbox="validated", collection_country="Brazil", specimen_sex="f")
        response = self.post(f"{API}/beetles/", {
            "image_asset_id": str(first.image_asset_id),
            "bbox_x": 0.5, "bbox_y": 0.5, "bbox_width": 0.2, "bbox_height": 0.2,
        })

        self.assertEqual(response.status_code, 201)
        self.assertEqual(Beetles.objects.filter(image_asset=first.image_asset).count(), 2)
        new = Beetles.objects.get(pk=response.json()["id"])
        self.assertEqual((new.collection_country, new.specimen_sex), ("Brazil", "f"))

    def test_creating_a_box_on_an_unknown_image_is_rejected(self):
        response = self.post(f"{API}/beetles/", {
            "image_asset_id": "00000000-0000-0000-0000-000000000000",
            "bbox_x": 0.1, "bbox_y": 0.1, "bbox_width": 0.2, "bbox_height": 0.2,
        })
        self.assertEqual(response.status_code, 400)


class BulkUpdateTests(ApiTestCase):
    URL = f"{API}/beetles/bulk-update/"

    def test_payload_must_be_a_list(self):
        response = self.patch(self.URL, {"id": "x"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json(), {"error": "Expected a list of objects."})

    def test_updates_every_row(self):
        a, b = make_beetle(bbox="unvalidated"), make_beetle(bbox="unvalidated")
        response = self.patch(self.URL, [
            {"id": str(a.id), "bbox_is_validated": True},
            {"id": str(b.id), "bbox_is_validated": True},
        ])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 2)
        for row in (a, b):
            row.refresh_from_db()
            self.assertTrue(row.bbox_is_validated)
            self.assertEqual(row.bbox_validated_by, self.staff)

    def test_one_bad_row_rolls_back_the_whole_batch(self):
        good, bad = make_beetle(bbox="unvalidated"), make_beetle(bbox="unvalidated")
        response = self.patch(self.URL, [
            {"id": str(good.id), "bbox_is_validated": True},
            {"id": str(bad.id), "bbox_x": 5},
        ])

        self.assertEqual(response.status_code, 400)
        good.refresh_from_db()
        self.assertFalse(good.bbox_is_validated)
        # The failing row and its field errors are reported so staff can fix them.
        details = response.json()["details"]
        self.assertEqual(details["id"], str(bad.id))
        self.assertIn("bbox_x", details["errors"])

    def test_unexpected_errors_are_not_echoed(self):
        beetle = make_beetle(bbox="unvalidated")
        with mock.patch.object(BeetlesViewSet, "perform_update", side_effect=RuntimeError("secret path /opt/x")):
            with self.assertLogs("beetlesgallery.beetles_app.api.views", level="ERROR"):
                response = self.patch(self.URL, [{"id": str(beetle.id), "bbox_is_validated": True}])
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("secret", response.content.decode())

    def test_unknown_and_deleted_ids_are_skipped(self):
        gone = make_beetle(bbox="unvalidated")
        gone.delete()
        response = self.patch(self.URL, [
            {"id": str(gone.id), "bbox_is_validated": True},
            {"id": "00000000-0000-0000-0000-000000000000", "bbox_is_validated": True},
        ])
        self.assertEqual((response.status_code, response.json()), (200, []))
        gone.refresh_from_db()
        self.assertFalse(gone.bbox_is_validated)


class ImageAssetApiTests(ApiTestCase):
    def test_lists_images_without_the_deleted_ones(self):
        kept, gone = make_image(), make_image()
        gone.delete()
        self.assertEqual(self.ids(self.client.get(f"{API}/image-assets/")), {str(kept.id)})

    def test_delete_soft_deletes_the_image_and_its_specimens(self):
        beetle = make_beetle()
        response = self.client.delete(f"{API}/image-assets/{beetle.image_asset_id}/")

        self.assertEqual(response.status_code, 204)
        image = ImageAsset.objects.get(pk=beetle.image_asset_id)
        beetle.refresh_from_db()
        self.assertTrue(image.is_deleted and beetle.is_deleted)
        self.assertEqual(image.last_updated_by, self.staff)

    def test_download_of_an_image_with_no_file_is_a_404(self):
        response = self.client.get(f"{API}/image-assets/{make_image().id}/download/")
        self.assertEqual(response.status_code, 404)


class ImageLockApiTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.image = make_image()
        self.lock_url = f"{API}/image-assets/{self.image.id}/lock/"
        self.unlock_url = f"{API}/image-assets/{self.image.id}/unlock/"

    def age(self, lock, minutes):
        ImageLock.objects.filter(pk=lock.pk).update(updated_at=timezone.now() - timedelta(minutes=minutes))

    def test_locking_a_free_image_takes_the_lock(self):
        response = self.post(self.lock_url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["message"], "Lock acquired")
        self.assertEqual(ImageLock.objects.get(image_asset=self.image).locked_by, self.staff)

    def test_locking_your_own_image_again_refreshes_it(self):
        self.post(self.lock_url)
        self.assertEqual(self.post(self.lock_url).json()["message"], "Lock refreshed")
        self.assertEqual(ImageLock.objects.count(), 1)

    def test_someone_elses_lock_blocks_you(self):
        ImageLock.objects.create(image_asset=self.image, locked_by=self.superuser)

        response = self.post(self.lock_url)

        self.assertEqual(response.status_code, 409)
        body = response.json()
        self.assertFalse(body["success"])
        self.assertEqual(body["locked_by"], "super")
        self.assertEqual(ImageLock.objects.get().locked_by, self.superuser)

    def test_an_expired_lock_can_be_taken_over(self):
        self.age(ImageLock.objects.create(image_asset=self.image, locked_by=self.superuser), minutes=10)

        response = self.post(self.lock_url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(ImageLock.objects.get().locked_by, self.staff)

    def test_unlock_releases_your_lock(self):
        self.post(self.lock_url)
        response = self.post(self.unlock_url)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(ImageLock.objects.exists())

    def test_unlock_without_a_lock_is_a_404(self):
        self.assertEqual(self.post(self.unlock_url).status_code, 404)

    def test_you_cannot_unlock_someone_elses_image(self):
        ImageLock.objects.create(image_asset=self.image, locked_by=self.superuser)
        self.assertEqual(self.post(self.unlock_url).status_code, 404)
        self.assertTrue(ImageLock.objects.exists())

    def test_heartbeat_takes_a_free_lock_then_refreshes_it(self):
        url = f"{API}/image-assets/{self.image.id}/heartbeat/"
        self.assertEqual(self.post(url).json(), {"status": "Lock acquired"})
        self.assertEqual(self.post(url).json(), {"status": "Lock refreshed"})

    def test_heartbeat_is_refused_while_someone_else_holds_the_lock(self):
        ImageLock.objects.create(image_asset=self.image, locked_by=self.superuser)
        response = self.post(f"{API}/image-assets/{self.image.id}/heartbeat/")
        self.assertEqual(response.status_code, 409)

    def test_locking_cleans_up_other_expired_locks(self):
        stale = ImageLock.objects.create(image_asset=make_image(), locked_by=self.superuser)
        self.age(stale, minutes=30)
        self.post(self.lock_url)
        self.assertFalse(ImageLock.objects.filter(pk=stale.pk).exists())


class AnnotationFeedTests(ApiTestCase):
    URL = f"{API}/beetles/images-with-annotations/"

    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        cls.validated = make_beetle(bbox="validated", collection_country="USA").image_asset
        cls.pending = make_beetle(bbox="unvalidated", collection_country="Brazil").image_asset
        cls.unboxed = make_beetle(collection_country="USA").image_asset

    def feed(self, query=""):
        response = self.client.get(f"{self.URL}?{query}")
        self.assertEqual(response.status_code, 200)
        return response.json()

    @staticmethod
    def image_ids(data):
        return {r["image_asset_id"] for r in data["results"]}

    def test_lists_every_image_with_its_annotation_state(self):
        data = self.feed()
        self.assertEqual(data["count"], 3)
        by_id = {r["image_asset_id"]: r for r in data["results"]}
        self.assertEqual(by_id[str(self.validated.id)]["annotation_count"], 1)
        self.assertTrue(by_id[str(self.validated.id)]["is_validated"])
        self.assertTrue(by_id[str(self.pending.id)]["has_unvalidated_boxes"])
        self.assertEqual(by_id[str(self.unboxed.id)]["annotation_count"], 0)

    def test_first_page_carries_summary_stats(self):
        self.assertEqual(self.feed()["stats"], {
            "images_validated": 1, "images_unvalidated": 2, "images_no_bbox": 1,
            "rois_validated": 1, "rois_unvalidated": 1,
        })

    def test_later_pages_skip_the_stats(self):
        data = self.feed("page_size=1&page=2")
        self.assertIsNone(data["stats"])
        self.assertIsNotNone(data["previous"])

    def test_page_size_and_next_link(self):
        data = self.feed("page_size=2")
        self.assertEqual(len(data["results"]), 2)
        self.assertEqual(data["count"], 3)
        self.assertIn("page=2", data["next"])

    def test_filters_narrow_the_feed(self):
        self.assertEqual(self.image_ids(self.feed("country=USA")), {str(self.validated.id), str(self.unboxed.id)})

    def test_search_narrows_the_feed(self):
        self.assertEqual(self.image_ids(self.feed("search=country:Brazil")), {str(self.pending.id)})

    def test_deleted_images_are_left_out(self):
        self.unboxed.delete()
        self.assertEqual(self.feed()["count"], 2)

    def test_a_current_lock_is_shown_on_the_image(self):
        ImageLock.objects.create(image_asset=self.pending, locked_by=self.superuser)
        by_id = {r["image_asset_id"]: r for r in self.feed()["results"]}
        self.assertEqual(by_id[str(self.pending.id)]["lock"]["locked_by"], "super")
        self.assertIsNone(by_id[str(self.validated.id)]["lock"])


class SpeciesApiTests(ApiTestCase):
    @classmethod
    def setUpTestData(cls):
        super().setUpTestData()
        make_taxon("T-IPS", scientific_name="Ips typographus", subfamily="Scolytinae", genus="Ips", species="typographus")
        make_taxon("T-XYL", scientific_name="Xyleborus affinis", subfamily="Scolytinae", genus="Xyleborus", species="affinis")
        make_taxon("T-PLA", scientific_name="Platypus cylindrus", subfamily="Platypodinae", genus="Platypus", species="cylindrus")

    def names(self, query=""):
        response = self.client.get(f"{API}/species/?{query}")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        rows = data["results"] if isinstance(data, dict) else data
        return {row["scientificName"] for row in rows}

    def test_lists_all_species(self):
        self.assertEqual(len(self.names()), 3)

    def test_search_matches_names_and_ranks(self):
        self.assertEqual(self.names("search=xyleb"), {"Xyleborus affinis"})
        self.assertEqual(self.names("search=scolytinae"), {"Ips typographus", "Xyleborus affinis"})

    def test_exact_filters_ignore_case(self):
        self.assertEqual(self.names("genus=ips"), {"Ips typographus"})
        self.assertEqual(self.names("subfamily=PLATYPODINAE"), {"Platypus cylindrus"})
        self.assertEqual(self.names("genus=Ips&subfamily=Platypodinae"), set())

    def test_species_endpoint_is_read_only(self):
        self.assertEqual(self.client.post(f"{API}/species/", {}, content_type="application/json").status_code, 405)
