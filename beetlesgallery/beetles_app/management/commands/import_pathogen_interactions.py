import json
import os
from pathlib import Path
from django.core.management.base import BaseCommand
from django.conf import settings
from beetlesgallery.beetles_app.models import PathogenInteraction


class Command(BaseCommand):
    help = (
        "Loads the published bark & ambrosia beetle pathogen dataset (v1.0) from JSON into the database. Safe to run "
        "on every deploy: rows are matched on Records ID, and a row that is already in the database is left exactly "
        "as it is, so corrections made through the upload page are never undone. Never touches proposals or uploads."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--file",
            type=str,
            help="Path to bark_beetle_pathogens_master.json (defaults to beetlesgallery/data/interactions/v1.0/bark_beetle_pathogens_master.json)",
            default=None,
        )
        parser.add_argument(
            "--refresh",
            action="store_true",
            help="Also overwrite rows that are already in the database with the file's values (undoes corrections made since)",
        )
        parser.add_argument(
            "--clear",
            action="store_true",
            help="Delete the published-dataset rows first and reload them (accepted proposals and uploads are kept)",
        )

    def handle(self, *args, **options):
        file_path = options["file"]
        if not file_path:
            file_path = settings.BASE_DIR / "beetlesgallery" / "data" / "interactions" / "v1.0" / "bark_beetle_pathogens_master.json"
            if not os.path.exists(file_path):
                file_path = Path("F:/notion_data/bark_beetle_pathogens_master.json")

        file_path = Path(file_path)
        if not file_path.exists():
            self.stderr.write(self.style.ERROR(f"Data file not found at: {file_path}"))
            return

        self.stdout.write(f"Reading records from {file_path}...")
        with open(file_path, "r", encoding="utf-8") as f:
            records = json.load(f)

        dataset_rows = PathogenInteraction.objects.filter(origin=PathogenInteraction.Origin.DATASET)

        if options["clear"]:
            # Only the published dataset is reloaded: accepted proposals and curator uploads are not touched.
            deleted_count, _ = dataset_rows.delete()
            self.stdout.write(self.style.WARNING(f"Cleared {deleted_count} published-dataset records."))

        # A record counts as already there whichever way it got in: loaded before, or uploaded from the
        # initial file with its Records ID (see make_interactions_upload_file).
        existing = {}
        for row in PathogenInteraction.objects.exclude(record_number__isnull=True).exclude(record_number=""):
            existing.setdefault(row.record_number, []).append(row)

        to_create, to_update, unchanged, seen = [], [], 0, set()
        for r in records:
            fields = self.fields_from(r)
            number = fields["record_number"]
            if number in seen:
                self.stderr.write(self.style.WARNING(f"Records ID {number} appears more than once in the file; the first is used."))
                continue
            seen.add(number)
            match = (existing.get(number) or [None])[0] if number else None
            if match is None:
                to_create.append(PathogenInteraction(origin=PathogenInteraction.Origin.DATASET, **fields))
                continue
            if not options["refresh"]:
                unchanged += 1
                continue
            changed = [name for name, value in fields.items() if getattr(match, name) != value]
            if changed:
                for name in changed:
                    setattr(match, name, fields[name])
                to_update.append((match, changed))
            else:
                unchanged += 1

        surplus = sum(len(rows) - 1 for rows in existing.values() if len(rows) > 1)
        if surplus:
            self.stderr.write(self.style.WARNING(
                f"{surplus} extra copies of the same Records ID are already in the database (an earlier import "
                "without --clear). They were left alone; run once with --clear to reload the dataset cleanly."
            ))

        PathogenInteraction.objects.bulk_create(to_create, batch_size=500)
        for row, changed in to_update:
            row.save(update_fields=changed + ["updated_at"])
        self.stdout.write(self.style.SUCCESS(
            f"Pathogen interaction records: {len(to_create)} added, {len(to_update)} updated, {unchanged} already there (left as they are)."
        ))

    @staticmethod
    def fields_from(r):
        return {
            "record_block_id": r.get("Record Block ID") or None,
            "record_number": str(r.get("Records ID") or ""),
            "beetle_host": r.get("Beetle Host") or "Unknown Host",
            "beetle_host_id": r.get("Beetle Host IDs") or None,
            "pathogen": r.get("pathogens") or "Unknown Pathogen",
            "category": r.get("categories") or "Unknown",
            "organism_source": r.get("organism source") or None,
            "infection_site": r.get("infection site") or None,
            "ecological_relationship": r.get("ecological relationship") or None,
            "identification_method": r.get("identification method") or None,
            "validation_type": r.get("validation type") or None,
            "experimental_conditions": r.get("experimental conditions") or None,
            "country_or_region": r.get("country or region") or None,
            "year": str(r.get("year") or ""),
            "source": r.get("source") or None,
            "title": r.get("title") or None,
            "doi_or_full_text": r.get("doi or full text") or None,
            "full_text_status": r.get("full text") or None,
        }
