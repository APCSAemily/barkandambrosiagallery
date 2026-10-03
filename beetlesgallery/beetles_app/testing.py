"""
Small builders shared by the logic and page tests (issue #206, parts 2 and 3).

Deliberately not named ``test_*.py`` so Django's test discovery does not treat
it as a test module.
"""
import itertools
import tempfile

from django.core.cache import cache
from django.test import override_settings

from beetlesgallery.beetles_app.models import Beetles, ImageAsset, Taxon
from beetlesgallery.beetles_app.test_pages import LOCMEM_CACHE, PageTestCase

_seq = itertools.count(1)

# A valid normalised bounding box; any non-null bbox_x makes a Beetles row an ROI.
BBOX = {"bbox_x": 0.1, "bbox_y": 0.1, "bbox_width": 0.2, "bbox_height": 0.2}


def make_taxon(valid_species_id=None, **fields):
    """Create a Taxon (a treebeard root node). Only the fields you pass are set."""
    n = next(_seq)
    fields.setdefault("scientific_name", f"Testus species{n}")
    return Taxon.add_root(valid_species_id=valid_species_id or f"TEST-{n}", **fields)


def make_image(**fields):
    """Create an ImageAsset. Pass any ImageAsset field as a keyword."""
    n = next(_seq)
    fields.setdefault("full_path_at_import", f"tests/img_{n}.jpg")
    return ImageAsset.objects.create(**fields)


def make_beetle(image=None, taxon=None, bbox=None, **fields):
    """Create a Beetles row (a specimen / region of interest).

    image: an ImageAsset to attach to; a fresh one is created if omitted.
    taxon: a Taxon. Beetles.save() links the taxon through depicts_valid_name_id,
           so this sets that ID for you (passing taxon= alone would be reset).
    bbox:  None for no box, "validated" or "unvalidated" for a box in that state.
    Any other keyword is a Beetles field.
    """
    if image is None:
        image = make_image()
    if taxon is not None:
        fields["depicts_valid_name_id"] = taxon.valid_species_id
    if bbox is not None:
        fields.update(BBOX)
        fields["bbox_is_validated"] = bbox == "validated"
    return Beetles.objects.create(image_asset=image, **fields)


@override_settings(
    CACHES=LOCMEM_CACHE,
    # The production hasher is deliberately slow (it dominated the run time here).
    PASSWORD_HASHERS=["django.contrib.auth.hashers.MD5PasswordHasher"],
)
class PageBehaviourCase(PageTestCase):
    """PageTestCase (one user per access level, plain static files) plus an
    empty cache, a throwaway MEDIA_ROOT and a fast password hasher."""

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.media_root = tmp.name
        self.enterContext(override_settings(MEDIA_ROOT=tmp.name))
