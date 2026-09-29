# Tests for the taxonomy rebuild (migrate_taxonomy_to_db) that the superuser reference pages run (issue #256, phase 3).
import io
import json
from contextlib import redirect_stdout

from django.contrib.messages import get_messages
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import CommandError, call_command
from django.db import IntegrityError
from django.urls import reverse

from beetlesgallery.beetles_app.models import CategoryMapping, Synonym, Taxon
from beetlesgallery.beetles_app.testing import PageBehaviourCase, make_beetle, make_taxon

SPECIES_KEY = "reference/valid_species.csv"
NAMES_KEY = "reference/described_names.csv"

SPECIES_HEADER = (
    "valid_species_id,scientificName,scientificNameAuthority,subfamily,tribe,subtribe,"
    "genus,species,subspecies,authority,authorityYear,originalGenus"
)
NAMES_HEADER = (
    "name_id,name_valid_species_id,describedScientificName,describedScientificNameAuthority,"
    "name_genus,name_species,name_subspecies,name_authority,name_year"
)

IPS = '3,Ips typographus,"Linnaeus, 1758",Scolytinae,Ipini,,Ips,typographus,,Linnaeus,1758,Bostrichus'
XYL = '9512,Xyleborus affinis,"Eichhoff, 1868",Scolytinae,Xyleborini,,Xyleborus,affinis,,Eichhoff,1868,Xyleborus'
IPS_NAMES = (
    '3,3,Ips typographus,"Linnaeus, 1758",Ips,typographus,,Linnaeus,1758',
    '17,3,Bostrichus typographus,"Linnaeus, 1758",Bostrichus,typographus,,Linnaeus,1758',
)


def csv_file(header, *rows, bom=True):
    # The real reference exports start with a UTF-8 byte order mark.
    return ("\ufeff" if bom else "").encode() + ("\n".join((header, *rows)) + "\n").encode()


def species(*rows, bom=True):
    return csv_file(SPECIES_HEADER, *rows, bom=bom)


def names(*rows):
    return csv_file(NAMES_HEADER, *rows)


def valid_ids():
    return sorted(Taxon.objects.values_list("valid_species_id", flat=True))


class TaxonomyRebuildCase(PageBehaviourCase):
    def store(self, key, content):
        if default_storage.exists(key):
            default_storage.delete(key)
        default_storage.save(key, ContentFile(content))

    def rebuild(self, species_csv=None, names_csv=None):
        if species_csv is not None:
            self.store(SPECIES_KEY, species_csv)
        if names_csv is not None:
            self.store(NAMES_KEY, names_csv)
        call_command("migrate_taxonomy_to_db", stdout=io.StringIO())


class RebuildTests(TaxonomyRebuildCase):
    def test_loads_every_species_column(self):
        self.rebuild(species(IPS, XYL), names())

        self.assertEqual(valid_ids(), ["3", "9512"])
        ips = Taxon.objects.get(valid_species_id="3")
        self.assertEqual(ips.scientific_name, "Ips typographus")
        self.assertEqual(ips.scientific_name_authority, "Linnaeus, 1758")
        self.assertEqual((ips.subfamily, ips.tribe, ips.genus, ips.species), ("Scolytinae", "Ipini", "Ips", "typographus"))
        self.assertEqual((ips.authority, ips.authority_year, ips.original_genus), ("Linnaeus", "1758", "Bostrichus"))

    def test_species_file_without_a_byte_order_mark_also_loads(self):
        self.rebuild(species(IPS, bom=False), names())
        self.assertEqual(valid_ids(), ["3"])

    def test_species_rows_without_an_id_are_skipped(self):
        self.rebuild(species(IPS, ",Nameless species,,,,,,,,,,"), names())
        self.assertEqual(valid_ids(), ["3"])

    def test_links_described_names_to_their_species(self):
        self.rebuild(species(IPS, XYL), names(*IPS_NAMES))

        self.assertEqual(
            sorted(Synonym.objects.filter(taxon__valid_species_id="3").values_list("described_scientific_name", flat=True)),
            ["Bostrichus typographus", "Ips typographus"],
        )
        old_name = Synonym.objects.get(name_id="17")
        self.assertEqual((old_name.genus, old_name.species, old_name.authority, old_name.year), ("Bostrichus", "typographus", "Linnaeus", "1758"))

    def test_described_names_for_unknown_species_or_without_ids_are_skipped(self):
        self.rebuild(species(IPS), names(IPS_NAMES[0], "99,4040,Unknown species,,,,,,", ",3,No name id,,,,,,"))
        self.assertEqual(list(Synonym.objects.values_list("name_id", flat=True)), ["3"])

    def test_described_names_in_latin_1_are_decoded(self):
        latin_1 = (NAMES_HEADER + "\n5,3,Hylésinus fraxini,,Hylésinus,fraxini,,,\n").encode("latin-1")
        self.rebuild(species(IPS), latin_1)
        self.assertEqual(Synonym.objects.get().described_scientific_name, "Hylésinus fraxini")

    def test_missing_described_names_file_still_loads_species(self):
        self.rebuild(species(IPS))
        self.assertEqual(valid_ids(), ["3"])
        self.assertEqual(Synonym.objects.count(), 0)

    def test_category_mappings_are_reloaded_from_the_reference_json(self):
        CategoryMapping.objects.create(category_id=7, name="stale")
        mapping = {"categories": [{"id": 0, "name": "ips", "full_name": "Ips typographus", "type": "beetle"}, {"id": 1}]}
        self.store("reference/category_mapping.json", json.dumps(mapping).encode())

        self.rebuild(species(IPS), names())

        self.assertEqual(
            list(CategoryMapping.objects.order_by("category_id").values_list("category_id", "name", "full_name", "supercategory")),
            [(0, "ips", "Ips typographus", "beetle"), (1, "class_1", "", "beetle")],
        )


