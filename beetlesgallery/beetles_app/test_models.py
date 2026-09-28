"""
Logic tests for the model methods in models.py (issue #206, part 2):
soft delete, the image-validation signal, taxon syncing on save, image-lock
expiry, and the upload / update batch status changes.
"""
import hashlib
import json
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from beetlesgallery.beetles_app.models import (
    Beetles, DownloadJob, ImageAsset, ImageLock, UpdateBatch, UploadBatch,
)
from beetlesgallery.beetles_app.testing import make_beetle, make_image, make_taxon


def just_now(moment):
    """True if a timestamp was set in the last minute."""
    return moment is not None and timezone.now() - moment < timedelta(minutes=1)


# --------------------------------------------------------------------------
# Soft delete
# --------------------------------------------------------------------------
class SoftDeleteTests(TestCase):
    """delete() flags the row instead of removing it; images cascade to their ROIs."""

    @classmethod
    def setUpTestData(cls):
        cls.user = get_user_model().objects.create_user("curator", password="pw")

    def test_beetle_delete_only_flags_the_row(self):
        beetle = make_beetle()
        beetle.delete()
        beetle.refresh_from_db()  # the row is still there
        self.assertTrue(beetle.is_deleted)
        self.assertTrue(just_now(beetle.deleted_at))

    def test_beetle_delete_records_who_did_it(self):
        beetle = make_beetle()
        beetle.delete(deleted_by=self.user)
        beetle.refresh_from_db()
        self.assertEqual(beetle.last_updated_by, self.user)

    def test_beetle_delete_without_a_user_leaves_the_editor_unset(self):
        beetle = make_beetle()
        beetle.delete()
        beetle.refresh_from_db()
        self.assertIsNone(beetle.last_updated_by)

    def test_beetle_delete_leaves_other_rows_alone(self):
        gone, kept = make_beetle(), make_beetle()
        gone.delete()
        kept.refresh_from_db()
        self.assertFalse(kept.is_deleted)
        self.assertIsNone(kept.deleted_at)

    def test_soft_deleted_rows_stay_in_the_default_queryset(self):
        # Nothing hides them automatically: every listing has to filter
        # is_deleted=False itself, as the image browser does.
        beetle = make_beetle()
        beetle.delete()
        self.assertTrue(Beetles.objects.filter(pk=beetle.pk).exists())
        self.assertFalse(Beetles.objects.filter(pk=beetle.pk, is_deleted=False).exists())

    def test_image_delete_flags_the_image_and_all_its_beetles(self):
        image = make_image()
        first, second = make_beetle(image=image), make_beetle(image=image)
        elsewhere = make_beetle()  # a different image

        image.delete(deleted_by=self.user)

        for row in (image, first, second, elsewhere):
            row.refresh_from_db()
        self.assertTrue(image.is_deleted)
        self.assertTrue(just_now(image.deleted_at))
        self.assertEqual(image.last_updated_by, self.user)
        self.assertTrue(first.is_deleted and second.is_deleted)
        self.assertEqual(first.last_updated_by, self.user)
        self.assertFalse(elsewhere.is_deleted)

    def test_image_delete_keeps_the_row(self):
        image = make_image()
        image.delete()
        self.assertTrue(ImageAsset.objects.filter(pk=image.pk).exists())

    def test_image_delete_does_not_re_stamp_beetles_deleted_earlier(self):
        image = make_image()
        early = make_beetle(image=image)
        early.delete()
        early.refresh_from_db()
        first_deleted_at = early.deleted_at

        make_beetle(image=image)  # a live one, so the cascade has work to do
        image.delete()

        early.refresh_from_db()
        self.assertEqual(early.deleted_at, first_deleted_at)


