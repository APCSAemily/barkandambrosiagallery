"""
Check that the site can send email: ``manage.py send_test_email you@example.org``.

Prints how email is configured (never the password), sends one message and says what happened. If it is
still writing to the console, nothing is delivered: set EMAIL_HOST and the other EMAIL_* variables (docs/email_setup.md).
"""
from django.conf import settings
from django.core.mail import send_mail
from django.core.management.base import BaseCommand, CommandError


class Command(BaseCommand):
    help = "Send a test email and show the email settings in use."

    def add_arguments(self, parser):
        parser.add_argument("to", help="Address to send the test message to")

    def handle(self, *args, **options):
        backend = settings.EMAIL_BACKEND.rsplit(".", 2)[-2]
        self.stdout.write(f"Backend:  {settings.EMAIL_BACKEND}")
        self.stdout.write(f"Host:     {settings.EMAIL_HOST or '(not set)'}:{settings.EMAIL_PORT}  TLS={settings.EMAIL_USE_TLS}")
        self.stdout.write(f"User:     {settings.EMAIL_HOST_USER or '(not set)'}  password {'set' if settings.EMAIL_HOST_PASSWORD else 'NOT set'}")
        self.stdout.write(f"From:     {settings.DEFAULT_FROM_EMAIL}")
        self.stdout.write(f"Approvers: {', '.join(settings.ACCESS_REQUEST_RECIPIENTS) or '(none)'} (plus every superuser with an email)")
        if backend in ("console", "locmem", "dummy", "filebased"):
            self.stdout.write(self.style.WARNING(
                f"The '{backend}' backend does not deliver mail. Nothing will reach an inbox until EMAIL_HOST is set."))
        try:
            send_mail("Bark & Ambrosia Beetle Gallery: test email",
                      "If you can read this, the site can send email.", settings.DEFAULT_FROM_EMAIL,
                      [options["to"]], fail_silently=False)
        except Exception as exc:
            raise CommandError(f"Sending failed: {type(exc).__name__}: {exc}")
        self.stdout.write(self.style.SUCCESS(f"Sent to {options['to']} (check the inbox and the spam folder)."))
