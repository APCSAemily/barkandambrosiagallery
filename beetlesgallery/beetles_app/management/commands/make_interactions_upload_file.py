"""
Write the published v1.0 interactions dataset as a CSV for the Upload or Update Interactions page.

The file has every record with its Records ID, so uploading it into an empty database recreates the dataset exactly
as the interactions page has always shown it. It is the same thing ``import_pathogen_interactions`` loads on every
deploy, in the form a person can upload by hand (and edit first if they like).

    manage.py make_interactions_upload_file            writes interactions_v1.0_upload.csv in the current folder
    manage.py make_interactions_upload_file --out x.csv

The site builds the same file on request (Upload or Update Interactions → Initial file), so it isn't kept in git.
"""
import csv
import json
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand

from beetlesgallery.beetles_app.interaction_upload import EXPORT_COLUMNS

DATA = Path(settings.BASE_DIR) / "beetlesgallery" / "data" / "interactions" / "v1.0"
SOURCE = DATA / "bark_beetle_pathogens_master.json"
DEFAULT_OUT = Path("interactions_v1.0_upload.csv")
COLUMNS = [c for c in EXPORT_COLUMNS if c not in ("origin",)]


def upload_rows(records):
    """The published records as dicts keyed by the upload columns. record_id is NEW: every row is added."""
    rows = []
    for r in records:
        rows.append({
            "record_id": "NEW",
            "records_id": str(r.get("Records ID") or ""),
            "beetle_host": r.get("Beetle Host") or "Unknown Host",
            "beetle_host_id": r.get("Beetle Host IDs") or "",
            "pathogen": r.get("pathogens") or "Unknown Pathogen",
            "category": r.get("categories") or "Unknown",
            "ecological_relationship": r.get("ecological relationship") or "",
            "country_or_region": r.get("country or region") or "",
            "organism_source": r.get("organism source") or "",
            "infection_site": r.get("infection site") or "",
            "identification_method": r.get("identification method") or "",
            "validation_type": r.get("validation type") or "",
            "experimental_conditions": r.get("experimental conditions") or "",
            "source": r.get("source") or "",
            "year": str(r.get("year") or ""),
            "title": r.get("title") or "",
            "doi_or_full_text": r.get("doi or full text") or "",
            "full_text_status": r.get("full text") or "",
        })
    return rows


def render(records):
    """The CSV text (UTF-8 with a byte order mark, so Excel reads the accents)."""
    import io
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=COLUMNS, lineterminator="\n")
    writer.writeheader()
    writer.writerows(upload_rows(records))
    return "﻿" + out.getvalue()


class Command(BaseCommand):
    help = "Write the published v1.0 interactions as a CSV for the Upload or Update Interactions page."

    def add_arguments(self, parser):
        parser.add_argument("--out", default=None, help=f"Where to write it (default {DEFAULT_OUT})")

    def handle(self, *args, **options):
        records = json.loads(SOURCE.read_text(encoding="utf-8"))
        out = Path(options["out"]) if options["out"] else DEFAULT_OUT
        out.write_text(render(records), encoding="utf-8", newline="")
        self.stdout.write(self.style.SUCCESS(f"Wrote {len(records)} records to {out}"))