# --------------------------------------------------------------------------
# Image validation signal
# --------------------------------------------------------------------------
class ImageValidationSignalTests(TestCase):
    """An image counts as validated when it has boxes and every live box is validated."""

    def setUp(self):
        self.image = make_image()

    def validated(self):
        self.image.refresh_from_db()
        return self.image.is_validated

    def test_image_with_no_boxes_is_not_validated(self):
        make_beetle(image=self.image)
        self.assertFalse(self.validated())

    def test_one_validated_box_validates_the_image(self):
        make_beetle(image=self.image, bbox="validated")
        self.assertTrue(self.validated())

    def test_an_unvalidated_box_keeps_the_image_unvalidated(self):
        make_beetle(image=self.image, bbox="validated")
        make_beetle(image=self.image, bbox="unvalidated")
        self.assertFalse(self.validated())

    def test_validating_the_last_box_flips_the_image(self):
        box = make_beetle(image=self.image, bbox="unvalidated")
        self.assertFalse(self.validated())
        box.bbox_is_validated = True
        box.save()
        self.assertTrue(self.validated())

    def test_adding_an_unvalidated_box_invalidates_the_image(self):
        make_beetle(image=self.image, bbox="validated")
        self.assertTrue(self.validated())
        make_beetle(image=self.image, bbox="unvalidated")
        self.assertFalse(self.validated())

    def test_soft_deleting_the_last_unvalidated_box_validates_the_image(self):
        make_beetle(image=self.image, bbox="validated")
        pending = make_beetle(image=self.image, bbox="unvalidated")
        self.assertFalse(self.validated())
        pending.delete()
        self.assertTrue(self.validated())

    def test_soft_deleting_every_box_unvalidates_the_image(self):
        box = make_beetle(image=self.image, bbox="validated")
        self.assertTrue(self.validated())
        box.delete()
        self.assertFalse(self.validated())

    def test_hard_deleting_a_box_recalculates_too(self):
        make_beetle(image=self.image, bbox="validated")
        pending = make_beetle(image=self.image, bbox="unvalidated")
        Beetles.objects.filter(pk=pending.pk).delete()
        self.assertTrue(self.validated())

    def test_a_beetle_with_no_image_is_fine(self):
        Beetles.objects.create(collection_country="USA")  # must not raise


# --------------------------------------------------------------------------
# Beetles.save() taxon syncing and small helpers
# --------------------------------------------------------------------------
class BeetleTaxonSyncTests(TestCase):
    """Beetles.save() keeps the taxon link in step with depicts_valid_name_id."""

    @classmethod
    def setUpTestData(cls):
        cls.ips = make_taxon("T-IPS", scientific_name="Ips typographus", genus="Ips")
        cls.xyl = make_taxon("T-XYL", scientific_name="Xyleborus affinis", genus="Xyleborus")

    def test_links_the_taxon_with_that_id(self):
        beetle = Beetles.objects.create(depicts_valid_name_id="T-IPS")
        beetle.refresh_from_db()
        self.assertEqual(beetle.taxon, self.ips)

    def test_unknown_id_leaves_no_taxon(self):
        beetle = Beetles.objects.create(depicts_valid_name_id="NOPE")
        beetle.refresh_from_db()
        self.assertIsNone(beetle.taxon)

    def test_changing_the_id_relinks_the_taxon(self):
        beetle = Beetles.objects.create(depicts_valid_name_id="T-IPS")
        beetle.depicts_valid_name_id = "T-XYL"
        beetle.save()
        beetle.refresh_from_db()
        self.assertEqual(beetle.taxon, self.xyl)

    def test_clearing_the_id_clears_the_taxon(self):
        beetle = Beetles.objects.create(depicts_valid_name_id="T-IPS")
        beetle.depicts_valid_name_id = ""
        beetle.save()
        beetle.refresh_from_db()
        self.assertIsNone(beetle.taxon)

    def test_a_taxon_passed_without_an_id_is_dropped(self):
        # The string ID is the source of truth; see make_beetle(taxon=...).
        beetle = Beetles.objects.create(taxon=self.ips)
        beetle.refresh_from_db()
        self.assertIsNone(beetle.taxon)

    def test_soft_delete_does_not_touch_the_taxon(self):
        beetle = Beetles.objects.create(depicts_valid_name_id="T-IPS")
        beetle.delete()
        beetle.refresh_from_db()
        self.assertEqual(beetle.taxon, self.ips)


class BeetleHelperTests(SimpleTestCase):
    def test_has_bbox_is_true_once_a_box_exists(self):
        self.assertTrue(Beetles(bbox_x=0.25).has_bbox())

    def test_has_bbox_is_true_for_a_box_at_the_left_edge(self):
        self.assertTrue(Beetles(bbox_x=0.0).has_bbox())  # 0.0 is a position, not "missing"

    def test_has_bbox_is_false_without_a_box(self):
        self.assertFalse(Beetles().has_bbox())


