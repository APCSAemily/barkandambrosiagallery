from django.test import TestCase

from beetlesgallery.beetles_app.interaction_names import SpeciesLookup, clean_doi
from beetlesgallery.beetles_app.models import Synonym
from beetlesgallery.beetles_app.testing import make_taxon


class NamesTests(TestCase):
    def setUp(self):
        self.typo = make_taxon(valid_species_id="1733", genus="Ips", species="typographus", scientific_name="Ips typographus")
        self.affinis = make_taxon(valid_species_id="2210", genus="Xyleborus", species="affinis", scientific_name="Xyleborus affinis")
        Synonym.objects.create(taxon=self.typo, name_id="s1", described_scientific_name="Bostrichus typographus")

    def test_doi(self):
        self.assertEqual(clean_doi("https://doi.org/10.1000/ABC "), "10.1000/abc")
        self.assertEqual(clean_doi("doi: 10.1000/abc"), "10.1000/abc")
        self.assertEqual(clean_doi(None), "")

    def test_a_valid_name_a_synonym_and_an_id(self):
        lookup = SpeciesLookup()
        self.assertEqual(lookup.find("ips  TYPOGRAPHUS")[0].valid_species_id, "1733")
        self.assertEqual(lookup.find("Bostrichus typographus")[0].valid_species_id, "1733")
        self.assertEqual(lookup.find("whatever", "2210")[0].valid_species_id, "2210")

    def test_unknown_names_and_ids_are_errors(self):
        lookup = SpeciesLookup()
        self.assertIn("not in the species list", lookup.find("Nope nope")[1])
        self.assertIn("'9999'", lookup.find("Ips typographus", "9999")[1])

    def test_a_synonym_shared_by_two_species_is_ambiguous_and_a_synonym_never_beats_a_valid_name(self):
        Synonym.objects.create(taxon=self.typo, name_id="s2", described_scientific_name="Shared name")
        Synonym.objects.create(taxon=self.affinis, name_id="s3", described_scientific_name="Shared name")
        Synonym.objects.create(taxon=self.affinis, name_id="s4", described_scientific_name="Ips typographus")
        lookup = SpeciesLookup()
        self.assertIn("not in the species list", lookup.find("Shared name")[1])
        self.assertEqual(lookup.find("Ips typographus")[0].valid_species_id, "1733")
