"""
Uploading and updating interactions from a CSV, the way the image metadata is uploaded and updated.

Download the current interactions (record_id and all), edit or add rows, and upload the file:

    record_id   blank or NEW: add a new interaction.   An existing record_id: update that interaction.

New rows need a beetle (beetle_host, or beetle_host_id), a pathogen (the other organism) and a category.
On an update only the columns that are in the file are applied; a blank cell empties an optional field. Every row
is checked before anything is written, and if any row is wrong nothing is saved.

Rows added here have origin "upload". Rows that came with the published v1.0 dataset cannot be changed here: the
interactions page shows that dataset from its fixed, versioned files, so a change would not appear. Correct those
at the source, or accept a proposal that supersedes them.
"""
import csv
import io
import re
import uuid
from dataclasses import dataclass, field

from django.db import transaction

from .interaction_proposals import SpeciesLookup, clean_doi
from .models import PathogenInteraction

EXPORT_COLUMNS = [
    "record_id", "beetle_host", "beetle_host_id", "pathogen", "category", "ecological_relationship",
    "country_or_region", "organism_source", "infection_site", "identification_method", "validation_type",
    "experimental_conditions", "source", "year", "title", "doi_or_full_text", "full_text_status", "origin",
]
# column -> (maximum length, or None). Blank clears an optional one on update.
TEXT_LIMITS = {
    "category": 64, "ecological_relationship": 128, "country_or_region": 128, "organism_source": 255,
    "infection_site": 255, "identification_method": 255, "validation_type": 128, "experimental_conditions": 255,
    "source": 255, "full_text_status": 64, "title": None,
}
OPTIONAL = [c for c in TEXT_LIMITS if c != "category"] + ["year", "doi_or_full_text"]
MAX_ERRORS_SHOWN = 30
NEW = {"", "new"}
COLUMN_ALIASES = {
    "record id": "record_id", "id": "record_id", "beetle": "beetle_host", "species": "beetle_host",
    "partner": "pathogen", "organism": "pathogen", "relationship": "ecological_relationship",
    "country": "country_or_region", "doi": "doi_or_full_text",
}


@dataclass
class UploadResult:
    rows: int = 0
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: list = field(default_factory=list)
    error_count: int = 0
    dry_run: bool = False

    @property
    def ok(self):
        return self.error_count == 0

    @property
    def hidden_errors(self):
        return self.error_count - len(self.errors)


def export_rows(queryset=None):
    """The interactions as dicts for the download, in a stable order."""
    queryset = queryset if queryset is not None else PathogenInteraction.objects.all()
    for r in queryset.order_by("origin", "record_number", "created_at", "id").iterator():
        yield {
            "record_id": str(r.id), "beetle_host": r.beetle_host, "beetle_host_id": r.beetle_host_id or "",
            "pathogen": r.pathogen, "category": r.category, "ecological_relationship": r.ecological_relationship or "",
            "country_or_region": r.country_or_region or "", "organism_source": r.organism_source or "",
            "infection_site": r.infection_site or "", "identification_method": r.identification_method or "",
            "validation_type": r.validation_type or "", "experimental_conditions": r.experimental_conditions or "",
            "source": r.source or "", "year": r.year or "", "title": r.title or "",
            "doi_or_full_text": r.doi_or_full_text or "", "full_text_status": r.full_text_status or "", "origin": r.origin,
        }


def write_csv(out):
    """Write the interactions as CSV to a text file object."""
    writer = csv.DictWriter(out, fieldnames=EXPORT_COLUMNS)
    writer.writeheader()
    for row in export_rows():
        writer.writerow(row)


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


def normalise_link(value):
    """(link, error): a DOI becomes https://doi.org/..., a web link is kept, anything else is refused."""
    value = (value or "").strip()
    if not value:
        return "", None
    if re.match(r"^https?://\S+$", value, re.I) and not re.match(r"^https?://(dx\.)?doi\.org/", value, re.I):
        return value, None
    doi = clean_doi(value)
    if re.fullmatch(r"10\.\d{4,9}/\S+", doi):
        return f"https://doi.org/{doi}", None
    return "", f"doi_or_full_text '{value[:60]}' should be a DOI (10.xxxx/...) or a web link starting with http:// or https://"


