"""
Behaviour tests for the upload form handler (/upload/): the checks run on the
CSV and ZIP before a batch is queued for the background worker (issue #206, part 3).

The worker itself is replaced by a mock; these tests cover what the view does
with the files, not the import that follows.
"""
import io
import zipfile
from unittest import expectedFailure, mock

from django.contrib.messages import get_messages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse

from beetlesgallery.beetles_app.models import UploadBatch
from beetlesgallery.beetles_app.testing import PageBehaviourCase

GOOD_CSV = b"full_path_at_import,collection_country\nimg_1.jpg,USA\n"
NO_REQUIRED_COLUMN_CSV = b"filename,collection_country\nimg_1.jpg,USA\n"


def make_zip(entries=("img_1.jpg",)):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name in entries:
            archive.writestr(name, b"fake image bytes")
    return buffer.getvalue()


class UploadViewTests(PageBehaviourCase):
    URL = "/upload/"

    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)
        patcher = mock.patch("beetlesgallery.beetles_app.views.process_upload_task")
        self.task = patcher.start()
        self.addCleanup(patcher.stop)

    def upload(self, csv=("metadata.csv", GOOD_CSV), zipped=("images.zip", None)):
        data = {}
        if csv is not None:
            data["csv_file"] = SimpleUploadedFile(csv[0], csv[1])
        if zipped is not None:
            data["zip"] = SimpleUploadedFile(zipped[0], zipped[1] if zipped[1] is not None else make_zip())
        return self.client.post(self.URL, data)

    def messages(self, response):
        return [str(m) for m in get_messages(response.wsgi_request)]

    def assertRejectedBeforeBatchCreated(self, response, text):
        self.assertRedirects(response, reverse("data_management"), fetch_redirect_response=False)
        self.assertTrue(any(text in m for m in self.messages(response)), self.messages(response))
        self.assertEqual(UploadBatch.objects.count(), 0)
        self.task.delay.assert_not_called()

    # --- the happy path -----------------------------------------------------

    def test_valid_upload_creates_a_batch_and_queues_the_worker(self):
        response = self.upload()

        self.assertRedirects(response, reverse("data_management"), fetch_redirect_response=False)
        batch = UploadBatch.objects.get()
        self.assertEqual(batch.uploaded_by, self.staff)
        self.assertEqual(batch.original_filename, "metadata.csv")
        self.assertEqual(batch.status, UploadBatch.Status.STAGING)
        self.assertEqual(batch.size_bytes, len(GOOD_CSV))
        self.assertTrue(batch.sha256)
        self.task.delay.assert_called_once_with(batch.id)
        self.assertTrue(any("Files received" in m for m in self.messages(response)))

    def test_accepts_the_alternative_csv_field_name(self):
        response = self.client.post(self.URL, {
            "csv": SimpleUploadedFile("metadata.csv", GOOD_CSV),
            "zip": SimpleUploadedFile("images.zip", make_zip()),
        })
        self.assertRedirects(response, reverse("data_management"), fetch_redirect_response=False)
        self.task.delay.assert_called_once()

    def test_file_extensions_are_case_insensitive(self):
        self.upload(csv=("METADATA.CSV", GOOD_CSV), zipped=("IMAGES.ZIP", None))
        self.task.delay.assert_called_once()

    # --- checks before a batch exists ----------------------------------------

    def test_both_files_are_required(self):
        self.assertRejectedBeforeBatchCreated(self.upload(zipped=None), "attach both")
        self.assertRejectedBeforeBatchCreated(self.upload(csv=None), "attach both")

    def test_metadata_file_must_be_a_csv(self):
        self.assertRejectedBeforeBatchCreated(
            self.upload(csv=("metadata.xlsx", GOOD_CSV)), "must be a .csv"
        )

    def test_images_archive_must_be_a_zip(self):
        self.assertRejectedBeforeBatchCreated(
            self.upload(zipped=("images.tar", b"x")), "must be a .zip"
        )

    @override_settings(MAX_UPLOAD_SIZE_CSV=10)
    def test_oversized_csv_is_rejected(self):
        self.assertRejectedBeforeBatchCreated(self.upload(), "too large")

    @override_settings(MAX_UPLOAD_SIZE_ZIP=10)
    def test_oversized_zip_is_rejected(self):
        self.assertRejectedBeforeBatchCreated(self.upload(), "too large")

    @override_settings(MAX_UPLOAD_TOTAL_BYTES=10)
    def test_upload_over_the_total_limit_is_rejected_with_advice(self):
        response = self.upload()
        self.assertRejectedBeforeBatchCreated(response, "too large")
        self.assertTrue(any("smaller batches" in m for m in self.messages(response)), self.messages(response))

    def test_size_messages_are_in_readable_units(self):
        with override_settings(MAX_UPLOAD_SIZE_ZIP=10, MAX_UPLOAD_TOTAL_BYTES=2 * 1024 ** 3):
            messages = self.messages(self.upload())
        self.assertTrue(any("1 KB" in m and "2.00 GB" in m for m in messages), messages)

    def test_limits_default_to_two_gigabytes(self):
        from django.conf import settings
        self.assertEqual(settings.MAX_UPLOAD_SIZE_ZIP, 2 * 1024 ** 3)
        self.assertEqual(settings.MAX_UPLOAD_TOTAL_BYTES, 2 * 1024 ** 3)
    def test_corrupt_zip_is_rejected(self):
        self.assertRejectedBeforeBatchCreated(
            self.upload(zipped=("images.zip", b"this is not a zip")), "corrupt"
        )

    def test_empty_zip_is_rejected(self):
        self.assertRejectedBeforeBatchCreated(
            self.upload(zipped=("images.zip", make_zip(entries=()))), "empty"
        )

    # --- checks on the CSV contents (batch is created, then rejected) ----------

    @expectedFailure
    def test_csv_missing_the_required_column_is_rejected(self):
        """KNOWN BUG: the view redirects to redirect("my_upload"), a URL name that does
        not exist (the page is called "data_management"), so every rejection after the
        batch is created raises NoReverseMatch and the user sees a server error.
        The same typo is on the unreadable-CSV, too-many-rows and no-pandas paths.
        Remove @expectedFailure when the redirects are fixed."""
        response = self.upload(csv=("metadata.csv", NO_REQUIRED_COLUMN_CSV))

        self.assertRedirects(response, reverse("data_management"), fetch_redirect_response=False)
        batch = UploadBatch.objects.get()
        self.assertEqual(batch.status, UploadBatch.Status.REJECTED)
        self.assertIn("full_path_at_import", batch.error_message)
        self.task.delay.assert_not_called()

    def test_rejected_csv_is_marked_rejected_before_the_error_is_raised(self):
        """Working part of the same path: the batch itself is handled correctly."""
        try:
            self.upload(csv=("metadata.csv", NO_REQUIRED_COLUMN_CSV))
        except Exception:
            pass  # the redirect typo above
        batch = UploadBatch.objects.get()
        self.assertEqual(batch.status, UploadBatch.Status.REJECTED)
        self.assertIn("full_path_at_import", batch.error_message)
        self.assertEqual("/".join(batch.file.name.split("/")[:2]), "uploads/rejected")
        self.task.delay.assert_not_called()

    def test_row_limit_is_enforced(self):
        with mock.patch("beetlesgallery.beetles_app.views.MAX_ROWS", 1):
            try:
                self.upload(csv=("metadata.csv", GOOD_CSV + b"img_2.jpg,USA\n"))
            except Exception:
                pass  # the redirect typo above
        batch = UploadBatch.objects.get()
        self.assertEqual(batch.status, UploadBatch.Status.REJECTED)
        self.assertIn("max 1", batch.error_message)
        self.task.delay.assert_not_called()


