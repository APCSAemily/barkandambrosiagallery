"""Names shared by the interaction importers: matching a beetle to the species list, and tidying a DOI."""
import re

from .models import Synonym, Taxon

DOI_PREFIX = re.compile(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", re.I)


def clean_doi(value):
    """'https://doi.org/10.1000/ABC ' and 'doi: 10.1000/abc' are both '10.1000/abc'."""
    return DOI_PREFIX.sub("", (value or "").strip()).strip().lower()


class SpeciesLookup:
    """Match a beetle name (valid or synonym) to a Taxon, loading the species list once."""

    def __init__(self):
        self.by_id = {t.valid_species_id: t for t in Taxon.objects.all().only("id", "valid_species_id", "scientific_name")}
        self.by_name = {}
        for taxon in self.by_id.values():
            if taxon.scientific_name:
                self.by_name[taxon.scientific_name.lower()] = taxon
        # A synonym never overrides a valid name, and a synonym shared by two species is ambiguous: leave it out.
        seen = {}
        for name, taxon_id in Synonym.objects.values_list("described_scientific_name", "taxon_id"):
            if name:
                seen.setdefault(name.lower(), set()).add(taxon_id)
        taxa = {t.id: t for t in self.by_id.values()}
        for name, ids in seen.items():
            if len(ids) == 1 and name not in self.by_name and next(iter(ids)) in taxa:
                self.by_name[name] = taxa[next(iter(ids))]

    def find(self, name, valid_species_id=""):
        """(taxon, error). A valid_species_id decides which species is meant; otherwise the name does."""
        if valid_species_id:
            taxon = self.by_id.get(valid_species_id)
            if taxon is None:
                return None, f"beetle_valid_species_id '{valid_species_id}' is not in the species list"
            return taxon, None
        taxon = self.by_name.get(" ".join(name.split()).lower())
        if taxon is None:
            return None, f"beetle '{name}' is not in the species list (or a name it is known by)"
        return taxon, None
