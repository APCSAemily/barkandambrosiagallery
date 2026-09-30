"""Classify with AI on the annotation page."""
import io
from unittest import mock

import requests
from django.core.files.base import ContentFile
from PIL import Image

from beetlesgallery.beetles_app.classify_assist import iou, to_fractions
from beetlesgallery.beetles_app.models import Beetles, ImageLock, ModelPrediction
from beetlesgallery.beetles_app.testing import PageBehaviourCase, make_beetle, make_image, make_taxon


def fake_response(detections, status=200, body_status="success"):
    response = mock.Mock(status_code=status)
    response.json.return_value = {
        "status": body_status, "detections": detections, "model_used": "ibbi-test",
        "class_names": ["Xyleborus affinis", "Ips typographus", "Unlisted species"],
    }
    return response


DETECTION = {"box": [100, 50, 300, 250], "score": 0.91, "label": "Xyleborus_affinis", "probs": [0.91, 0.06, 0.03]}


class ClassifyCase(PageBehaviourCase):
    def setUp(self):
        super().setUp()
        self.affinis = make_taxon(valid_species_id="2210", genus="Xyleborus", species="affinis", scientific_name="Xyleborus affinis")
        self.typo = make_taxon(valid_species_id="1733", genus="Ips", species="typographus", scientific_name="Ips typographus")
        self.asset = make_image(image_width=1000, image_height=500)
        buffer = io.BytesIO()
        Image.new("RGB", (1000, 500), "white").save(buffer, "JPEG")
        self.asset.image_file.save("a.jpg", ContentFile(buffer.getvalue()))
        self.url = f"/api/v1/image-assets/{self.asset.id}/classify/"
        self.client.force_login(self.staff)

    def classify(self, detections=(DETECTION,), **kwargs):
        with mock.patch("requests.post", return_value=fake_response(list(detections), **kwargs)) as post:
            response = self.client.post(self.url, {"architecture": "rtdetr"}, content_type="application/json")
        return response, post


class BoxMathTests(ClassifyCase):
    def test_pixels_become_clamped_fractions(self):
        self.assertEqual(to_fractions([100, 50, 300, 250], 1000, 500), (0.1, 0.1, 0.2, 0.4))
        self.assertEqual(to_fractions([-20, -5, 1200, 600], 1000, 500), (0.0, 0.0, 1.0, 1.0))
        self.assertIsNone(to_fractions([10, 10, 10, 200], 1000, 500))

    def test_overlap(self):
        self.assertEqual(iou((0, 0, .5, .5), (0, 0, .5, .5)), 1.0)
        self.assertEqual(iou((0, 0, .2, .2), (.5, .5, .2, .2)), 0.0)


class ClassifyTests(ClassifyCase):
    def test_adds_unvalidated_rois_with_the_proposed_species(self):
        response, post = self.classify()
        self.assertEqual(response.status_code, 200, response.content)
        self.assertEqual(response.json()["added"], 1)
        roi = Beetles.objects.get(image_asset=self.asset)
        self.assertEqual((roi.bbox_x, roi.bbox_y, roi.bbox_width, roi.bbox_height), (0.1, 0.1, 0.2, 0.4))
        self.assertFalse(roi.bbox_is_validated)
        self.assertEqual((roi.depicts_valid_name_id, roi.bbox_created_by), ("2210", self.staff))
        self.asset.refresh_from_db()
        self.assertFalse(self.asset.is_validated)
        prediction = ModelPrediction.objects.get(roi=roi)
        self.assertEqual((prediction.valid_species_id, prediction.confidence, prediction.model_name), ("2210", 0.91, "annotator:ibbi-test"))
        self.assertEqual(prediction.top_k, [{"valid_species_id": "1733", "confidence": 0.06}])  # unlisted species left out
        self.assertEqual(post.call_args.kwargs["data"]["architecture"], "rtdetr")

    def test_existing_rois_are_kept_and_an_overlapping_box_is_skipped(self):
        existing = make_beetle(image=self.asset, taxon=self.typo, bbox="validated")
        existing.bbox_x, existing.bbox_y, existing.bbox_width, existing.bbox_height = 0.1, 0.1, 0.2, 0.4
        existing.save()
        other = {**DETECTION, "box": [600, 100, 800, 300]}
        response, _ = self.classify([DETECTION, other])
        self.assertEqual(response.json(), {"added": 1, "already_boxed": 1, "model": "ibbi-test"})
        self.assertEqual(Beetles.objects.filter(image_asset=self.asset).count(), 2)
        existing.refresh_from_db()
        self.assertEqual((existing.depicts_valid_name_id, existing.bbox_is_validated), ("1733", True))

    def test_running_twice_adds_nothing_the_second_time(self):
        self.classify()
        response, _ = self.classify()
        self.assertEqual(response.json()["added"], 0)
        self.assertEqual(Beetles.objects.filter(image_asset=self.asset).count(), 1)

    def test_an_unknown_species_still_gets_a_box_without_a_label(self):
        response, _ = self.classify([{**DETECTION, "label": "Not in our list"}])
        roi = Beetles.objects.get(image_asset=self.asset)
        self.assertIsNone(roi.depicts_valid_name_id)
        self.assertFalse(ModelPrediction.objects.exists())

    def test_service_problems_are_reported_and_nothing_is_added(self):
        for kwargs in ({"status": 500}, {"body_status": "error"}):
            response, _ = self.classify(**kwargs)
            self.assertEqual(response.status_code, 502)
        for error in (requests.exceptions.Timeout(), requests.exceptions.ConnectionError()):
            with mock.patch("requests.post", side_effect=error):
                self.assertEqual(self.client.post(self.url, {}, content_type="application/json").status_code, 502)
        self.assertFalse(Beetles.objects.filter(image_asset=self.asset).exists())

    def test_unknown_model_is_refused_before_calling_the_service(self):
        with mock.patch("requests.post") as post:
            response = self.client.post(self.url, {"architecture": "evil"}, content_type="application/json")
        self.assertEqual(response.status_code, 502)
        post.assert_not_called()

    def test_needs_the_annotate_area_and_respects_another_users_lock(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.post(self.url, {}, content_type="application/json").status_code, 403)
        self.client.force_login(self.staff)
        ImageLock.objects.create(image_asset=self.asset, locked_by=self.superuser)
        response, post = self.classify()
        self.assertEqual(response.status_code, 409)
        post.assert_not_called()

    def test_the_annotation_page_has_the_button(self):
        self.assertContains(self.client.get("/tools/annotate/"), "classifyCurrentImage")
