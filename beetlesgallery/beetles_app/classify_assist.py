"""
"Classify with AI" on the annotation page: ask the classifier for boxes and species, and add them as new ROIs.

The new ROIs are never validated: a person does that. A box that overlaps an existing ROI of the image is skipped,
so running it twice, or on an image that already has boxes, adds only what is new. Each proposed species is also
kept as a ModelPrediction, so the model's confidence and runners-up are not lost.
"""
import re

import requests
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from .bbox_rules import TOLERANCE
from .models import Beetles, ModelPrediction, Taxon

ARCHITECTURES = ("rtdetr", "yolov12", "yolov11", "yolov10", "yolov9", "yolov8")
SAME_BOX_IOU = 0.5


class ClassifyError(Exception):
    """A problem to show the annotator; nothing was added."""


def iou(a, b):
    """Overlap of two boxes given as (x, y, width, height)."""
    ax2, ay2, bx2, by2 = a[0] + a[2], a[1] + a[3], b[0] + b[2], b[1] + b[3]
    w = min(ax2, bx2) - max(a[0], b[0])
    h = min(ay2, by2) - max(a[1], b[1])
    if w <= 0 or h <= 0:
        return 0.0
    inter = w * h
    return inter / (a[2] * a[3] + b[2] * b[3] - inter)


def to_fractions(pixel_box, width, height):
    """(x1, y1, x2, y2) in pixels to (x, y, w, h) fractions clamped to the image, or None if it is empty."""
    x1, y1, x2, y2 = (float(v) for v in pixel_box)
    x1, x2 = max(0.0, min(x1, x2) / width), min(1.0, max(x1, x2) / width)
    y1, y2 = max(0.0, min(y1, y2) / height), min(1.0, max(y1, y2) / height)
    if x2 - x1 <= TOLERANCE or y2 - y1 <= TOLERANCE:
        return None
    return tuple(round(v, 6) for v in (x1, y1, x2 - x1, y2 - y1))


def _norm(name):
    return re.sub(r"\s+", " ", str(name or "").replace("_", " ")).strip().lower()


def call_classifier(image_bytes, filename, content_type, architecture, box_threshold):
    """The Modal service's answer for one image, or ClassifyError."""
    if architecture not in ARCHITECTURES:
        raise ClassifyError("Unknown model.")
    try:
        response = requests.post(
            settings.MODAL_API_URL, data={"architecture": architecture, "box_threshold": box_threshold},
            files={"image": (filename, image_bytes, content_type)}, timeout=300,
        )
    except requests.exceptions.Timeout:
        raise ClassifyError("The AI model is waking up (cold start). Please try again in a minute.")
    except requests.exceptions.RequestException:
        raise ClassifyError("The AI service could not be reached. Please try again later.")
    if response.status_code != 200:
        raise ClassifyError(f"The AI service returned an error ({response.status_code}).")
    data = response.json()
    if data.get("status") != "success":
        raise ClassifyError("The AI service could not process this image.")
    return data


def add_rois(asset, result, user):
    """Create unvalidated ROIs from the classifier's ``result``. Returns (created, skipped_as_existing)."""
    width, height = asset.image_width, asset.image_height
    if not (width and height):
        from PIL import Image
        with asset.image_file.open("rb") as fh:
            width, height = Image.open(fh).size
    names = {_norm(t.scientific_name): t for t in Taxon.objects.exclude(scientific_name__isnull=True)}
    class_names = result.get("class_names") or []
    model = result.get("model_used") or "classifier"
    now = timezone.now()

    existing = [
        (b.bbox_x, b.bbox_y, b.bbox_width, b.bbox_height)
        for b in Beetles.objects.filter(image_asset=asset, is_deleted=False, bbox_x__isnull=False)
    ]
    created, skipped = [], 0
    with transaction.atomic():
        for det in sorted(result.get("detections", []), key=lambda d: -float(d.get("score") or 0)):
            box = to_fractions(det.get("box") or [0, 0, 0, 0], width, height)
            if box is None:
                continue
            if any(iou(box, other) >= SAME_BOX_IOU for other in existing):
                skipped += 1
                continue
            taxon = names.get(_norm(det.get("label")))
            roi = Beetles.objects.create(
                image_asset=asset, bbox_x=box[0], bbox_y=box[1], bbox_width=box[2], bbox_height=box[3],
                bbox_is_validated=False, bbox_created_by=user, bbox_created_at=now, last_updated_by=user,
                depicts_valid_name_id=taxon.valid_species_id if taxon else None,
            )
            if taxon:
                probs = det.get("probs") or []
                runners = sorted(
                    ((names.get(_norm(n)), p) for n, p in zip(class_names, probs) if names.get(_norm(n)) not in (None, taxon)),
                    key=lambda item: -item[1],
                )[:5]
                ModelPrediction.objects.create(
                    roi=roi, valid_species_id=taxon.valid_species_id, taxon=taxon,
                    confidence=min(1.0, max(0.0, float(det.get("score") or 0))),
                    top_k=[{"valid_species_id": t.valid_species_id, "confidence": round(float(p), 4)} for t, p in runners],
                    model_name=f"annotator:{model}"[:100], uploaded_by=user,
                )
            existing.append(box)
            created.append(roi)
    return created, skipped