class FilePathHelperTests(SimpleTestCase):
    """Content-addressed storage paths are built from the first four hex characters."""

    SHA = "abcdef0123456789" * 4

    def test_shard_is_the_first_two_pairs_lowercased(self):
        self.assertEqual(ImageAsset.shard_from_sha("ABCDEF00"), ("ab", "cd"))

    def test_shard_of_nothing_is_empty(self):
        self.assertEqual(ImageAsset.shard_from_sha(""), ("", ""))
        self.assertEqual(ImageAsset.shard_from_sha(None), ("", ""))

    def test_display_path(self):
        self.assertEqual(
            ImageAsset.path_for_display(self.SHA), f"display/ab/cd/{self.SHA}.jpg"
        )

    def test_original_path_normalises_the_extension(self):
        self.assertEqual(
            Beetles.path_for_original(self.SHA, ".TIF"), f"originals/ab/cd/{self.SHA}.tif"
        )
        self.assertEqual(
            Beetles.path_for_original(self.SHA, ""), f"originals/ab/cd/{self.SHA}.bin"
        )

    def test_thumbnail_path_is_webp_unless_asked_otherwise(self):
        self.assertEqual(
            Beetles.path_for_thumb96(self.SHA), f"thumbnails/ab/cd/{self.SHA}_96.webp"
        )
        self.assertEqual(
            Beetles.path_for_thumb96(self.SHA, webp=False), f"thumbnails/ab/cd/{self.SHA}_96.jpg"
        )