class BeetleLinkTests(TaxonomyRebuildCase):
    def test_beetles_are_linked_to_their_species(self):
        beetle = make_beetle(depicts_valid_name_id="9512")
        self.assertIsNone(beetle.taxon)

        self.rebuild(species(IPS, XYL), names())

        beetle.refresh_from_db()
        self.assertEqual(beetle.taxon.valid_species_id, "9512")

    def test_beetles_whose_species_was_dropped_are_kept_but_unlinked(self):
        beetle = make_beetle(taxon=make_taxon("OLD-1"))

        self.rebuild(species(IPS), names())

        self.assertEqual(valid_ids(), ["3"])
        beetle.refresh_from_db()
        self.assertIsNone(beetle.taxon)
        self.assertEqual(beetle.depicts_valid_name_id, "OLD-1")

    def test_running_twice_gives_the_same_result(self):
        beetle = make_beetle(depicts_valid_name_id="3")

        def state():
            beetle.refresh_from_db()
            return valid_ids(), sorted(Synonym.objects.values_list("name_id", "taxon__valid_species_id")), beetle.taxon.valid_species_id

        self.rebuild(species(IPS, XYL), names(*IPS_NAMES))
        first = state()
        self.rebuild()
        self.assertEqual(state(), first)


class RebuildFailureTests(TaxonomyRebuildCase):
    def setUp(self):
        super().setUp()
        self.rebuild(species(IPS, XYL), names(*IPS_NAMES))
        self.beetle = make_beetle(depicts_valid_name_id="3")

    def assertOldTaxonomyKept(self):
        self.assertEqual(valid_ids(), ["3", "9512"])
        self.assertEqual(Synonym.objects.count(), 2)
        self.beetle.refresh_from_db()
        self.assertEqual(self.beetle.taxon.valid_species_id, "3")

    def test_duplicate_species_ids_roll_back_the_whole_rebuild(self):
        with self.assertRaises(IntegrityError):
            self.rebuild(species(IPS, IPS))
        self.assertOldTaxonomyKept()

    def test_species_file_without_the_id_column_keeps_the_old_taxonomy(self):
        with self.assertRaisesMessage(CommandError, "no rows with a valid_species_id"):
            self.rebuild(names(*IPS_NAMES))
        self.assertOldTaxonomyKept()

    def test_species_file_with_only_blank_ids_keeps_the_old_taxonomy(self):
        with self.assertRaisesMessage(CommandError, "no rows with a valid_species_id"):
            self.rebuild(species(",Nameless species,,,,,,,,,,"))
        self.assertOldTaxonomyKept()

    def test_missing_species_file_keeps_the_old_taxonomy(self):
        default_storage.delete(SPECIES_KEY)
        with self.assertRaisesMessage(CommandError, "The existing taxonomy was kept"):
            self.rebuild()
        self.assertOldTaxonomyKept()


class ReferencePageRebuildTests(TaxonomyRebuildCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.superuser)

    def upload(self, page, content):
        with redirect_stdout(io.StringIO()):
            response = self.client.post(reverse(page), {"csv_file": SimpleUploadedFile("reference.csv", content)})
        self.assertRedirects(response, reverse(page), fetch_redirect_response=False)
        return [str(m) for m in get_messages(response.wsgi_request)]

    def test_uploading_valid_species_rebuilds_the_taxonomy(self):
        beetle = make_beetle(depicts_valid_name_id="9512")

        messages = self.upload("admin_valid_species", species(IPS, XYL))

        self.assertTrue(any("successfully rebuilt" in m for m in messages), messages)
        self.assertEqual(valid_ids(), ["3", "9512"])
        beetle.refresh_from_db()
        self.assertEqual(beetle.taxon.valid_species_id, "9512")

    def test_uploading_described_names_rebuilds_the_synonyms(self):
        self.store(SPECIES_KEY, species(IPS))

        messages = self.upload("admin_described_names", names(*IPS_NAMES))

        self.assertTrue(any("successfully rebuilt" in m for m in messages), messages)
        self.assertEqual(Synonym.objects.count(), 2)

    def test_failed_rebuild_is_reported_and_keeps_the_old_taxonomy(self):
        self.rebuild(species(IPS), names())

        messages = self.upload("admin_valid_species", species(XYL, XYL))

        self.assertTrue(any("database rebuild failed" in m for m in messages), messages)
        self.assertEqual(valid_ids(), ["3"])

    def test_described_names_uploaded_on_the_species_page_are_refused(self):
        self.rebuild(species(IPS), names())

        messages = self.upload("admin_valid_species", names(*IPS_NAMES))

        self.assertTrue(any("database rebuild failed" in m and "existing taxonomy was kept" in m for m in messages), messages)
        self.assertEqual(valid_ids(), ["3"])
