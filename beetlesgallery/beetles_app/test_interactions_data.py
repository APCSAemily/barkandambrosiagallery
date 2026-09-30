"""The interactions data: the dataset importer and the proposal table."""
import io
import json
import tempfile
from pathlib import Path

from django.core.management import call_command
from django.db import IntegrityError, transaction
from django.test import TestCase

from beetlesgallery.beetles_app.models import InteractionProposal, PathogenInteraction

RECORDS = [
    {"Records ID": 1, "Beetle Host": "Ips typographus", "Beetle Host IDs": "1733", "categories": "Fungi",
     "pathogens": "Beauveria bassiana", "ecological relationship": "pathogen", "year": 2013,
     "source": "Andrei et al., 2013", "title": "Laboratory bioassays"},
    {"Records ID": 2, "Beetle Host": "Scolytus ventralis", "Beetle Host IDs": "1186", "categories": "Nematode",
     "pathogens": "Sulphuretylenchus elongatus", "ecological relationship": "parasite", "year": 1970},
]


def load(records, *flags):
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "records.json"
        path.write_text(json.dumps(records))
        out, err = io.StringIO(), io.StringIO()
        call_command("import_pathogen_interactions", "--file", str(path), *flags, stdout=out, stderr=err)
    return out.getvalue(), err.getvalue()


class DatasetImportTests(TestCase):
    def test_a_first_load_adds_every_record_as_dataset(self):
        out, _ = load(RECORDS)
        self.assertIn("2 added, 0 updated, 0 already there", out)
        self.assertEqual(PathogenInteraction.objects.filter(origin="dataset").count(), 2)

    def test_loading_again_does_not_duplicate(self):
        load(RECORDS)
        out, _ = load(RECORDS)
        self.assertIn("0 added, 0 updated, 2 already there", out)
        self.assertEqual(PathogenInteraction.objects.count(), 2)

    def test_loading_again_leaves_a_row_that_was_corrected_in_the_database_alone(self):
        load(RECORDS)
        row = PathogenInteraction.objects.get(record_number="1")
        row.pathogen = "Beauveria bassiana (corrected on the site)"
        row.save()
        out, _ = load(RECORDS)   # what every deploy does
        self.assertIn("0 added, 0 updated, 2 already there", out)
        row.refresh_from_db()
        self.assertEqual(row.pathogen, "Beauveria bassiana (corrected on the site)")

    def test_refresh_updates_a_record_from_the_file_in_place(self):
        load(RECORDS)
        before = PathogenInteraction.objects.get(record_number="1")
        corrected = [{**RECORDS[0], "pathogens": "Beauveria bassiana s.l."}, RECORDS[1]]
        out, _ = load(corrected, "--refresh")
        self.assertIn("0 added, 1 updated, 1 already there", out)
        after = PathogenInteraction.objects.get(record_number="1")
        self.assertEqual((after.id, after.pathogen), (before.id, "Beauveria bassiana s.l."))

    def test_clear_reloads_the_dataset_but_keeps_accepted_and_uploaded_rows(self):
        load(RECORDS)
        kept = [
            PathogenInteraction.objects.create(beetle_host="Ips typographus", pathogen="Ophiostoma", category="Fungi", origin="proposal"),
            PathogenInteraction.objects.create(beetle_host="Ips typographus", pathogen="Pityophthorus", category="Nematode", origin="upload"),
        ]
        out, _ = load(RECORDS, "--clear")
        self.assertIn("Cleared 2 published-dataset records", out)
        self.assertEqual(PathogenInteraction.objects.filter(origin="dataset").count(), 2)
        for row in kept:
            self.assertTrue(PathogenInteraction.objects.filter(pk=row.pk).exists())

    def test_extra_copies_from_an_earlier_import_are_reported_not_deleted(self):
        load(RECORDS)
        PathogenInteraction.objects.create(record_number="1", beetle_host="Ips typographus", pathogen="dup", category="Fungi")
        out, err = load(RECORDS)
        self.assertIn("1 extra copies", err)
        self.assertEqual(PathogenInteraction.objects.count(), 3)

    def test_a_repeated_records_id_in_the_file_is_loaded_once(self):
        _, err = load([RECORDS[0], RECORDS[0]])
        self.assertIn("appears more than once", err)
        self.assertEqual(PathogenInteraction.objects.count(), 1)


class ProposalModelTests(TestCase):
    def proposal(self, **changes):
        fields = dict(beetle_name="Ips typographus", partner_name="Ophiostoma polonicum",
                      source_key="10.1000/abc", score=0.7)
        fields.update(changes)
        return InteractionProposal.objects.create(**fields)

    def test_defaults_to_waiting_for_review(self):
        self.assertEqual(self.proposal().status, "proposed")

    def test_the_same_claim_from_the_same_source_is_stored_once_whatever_the_case(self):
        self.proposal()
        with self.assertRaises(IntegrityError), transaction.atomic():
            self.proposal(beetle_name="IPS TYPOGRAPHUS", partner_name="ophiostoma polonicum")

    def test_the_same_claim_from_another_source_is_another_proposal(self):
        self.proposal()
        self.proposal(source_key="10.1000/def")
        self.assertEqual(InteractionProposal.objects.count(), 2)

    def test_score_must_be_between_0_and_1(self):
        for bad in (-0.1, 1.5):
            with self.subTest(score=bad), self.assertRaises(IntegrityError), transaction.atomic():
                self.proposal(source_key=f"k{bad}", score=bad)
        self.proposal(source_key="no-score", score=None)

    def test_deleting_the_published_row_keeps_the_proposal(self):
        row = PathogenInteraction.objects.create(beetle_host="Ips typographus", pathogen="x", category="Fungi", origin="proposal")
        proposal = self.proposal(status="accepted", published_as=row)
        row.delete()
        proposal.refresh_from_db()
        self.assertEqual((proposal.status, proposal.published_as), ("accepted", None))
