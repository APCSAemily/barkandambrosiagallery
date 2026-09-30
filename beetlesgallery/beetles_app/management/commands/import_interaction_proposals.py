from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from beetlesgallery.beetles_app.interaction_proposals import import_proposals


class Command(BaseCommand):
    help = (
        "Load proposed ecological interactions (one row per claim and source) from a CSV, for experts to review. "
        "The file is checked first; if any row is wrong nothing is saved. Safe to run again: a claim that is "
        "already there is refreshed while it waits for review and left alone once an expert has decided it. "
        "Columns: see beetles_app/interaction_proposals.py."
    )

    def add_arguments(self, parser):
        parser.add_argument("file", help="Path to the CSV file")
        parser.add_argument("--collector", default="", help="collector name for rows that leave it empty")
        parser.add_argument("--user", default="", help="Username to record as the uploader")
        parser.add_argument("--dry-run", action="store_true", help="Check the file and report, but save nothing")

    def handle(self, *args, **options):
        user = None
        if options["user"]:
            user = get_user_model().objects.filter(username=options["user"]).first()
            if user is None:
                raise CommandError(f"No user named '{options['user']}'.")
        try:
            with open(options["file"], "rb") as fh:
                result = import_proposals(
                    fh, user=user, dry_run=options["dry_run"], default_collector=options["collector"],
                )
        except OSError as exc:
            raise CommandError(str(exc))

        if not result.ok:
            for message in result.errors:
                self.stderr.write(message)
            if result.hidden_errors:
                self.stderr.write(f"...and {result.hidden_errors} more.")
            raise CommandError(f"{result.error_count} problem(s) found, so nothing was saved.")
        summary = f"{result.refreshed} refreshed, {result.kept} already decided by an expert (left as they are)"
        if result.dry_run:
            self.stdout.write(self.style.SUCCESS(
                f"{result.rows} rows are valid: {result.created} would be new, {summary}. Nothing was saved."
            ))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"{result.rows} rows: {result.created} new, {summary}. They wait for an expert's review."
            ))
