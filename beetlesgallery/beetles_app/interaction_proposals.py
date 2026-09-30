"""
Loading proposed ecological interactions from a CSV.

Used by ``manage.py import_interaction_proposals`` and by the collector's output
(beetlesgallery/tools/collect_interactions.py). Each row is one claim from one source:

    beetle_name              the beetle, by any name in the species list (a synonym is matched to its valid name)
    beetle_valid_species_id  optional; if given it decides which species is meant
    partner_name             the other organism (required)
    category                 Fungi, Nematode, Host plant, Mite ...
    relationship             pathogen, parasite, symbiont, host plant ...
    source_doi               \\
    source_url                } at least one of these, so a reviewer can read the source
    source_id                /  (e.g. a PubMed id, with source_db)
    source_title, source_authors, source_journal, source_year, source_db, open_access
    evidence                 the sentence(s) in the source that state it
    evidence_location        title, abstract, full text
    score                    0 to 1
    collector                what proposed it, and its version

The whole file is checked before anything is written; if any row is wrong nothing is saved and each problem
is listed with its row number. Loading a claim that already exists refreshes it while it is still waiting for
review; once an expert has accepted or rejected it, it is left exactly as decided.
"""
import csv
import io
import re
from dataclasses import dataclass, field

from django.db import transaction
from django.db.models.functions import Lower

from .interaction_names import SpeciesLookup, clean_doi  # noqa: F401  (clean_doi is re-exported)
from .models import InteractionProposal

REQUIRED_COLUMNS = ("beetle_name", "partner_name")
MAX_ERRORS_SHOWN = 30
MAX_EVIDENCE = 2000
CHUNK = 1000
TRUE, FALSE = {"true", "yes", "y", "1"}, {"false", "no", "n", "0"}

COLUMN_ALIASES = {
    "beetle": "beetle_name", "species": "beetle_name", "partner": "partner_name", "organism": "partner_name",
    "doi": "source_doi", "url": "source_url", "title": "source_title", "journal": "source_journal", "authors": "source_authors",
    "year": "source_year", "database": "source_db", "pmid": "source_id", "open access": "open_access",
    "valid_species_id": "beetle_valid_species_id",
}


@dataclass
class ImportResult:
    rows: int = 0
    created: int = 0
    refreshed: int = 0
    kept: int = 0            # already decided by an expert: left as it is
    errors: list = field(default_factory=list)
    error_count: int = 0
    dry_run: bool = False

    @property
    def ok(self):
        return self.error_count == 0

    @property
    def hidden_errors(self):
        return self.error_count - len(self.errors)


def source_key_for(doi, source_db, source_id, url):
    """What makes a source unique: the DOI, else the database id, else the link."""
    if doi:
        return doi
    if source_id:
        return f"{(source_db or 'source').lower()}:{source_id.strip().lower()}"
    return (url or "").strip().lower()


def read_rows(source):
    text = source.read() if hasattr(source, "read") else source
    if isinstance(text, bytes):
        text = text.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text.lstrip("﻿")))
    if reader.fieldnames is None:
        return [], []
    names = [COLUMN_ALIASES.get(n.strip().lower(), n.strip().lower()) for n in reader.fieldnames]
    rows = []
    for raw in reader:
        values = [v if isinstance(v, str) else "" for v in raw.values()]
        rows.append(dict(zip(names, (v.strip() for v in values))))
    return names, rows


def _chunks(items, size=CHUNK):
    items = list(items)
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _bool(value):
    text = (value or "").strip().lower()
    if not text:
        return None, None
    if text in TRUE:
        return True, None
    if text in FALSE:
        return False, None
    return None, f"open_access '{value}' should be true or false"