def import_interactions(source, user=None, dry_run=False):
    """Validate an interactions CSV and, unless dry_run, save it. Returns an UploadResult."""
    result = UploadResult(dry_run=dry_run)

    def problem(row_num, message):
        result.error_count += 1
        if len(result.errors) < MAX_ERRORS_SHOWN:
            result.errors.append(f"Row {row_num}: {message}." if row_num else f"{message}.")

    columns, rows = read_rows(source)
    if not columns:
        problem(0, "the file has no header row")
        return result
    if "record_id" not in columns:
        problem(0, "missing column: record_id (leave it blank, or write NEW, for a new interaction)")
        return result
    if not rows:
        problem(0, "the file has no rows")
        return result
    result.rows = len(rows)

    species = SpeciesLookup()
    known = {str(r.id): r for r in PathogenInteraction.objects.all()}
    duplicates = {}
    for r in known.values():
        duplicates[_claim(r.beetle_host_id, r.pathogen, r.doi_or_full_text or r.source or r.title)] = r

    changes, creations, seen_ids, seen_new = [], [], {}, {}
    for i, row in enumerate(rows):
        row_num = i + 2
        before = result.error_count
        record = row.get("record_id", "")
        target, creating = None, record.lower() in NEW
        if not creating:
            try:
                record = str(uuid.UUID(record))
            except ValueError:
                problem(row_num, f"record_id '{record}' is not an interaction id (leave it blank or write NEW for a new one)")
                continue
            target = known.get(record)
            if target is None:
                problem(row_num, f"record_id {record} is not an interaction in the database")
                continue
            if record in seen_ids:
                problem(row_num, f"repeats row {seen_ids[record]} (the same record_id)")
                continue
            seen_ids[record] = row_num
            if target.origin == PathogenInteraction.Origin.DATASET:
                problem(row_num, "this record is part of the published v1.0 dataset, which the interactions page shows from fixed files, "
                                 "so a change here would not show. Correct it at the source")
                continue

        values = {}
        # The beetle
        if creating or "beetle_host" in row or "beetle_host_id" in row:
            name, vid = row.get("beetle_host", ""), row.get("beetle_host_id", "")
            taxon, error = (None, "give the beetle: beetle_host (a name in the species list) or beetle_host_id") \
                if not name and not vid else species.find(name, vid)
            if not error and name and vid:
                named = species.by_name.get(" ".join(name.split()).lower())
                if named is not None and named.valid_species_id != taxon.valid_species_id:
                    error = f"beetle_host '{name}' and beetle_host_id {vid} are different species"
            if error:
                problem(row_num, error.replace("beetle_valid_species_id", "beetle_host_id"))
            else:
                values["beetle_host"], values["beetle_host_id"] = taxon.scientific_name, taxon.valid_species_id
        # The other organism and the category (required)
        for column, limit in (("pathogen", 255), ("category", 64)):
            if creating or column in row:
                text = " ".join(row.get(column, "").split())
                if not text:
                    problem(row_num, f"{column} is required")
                elif len(text) > limit:
                    problem(row_num, f"{column} is limited to {limit} characters")
                else:
                    values[column] = text
        # Everything else is optional
        for column, limit in TEXT_LIMITS.items():
            if column == "category" or column not in row:
                continue
            text = row.get(column, "")
            if limit and len(text) > limit:
                problem(row_num, f"{column} is limited to {limit} characters")
            else:
                values[column] = text or None
        if "year" in row:
            if row["year"] and (not re.fullmatch(r"\d{4}", row["year"]) or not 1500 <= int(row["year"]) <= 2200):
                problem(row_num, f"year '{row['year']}' is not a year")
            else:
                values["year"] = row["year"] or ""
        if "doi_or_full_text" in row:
            link, error = normalise_link(row["doi_or_full_text"])
            if error:
                problem(row_num, error)
            else:
                values["doi_or_full_text"] = link or None

        if result.error_count != before:
            continue

        if creating:
            claim = _claim(values["beetle_host_id"], values["pathogen"],
                           values.get("doi_or_full_text") or values.get("source") or values.get("title"))
            if claim in seen_new:
                problem(row_num, f"repeats row {seen_new[claim]} (same beetle, organism and source)")
                continue
            if claim in duplicates:
                problem(row_num, f"this interaction is already in the database (record {duplicates[claim].id}). "
                                 "To change it, upload it with its record_id")
                continue
            seen_new[claim] = row_num
            creations.append(values)
        else:
            changed = {k: v for k, v in values.items() if (getattr(target, k) or None) != (v or None)}
            if changed:
                changes.append((target, changed))
            else:
                result.unchanged += 1

    if not result.ok:
        return result
    result.created, result.updated = len(creations), len(changes)
    if dry_run:
        return result

    with transaction.atomic():
        PathogenInteraction.objects.bulk_create([
            PathogenInteraction(origin=PathogenInteraction.Origin.UPLOAD, added_by=user, **values) for values in creations
        ])
        for target, changed in changes:
            for name, value in changed.items():
                setattr(target, name, value)
            target.save(update_fields=list(changed) + ["updated_at"])
    return result


def _claim(beetle_id, pathogen, source):
    return ((beetle_id or "").strip(), " ".join((pathogen or "").split()).lower(), " ".join((source or "").split()).lower())