# --------------------------------------------------------------------------
# Image locks
# --------------------------------------------------------------------------
class ImageLockTests(TestCase):
    """A lock stops being honoured after LOCK_TIMEOUT_MINUTES without activity."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user("annotator", password="pw")
        cls.other = User.objects.create_user("other", password="pw")

    def make_lock(self, minutes_old=0):
        lock = ImageLock.objects.create(image_asset=make_image(), locked_by=self.user)
        if minutes_old:
            # update() skips auto_now, so we can pretend the last activity was earlier.
            ImageLock.objects.filter(pk=lock.pk).update(
                updated_at=timezone.now() - timedelta(minutes=minutes_old)
            )
            lock.refresh_from_db()
        return lock

    def test_default_timeout_is_five_minutes(self):
        self.assertEqual(ImageLock.LOCK_TIMEOUT_MINUTES, 5)

    def test_fresh_lock_is_not_expired(self):
        self.assertFalse(self.make_lock().is_expired())

    def test_lock_just_inside_the_timeout_is_not_expired(self):
        self.assertFalse(self.make_lock(minutes_old=4).is_expired())

    def test_lock_past_the_timeout_is_expired(self):
        self.assertTrue(self.make_lock(minutes_old=6).is_expired())

    def test_expiry_follows_the_configured_timeout(self):
        with mock.patch.object(ImageLock, "LOCK_TIMEOUT_MINUTES", 1):
            self.assertTrue(self.make_lock(minutes_old=2).is_expired())
            self.assertFalse(self.make_lock().is_expired())

    def test_cleanup_removes_only_expired_locks_and_counts_them(self):
        fresh = self.make_lock(minutes_old=1)
        self.make_lock(minutes_old=10)
        self.make_lock(minutes_old=60)

        self.assertEqual(ImageLock.cleanup_expired_locks(), 2)
        self.assertEqual(list(ImageLock.objects.values_list("pk", flat=True)), [fresh.pk])

    def test_cleanup_with_nothing_expired_removes_nothing(self):
        self.make_lock()
        self.assertEqual(ImageLock.cleanup_expired_locks(), 0)
        self.assertEqual(ImageLock.objects.count(), 1)

    def test_cleanup_uses_the_configured_timeout(self):
        self.make_lock(minutes_old=10)
        stale = self.make_lock(minutes_old=60)
        with mock.patch.object(ImageLock, "LOCK_TIMEOUT_MINUTES", 30):
            self.assertEqual(ImageLock.cleanup_expired_locks(), 1)
        self.assertFalse(ImageLock.objects.filter(pk=stale.pk).exists())
        self.assertEqual(ImageLock.objects.count(), 1)

    def test_an_image_can_only_be_locked_once(self):
        lock = self.make_lock()
        with self.assertRaises(IntegrityError), transaction.atomic():
            ImageLock.objects.create(image_asset=lock.image_asset, locked_by=self.other)


# --------------------------------------------------------------------------
# Upload / update batch status changes
# --------------------------------------------------------------------------
class MediaRootMixin:
    """Point MEDIA_ROOT at a throwaway folder so batch files never touch real media."""

    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.enterContext(override_settings(MEDIA_ROOT=tmp.name))

    @staticmethod
    def folder(field_file):
        """The lifecycle folder a stored file sits in, e.g. 'uploads/validated'."""
        return "/".join(Path(field_file.name).parts[:2])


CSV_BYTES = b"a,b\n1,2\n"
ZIP_BYTES = b"not-really-a-zip"


class UploadBatchStatusTests(MediaRootMixin, TestCase):
    """staging -> validating -> validated | rejected -> imported | import_failed"""

    def make_batch(self, with_zip=True):
        return UploadBatch.objects.create(
            original_filename="metadata.csv",
            file=SimpleUploadedFile("metadata.csv", CSV_BYTES),
            zip_file=SimpleUploadedFile("images.zip", ZIP_BYTES) if with_zip else "",
        )

    def test_new_batch_starts_in_staging_with_its_files_saved(self):
        batch = self.make_batch()
        self.assertEqual(batch.status, UploadBatch.Status.STAGING)
        self.assertEqual(self.folder(batch.file), "uploads/staging")
        self.assertEqual(Path(batch.file.path).read_bytes(), CSV_BYTES)
        self.assertEqual(Path(batch.zip_file.path).read_bytes(), ZIP_BYTES)

    def test_sha256_is_computed_from_the_stored_csv(self):
        batch = self.make_batch()
        digest = batch.compute_sha256_from_disk()
        self.assertEqual(digest, hashlib.sha256(CSV_BYTES).hexdigest())
        self.assertEqual(batch.sha256, digest)

    def test_mark_validating(self):
        batch = self.make_batch()
        batch.mark_validating()
        batch.refresh_from_db()
        self.assertEqual(batch.status, UploadBatch.Status.VALIDATING)

    def test_validated_moves_the_files_and_stamps_the_time(self):
        batch = self.make_batch()
        old_csv, old_zip = batch.file.path, batch.zip_file.path
        batch.error_message = "left over from an earlier attempt"
        batch.save()

        batch.mark_validated_and_move()
        batch.refresh_from_db()

        self.assertEqual(batch.status, UploadBatch.Status.VALIDATED)
        self.assertTrue(just_now(batch.validated_at))
        self.assertEqual(batch.error_message, "")
        self.assertEqual(self.folder(batch.file), "uploads/validated")
        self.assertEqual(self.folder(batch.zip_file), "uploads/validated")
        self.assertEqual(Path(batch.file.path).read_bytes(), CSV_BYTES)
        self.assertEqual(Path(batch.zip_file.path).read_bytes(), ZIP_BYTES)
        self.assertFalse(Path(old_csv).exists())
        self.assertFalse(Path(old_zip).exists())

    def test_rejected_moves_the_files_and_keeps_the_reason(self):
        batch = self.make_batch()
        batch.mark_rejected_and_move("row 3: unknown species")
        batch.refresh_from_db()

        self.assertEqual(batch.status, UploadBatch.Status.REJECTED)
        self.assertEqual(batch.error_message, "row 3: unknown species")
        self.assertEqual(self.folder(batch.file), "uploads/rejected")
        self.assertEqual(self.folder(batch.zip_file), "uploads/rejected")
        self.assertIsNone(batch.validated_at)

    def test_rejection_reason_is_cut_to_2000_characters(self):
        batch = self.make_batch()
        batch.mark_rejected_and_move("x" * 5000)
        batch.refresh_from_db()
        self.assertEqual(len(batch.error_message), 2000)

    def test_rejection_without_a_reason_stores_an_empty_message(self):
        batch = self.make_batch()
        batch.mark_rejected_and_move(None)
        batch.refresh_from_db()
        self.assertEqual(batch.error_message, "")

    def test_a_batch_without_a_zip_moves_fine(self):
        batch = self.make_batch(with_zip=False)
        batch.mark_validated_and_move()
        batch.refresh_from_db()
        self.assertEqual(self.folder(batch.file), "uploads/validated")
        self.assertFalse(batch.zip_file)

    def test_imported_archives_the_files_and_their_sidecars(self):
        batch = self.make_batch()
        batch.mark_validated_and_move()
        old_dir = Path(batch.file.path).parent
        (old_dir / "manifest.json").write_text("{}")

        batch.mark_imported_and_archive()
        batch.refresh_from_db()

        self.assertEqual(batch.status, UploadBatch.Status.IMPORTED)
        self.assertTrue(just_now(batch.imported_at))
        self.assertEqual(self.folder(batch.file), "uploads/archived")
        self.assertEqual(self.folder(batch.zip_file), "uploads/archived")
        new_dir = Path(batch.file.path).parent
        self.assertTrue((new_dir / "manifest.json").exists())
        self.assertFalse((old_dir / "manifest.json").exists())

    def test_import_failure_records_the_reason_and_leaves_the_files(self):
        batch = self.make_batch()
        old_csv = batch.file.path
        batch.mark_import_failed("database went away")
        batch.refresh_from_db()

        self.assertEqual(batch.status, UploadBatch.Status.IMPORT_FAILED)
        self.assertEqual(batch.error_message, "IMPORT ERROR: database went away")
        self.assertEqual(self.folder(batch.file), "uploads/staging")
        self.assertTrue(Path(old_csv).exists())

    def test_import_failure_can_move_the_files_aside(self):
        batch = self.make_batch()
        batch.mark_import_failed("boom", move_to_failed_folder=True)
        batch.refresh_from_db()
        self.assertEqual(self.folder(batch.file), "uploads/failed_import")
        self.assertEqual(self.folder(batch.zip_file), "uploads/failed_import")

    def test_import_failure_message_is_cut_to_2000_characters(self):
        batch = self.make_batch()
        batch.mark_import_failed("x" * 5000)
        batch.refresh_from_db()
        self.assertEqual(len(batch.error_message), 2000)
        self.assertTrue(batch.error_message.startswith("IMPORT ERROR: "))


class UpdateBatchStatusTests(MediaRootMixin, TestCase):
    """staging -> validating -> validated | rejected -> applied | apply_failed"""

    def make_batch(self):
        return UpdateBatch.objects.create(
            original_filename="updates.csv",
            file=SimpleUploadedFile("updates.csv", CSV_BYTES),
        )

    def test_new_batch_starts_in_staging(self):
        batch = self.make_batch()
        self.assertEqual(batch.status, UpdateBatch.Status.STAGING)
        self.assertEqual(self.folder(batch.file), "updates/staging")

    def test_sha256_is_computed_from_the_stored_csv(self):
        batch = self.make_batch()
        self.assertEqual(batch.compute_sha256_from_disk(), hashlib.sha256(CSV_BYTES).hexdigest())

    def test_mark_validating(self):
        batch = self.make_batch()
        batch.mark_validating()
        batch.refresh_from_db()
        self.assertEqual(batch.status, UpdateBatch.Status.VALIDATING)

    def test_validated_moves_the_file_and_stamps_the_time(self):
        batch = self.make_batch()
        old_csv = batch.file.path
        batch.error_message = "left over"
        batch.save()

        batch.mark_validated_and_move()
        batch.refresh_from_db()

        self.assertEqual(batch.status, UpdateBatch.Status.VALIDATED)
        self.assertTrue(just_now(batch.validated_at))
        self.assertEqual(batch.error_message, "")
        self.assertEqual(self.folder(batch.file), "updates/validated")
        self.assertEqual(Path(batch.file.path).read_bytes(), CSV_BYTES)
        self.assertFalse(Path(old_csv).exists())

    def test_rejected_moves_the_file_and_keeps_the_reason(self):
        batch = self.make_batch()
        batch.mark_rejected_and_move("unknown UUID")
        batch.refresh_from_db()
        self.assertEqual(batch.status, UpdateBatch.Status.REJECTED)
        self.assertEqual(batch.error_message, "unknown UUID")
        self.assertEqual(self.folder(batch.file), "updates/rejected")

    def test_rejection_reason_is_cut_to_2000_characters(self):
        batch = self.make_batch()
        batch.mark_rejected_and_move("x" * 5000)
        batch.refresh_from_db()
        self.assertEqual(len(batch.error_message), 2000)

    def test_applied_archives_the_file_and_stamps_the_time(self):
        batch = self.make_batch()
        batch.mark_validated_and_move()
        batch.mark_applied_and_archive()
        batch.refresh_from_db()

        self.assertEqual(batch.status, UpdateBatch.Status.APPLIED)
        self.assertTrue(just_now(batch.applied_at))
        self.assertEqual(self.folder(batch.file), "updates/archived")

    def test_apply_failure_records_the_reason_and_leaves_the_file(self):
        batch = self.make_batch()
        old_csv = batch.file.path
        batch.mark_apply_failed("row 2 changed underneath us")
        batch.refresh_from_db()

        self.assertEqual(batch.status, UpdateBatch.Status.APPLY_FAILED)
        self.assertEqual(batch.error_message, "APPLY ERROR: row 2 changed underneath us")
        self.assertEqual(self.folder(batch.file), "updates/staging")
        self.assertTrue(Path(old_csv).exists())

    def test_apply_failure_can_move_the_file_aside(self):
        batch = self.make_batch()
        batch.mark_apply_failed("boom", move_to_failed_folder=True)
        batch.refresh_from_db()
        self.assertEqual(self.folder(batch.file), "updates/failed_apply")

    def test_apply_failure_message_is_cut_to_2000_characters(self):
        batch = self.make_batch()
        batch.mark_apply_failed("x" * 5000)
        batch.refresh_from_db()
        self.assertEqual(len(batch.error_message), 2000)
        self.assertTrue(batch.error_message.startswith("APPLY ERROR: "))


# --------------------------------------------------------------------------
# Download jobs (pure helpers, no database)
# --------------------------------------------------------------------------
class DownloadJobTests(SimpleTestCase):
    def test_ids_round_trip_as_strings(self):
        job = DownloadJob()
        ids = [uuid.uuid4(), uuid.uuid4()]
        job.set_ids(ids)
        self.assertEqual(job.get_ids(), [str(i) for i in ids])

    def test_get_ids_with_nothing_or_bad_json_is_empty(self):
        self.assertEqual(DownloadJob().get_ids(), [])
        self.assertEqual(DownloadJob(selected_ids_json="{oops").get_ids(), [])

    def test_set_ids_with_something_unusable_stores_an_empty_list(self):
        job = DownloadJob()
        job.set_ids(None)
        self.assertEqual(job.get_ids(), [])

    def test_readable_query_of_nothing_is_a_dash(self):
        self.assertEqual(DownloadJob(query_string="").get_readable_query(), "—")
        self.assertEqual(DownloadJob(query_string="   ").get_readable_query(), "—")

    def test_readable_query_summarises_text_filters_and_ranges(self):
        query = json.dumps({
            "q": "bear",
            "filters": {"subfamily": ["Scolytinae", "Platypodinae"], "date_taken": ["2020"]},
            "ranges": {"size_min": "10", "res_max": "5"},
        })
        self.assertEqual(
            DownloadJob(query_string=query).get_readable_query(),
            "Text: “bear”; Subfamily: Scolytinae, Platypodinae; "
            "Date Taken: 2020; Size ≥ 10MB; Res ≤ 5",
        )

    def test_readable_query_with_no_criteria_says_all_records(self):
        self.assertEqual(
            DownloadJob(query_string='{"q": ""}').get_readable_query(), "All records"
        )

    def test_old_plain_text_queries_are_returned_as_they_are(self):
        self.assertEqual(DownloadJob(query_string="genus:Ips").get_readable_query(), "genus:Ips")

    def test_broken_json_falls_back_to_the_plain_text(self):
        self.assertEqual(DownloadJob(query_string="{not json").get_readable_query(), "{not json")