def import_proposals(source, user=None, dry_run=False, default_collector=""):
    """Validate a proposals CSV and, unless dry_run, save it. Returns an ImportResult."""
    result = ImportResult(dry_run=dry_run)

    def problem(row_num, message):
        result.error_count += 1
        if len(result.errors) < MAX_ERRORS_SHOWN:
            result.errors.append(f"Row {row_num}: {message}." if row_num else f"{message}.")

    columns, rows = read_rows(source)
    missing = [c for c in REQUIRED_COLUMNS if c not in columns]
    if missing:
        problem(0, f"missing column(s): {', '.join(missing)}. See beetles_app/interaction_proposals.py for the columns")
        return result
    if not rows:
        problem(0, "the file has no rows")
        return result
    result.rows = len(rows)

    species = SpeciesLookup()
    valid, first_seen = [], {}
    for i, row in enumerate(rows):
        row_num = i + 2
        before = result.error_count

        beetle = " ".join(row.get("beetle_name", "").split())
        partner = " ".join(row.get("partner_name", "").split())
        taxon = None
        if not beetle and not row.get("beetle_valid_species_id"):
            problem(row_num, "beetle_name is empty")
        else:
            taxon, error = species.find(beetle, row.get("beetle_valid_species_id", ""))
            if error:
                problem(row_num, error)
        if not partner:
            problem(row_num, "partner_name is empty")
        elif len(partner) > 255:
            problem(row_num, "partner_name is limited to 255 characters")

        category, relationship = row.get("category", ""), row.get("relationship", "")
        if len(category) > 64 or len(relationship) > 128:
            problem(row_num, "category is limited to 64 characters and relationship to 128")

        doi = clean_doi(row.get("source_doi", ""))
        url = row.get("source_url", "")
        if url and not re.match(r"^https?://\S+$", url, re.I):
            problem(row_num, f"source_url '{url[:60]}' must be a web link starting with http:// or https://")
            url = ""
        if not url and doi:
            url = f"https://doi.org/{doi}"
        key = source_key_for(doi, row.get("source_db", ""), row.get("source_id", ""), url)
        if not key:
            problem(row_num, "there is no source: give source_doi, source_url or source_id so a reviewer can read it")
        elif len(key) > 300 or len(url) > 500 or len(doi) > 255:
            problem(row_num, "source_doi is limited to 255 characters, source_url to 500")

        year = None
        if row.get("source_year"):
            if not re.fullmatch(r"\d{4}", row["source_year"]) or not 1500 <= int(row["source_year"]) <= 2200:
                problem(row_num, f"source_year '{row['source_year']}' is not a year")
            else:
                year = int(row["source_year"])

        open_access, error = _bool(row.get("open_access"))
        if error:
            problem(row_num, error)

        score = None
        if row.get("score"):
            try:
                score = float(row["score"])
            except ValueError:
                score = None
            if score is None or score != score or not 0 <= score <= 1:
                problem(row_num, f"score '{row['score']}' must be a number from 0 to 1")
                score = None

        evidence = row.get("evidence", "")
        if len(evidence) > MAX_EVIDENCE:
            problem(row_num, f"evidence is limited to {MAX_EVIDENCE} characters")
        if len(row.get("source_journal", "")) > 255 or len(row.get("source_db", "")) > 30 \
                or len(row.get("evidence_location", "")) > 30 or len(row.get("collector", "")) > 100 \
                or len(row.get("source_authors", "")) > 500:
            problem(row_num, "source_authors is limited to 500 characters, source_journal to 255, "
                             "source_db and evidence_location to 30, collector to 100")

        if result.error_count != before:
            continue
        claim = (taxon.scientific_name.lower(), partner.lower(), key)
        if claim in first_seen:
            problem(row_num, f"repeats row {first_seen[claim]} (same beetle, partner and source)")
            continue
        first_seen[claim] = row_num
        valid.append(dict(
            beetle_name=taxon.scientific_name, beetle_valid_species_id=taxon.valid_species_id, taxon_id=taxon.id,
            partner_name=partner, category=category, relationship=relationship,
            source_key=key, source_doi=doi, source_url=url, source_title=row.get("source_title", ""),
            source_authors=row.get("source_authors", ""), source_journal=row.get("source_journal", ""), source_year=year, source_db=row.get("source_db", ""),
            open_access=open_access, evidence=evidence, evidence_location=row.get("evidence_location", ""),
            score=score, collector=row.get("collector", "") or default_collector,
        ))

    if not result.ok:
        return result

    existing = {}
    for chunk in _chunks({v["beetle_name"].lower() for v in valid}):
        for p in InteractionProposal.objects.annotate(b=Lower("beetle_name"), pn=Lower("partner_name")).filter(b__in=chunk):
            existing[(p.b, p.pn, p.source_key)] = p
    fresh, refresh = [], []
    for v in valid:
        current = existing.get((v["beetle_name"].lower(), v["partner_name"].lower(), v["source_key"]))
        if current is None:
            fresh.append(InteractionProposal(created_by=user, **v))
        elif current.status == InteractionProposal.Status.PROPOSED:
            for name, value in v.items():
                setattr(current, name, value)
            refresh.append(current)
        else:
            result.kept += 1
    result.created, result.refreshed = len(fresh), len(refresh)
    if dry_run:
        return result

    fields = [name.removesuffix("_id") if name == "taxon_id" else name for name in valid[0]]
    with transaction.atomic():
        for chunk in _chunks(fresh):
            InteractionProposal.objects.bulk_create(chunk)
        for chunk in _chunks(refresh):
            InteractionProposal.objects.bulk_update(chunk, fields)
    return result
