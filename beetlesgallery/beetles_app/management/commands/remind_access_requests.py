"""
Email the approvers a reminder about access requests that are still waiting for a decision.

    manage.py remind_access_requests                    requests waiting more than 24 hours
    manage.py remind_access_requests --older-than-hours 48
    manage.py remind_access_requests --dry-run          list them, send nothing

One email lists everyone waiting, so it is a nudge and not a flood. The server runs it once a day from the
"Access request reminders" GitHub workflow; it sends nothing on days when nobody is waiting.
"""
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.urls import reverse

from beetlesgallery.beetles_app import access


class Command(BaseCommand):
    help = "Email the approvers a list of access requests still waiting for a decision."

    def add_arguments(self, parser):
        parser.add_argument("--older-than-hours", type=int, default=24, help="Only requests confirmed at least this long ago")
        parser.add_argument("--dry-run", action="store_true", help="List them and say who would be emailed, but send nothing")

    def handle(self, *args, **options):
        review_url = settings.SITE_URL.rstrip("/") + reverse("access_requests")
        try:
            waiting, recipients = access.send_reminder(review_url, options["older_than_hours"], dry_run=options["dry_run"])
        except Exception as exc:
            raise CommandError(f"Could not send the reminder: {type(exc).__name__}: {exc}")
        if not waiting:
            self.stdout.write("No access requests are waiting.")
            return
        if not recipients:
            raise CommandError("Requests are waiting but nobody is set up to be emailed (ACCESS_REQUEST_RECIPIENTS, or a superuser with an email).")
        names = ", ".join(r.name for r in waiting)
        verb = "Would email" if options["dry_run"] else "Emailed"
        self.stdout.write(self.style.SUCCESS(f"{verb} {', '.join(recipients)} about {len(waiting)} waiting: {names}"))