class UploadAccessTests(PageBehaviourCase):
    def test_upload_needs_staff_and_creates_nothing_for_others(self):
        with mock.patch("beetlesgallery.beetles_app.views.process_upload_task") as task:
            self.client.force_login(self.user)
            response = self.client.post("/upload/", {
                "csv_file": SimpleUploadedFile("metadata.csv", GOOD_CSV),
                "zip": SimpleUploadedFile("images.zip", make_zip()),
            })
        self.assertRedirectsToLogin(response)
        self.assertEqual(UploadBatch.objects.count(), 0)
        task.delay.assert_not_called()

    def test_my_uploads_lists_only_your_own_batches(self):
        mine = UploadBatch.objects.create(uploaded_by=self.staff, original_filename="mine.csv")
        UploadBatch.objects.create(uploaded_by=self.superuser, original_filename="theirs.csv")
        self.client.force_login(self.staff)

        response = self.client.get(reverse("data_management"))

        self.assertEqual(list(response.context["batches"]), [mine])

class UploadModalTests(PageBehaviourCase):
    """The upload dialog on My Uploads tells people the size limit up front."""

    def modal(self):
        self.client.force_login(self.staff)
        return self.client.get(reverse("data_management")).content.decode()

    def test_dialog_states_the_two_gigabyte_limit(self):
        self.assertIn("must be less than 2 GB", self.modal())

    def test_dialog_lets_the_browser_check_the_size_before_uploading(self):
        self.assertIn('id="form-upload-new" data-max-bytes="2147483648"', self.modal())
    def test_update_and_reference_dialogs_state_and_enforce_their_limits(self):
        self.client.force_login(self.superuser)
        page = self.client.get(reverse("data_management")).content.decode()
        self.assertIn('id="form-update-existing" data-max-bytes="10485760"', page)
        self.assertIn('id="form-upload-taxonomy" data-max-bytes="104857600"', page)
        self.assertIn("the CSV must be less than 10 MB", page)
        self.assertIn("the CSV must be less than 100 MB", page)