# --- Images submitted to the public classifier page ----------------------------------------------------------------
SAVED, DUPLICATE, NOT_SAVED, OPTED_OUT = "saved", "already_on_platform", "not_saved", "opted_out"
MAX_SUBMISSION_PIXELS = 100_000_000
MAX_SUBMISSION_BYTES = 25 * 1024 * 1024
SUBMISSIONS_PER_HOUR = 20


def save_classifier_submission(image_bytes, filename, result, user=None):
    """
    Keep an image that was sent to the classifier page and had at least one beetle found in it, as an
    unvalidated image with the proposed boxes and species (see add_rois).

    An image already on the platform (same bytes, by SHA-256, deleted ones included) is never replaced, touched or
    given new ROIs: the platform's copy is kept. Returns SAVED, DUPLICATE or NOT_SAVED (no beetle, or not a usable image).
    """
    import hashlib
    import io

    from django.db import IntegrityError
    from PIL import Image

    from .image_pipeline import write_original_and_thumb96
    from .models import ImageAsset

    if not result.get("detections") or len(image_bytes) > MAX_SUBMISSION_BYTES:
        return NOT_SAVED
    digest = hashlib.sha256(image_bytes).hexdigest()
    if ImageAsset.objects.filter(image_sha256=digest).exists():
        return DUPLICATE
    try:
        with Image.open(io.BytesIO(image_bytes)) as probe:
            if probe.width * probe.height > MAX_SUBMISSION_PIXELS:
                return NOT_SAVED
            probe.verify()
    except Exception:
        return NOT_SAVED

    saved = write_original_and_thumb96(digest, io.BytesIO(without_location(image_bytes)))
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename or "image")[:120]
    try:
        with transaction.atomic():
            asset = ImageAsset.objects.create(
                full_path_at_import=f"classifier/{digest[:16]}/{safe_name}",
                image_sha256=digest, image_size_bytes=len(image_bytes),
                image_file=saved["original_path"], thumb_small=saved["thumb_path"],
                image_width=saved["image_size"][0], image_height=saved["image_size"][1],
                thumb_width=saved["thumb_size"][0], thumb_height=saved["thumb_size"][1],
                image_notes="Submitted to the AI classifier; boxes and species are proposals to be checked.",
                last_updated_by=user if getattr(user, "is_authenticated", False) else None,
            )
            add_rois(asset, result, user if getattr(user, "is_authenticated", False) else None)
    except IntegrityError:
        return DUPLICATE  # the same image arrived twice at once
    return SAVED


def without_location(image_bytes):
    """The same image without GPS coordinates in its EXIF, so a photo's location is not published. Other bytes are returned as they were."""
    import io

    from PIL import Image

    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            exif = img.getexif()
            if 0x8825 not in exif or img.format != "JPEG":   # 0x8825 is the GPS block
                return image_bytes
            del exif[0x8825]
            out = io.BytesIO()
            img.save(out, "JPEG", exif=exif, quality="keep")
            return out.getvalue()
    except Exception:
        return image_bytes