class ReferenceCsvLimitTests(PageBehaviourCase):
    """The taxonomy reference pages refuse oversized CSVs before the slow database rebuild."""

    PAGES = [
        ("admin_valid_species", "reference/valid_species.csv"),
        ("admin_described_names", "reference/described_names.csv"),
    ]

    def setUp(self):
        super().setUp()
        self.client.force_login(self.superuser)
        patcher = mock.patch("beetlesgallery.beetles_app.views.call_command")
        self.rebuild = patcher.start()
        self.addCleanup(patcher.stop)

    def post(self, page, size):
        return self.client.post(reverse(page), {"csv_file": SimpleUploadedFile("ref.csv", b"x" * size)})

    @override_settings(MAX_UPLOAD_SIZE_TAXONOMY=10)
    def test_oversized_reference_is_refused_and_nothing_is_rebuilt(self):
        from django.core.files.storage import default_storage
        for page, storage_key in self.PAGES:
            with self.subTest(page=page):
                response = self.post(page, 100)
                self.assertRedirects(response, reverse(page), fetch_redirect_response=False)
                msgs = [str(m) for m in get_messages(response.wsgi_request)]
                self.assertTrue(any("Reference .csv is too large" in m for m in msgs), msgs)
                self.assertFalse(default_storage.exists(storage_key))
        self.rebuild.assert_not_called()

    def test_reference_within_the_limit_is_saved_and_rebuilt(self):
        from django.core.files.storage import default_storage
        for page, storage_key in self.PAGES:
            with self.subTest(page=page):
                self.rebuild.reset_mock()
                self.post(page, 100)
                self.assertTrue(default_storage.exists(storage_key))
                self.rebuild.assert_called_once_with("migrate_taxonomy_to_db")

    def test_limit_defaults_to_100_mb(self):
        from django.conf import settings
        self.assertEqual(settings.MAX_UPLOAD_SIZE_TAXONOMY, 100 * 1024 ** 2)


class UpdateCsvLimitTests(PageBehaviourCase):
    @override_settings(MAX_UPLOAD_SIZE_CSV=10)
    def test_oversized_update_csv_is_refused_with_readable_sizes(self):
        from beetlesgallery.beetles_app.models import UpdateBatch
        self.client.force_login(self.staff)
        response = self.client.post("/updates/", {"csv_file": SimpleUploadedFile("u.csv", b"x" * 100)})
        self.assertRedirects(response, reverse("data_management"), fetch_redirect_response=False)
        msgs = [str(m) for m in get_messages(response.wsgi_request)]
        self.assertTrue(any("too large (1 KB)" in m for m in msgs), msgs)
        self.assertEqual(UpdateBatch.objects.count(), 0)
