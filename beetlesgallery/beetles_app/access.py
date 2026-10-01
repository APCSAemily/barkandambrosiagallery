"""
Requests for access to the site.

Someone fills in /accounts/request-access/ with who they are, which parts of the site they want, and the username
and password they want to use. They get an inactive account and an email with a link to confirm their address.
Once it is confirmed the approvers (every superuser, plus settings.ACCESS_REQUEST_RECIPIENTS) are emailed a link to
My Account -> Access Requests, where a superuser approves the request as a Member or a Curator, or denies it.
Approving activates the account and emails the applicant; they sign in as usual and can reset their own password.
A denied applicant's unused account is removed.

Someone who already has an account can ask for more access the same way: their email is already confirmed, so
the approvers are told straight away.

Roles are the ones the site already has:

    Member    a normal account: gallery and downloads, taxonomy, AI classifier, Beetle ID game
    Curator   is_staff: also annotate and validate, upload and update data

Superuser is never granted through a request. Finer access is set per person on My Account (see areas.py).
"""
import logging
import re
from datetime import timedelta
from dataclasses import dataclass

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.exceptions import ValidationError
from django.core.mail import EmailMessage
from django.db import IntegrityError, transaction
from django.db.models.functions import Lower
from django.urls import reverse
from django.utils import timezone
from django.utils.encoding import force_bytes
from django.utils.http import urlsafe_base64_encode

from .models import AccessRequest

logger = logging.getLogger(__name__)

MEMBER, CURATOR, DENY = "member", "curator", "deny"
ROLE_LABELS = {MEMBER: "Member", CURATOR: "Curator"}

# key, label shown on the form, what it covers, the role it needs
AREAS = [
    ("browse", "Browse and download images", "The image gallery, the taxonomy browser and batch downloads.", MEMBER),
    ("classify", "AI species classifier", "Identify a beetle from a photo.", MEMBER),
    ("game", "Beetle ID game", "Play the identification game and see your own results.", MEMBER),
    ("annotate", "Annotate and validate", "Draw bounding boxes, label and validate records.", CURATOR),
    ("upload", "Upload and update data", "Add new images and their metadata, or correct existing records.", CURATOR),
]
AREA_LABELS = {key: label for key, label, _, _ in AREAS}
AREA_ROLES = {key: role for key, _, _, role in AREAS}
ROLE_SUMMARY = {
    MEMBER: "the image gallery and downloads, the taxonomy browser, the AI classifier and the Beetle ID game",
    CURATOR: "everything a Member can use, plus annotating and validating records and uploading and updating data",
}

THROTTLE_PER_IP = 5      # requests per hour from one address
THROTTLE_IN_TOTAL = 30   # requests per hour in all, so approvers' inboxes cannot be flooded
THROTTLE_WINDOW = 60 * 60


class AccessError(Exception):
    """A problem to show the approver; nothing was changed."""


def area_labels(keys):
    return [AREA_LABELS[k] for k in keys if k in AREA_LABELS]


def role_needed(keys):
    """The lowest role that covers every requested area."""
    return CURATOR if any(AREA_ROLES.get(k) == CURATOR for k in keys) else MEMBER


def throttled(request):
    """True when this address (or everyone together) has sent too many requests in the last hour."""
    from django.core.cache import cache

    keys = {
        f"access-request:ip:{request.META.get('REMOTE_ADDR', '')}": THROTTLE_PER_IP,
        "access-request:all": THROTTLE_IN_TOTAL,
    }
    counts = {key: cache.get(key, 0) for key in keys}
    if any(counts[key] >= limit for key, limit in keys.items()):
        return True
    for key, count in counts.items():
        cache.set(key, count + 1, THROTTLE_WINDOW)
    return False


def absolute_url(request, name, *args):
    return request.build_absolute_uri(reverse(name, args=args))


# ---------------------------------------------------------------------------
# A request arrives
# ---------------------------------------------------------------------------
def verification_url(request, user):
    uid = urlsafe_base64_encode(force_bytes(user.pk))
    return request.build_absolute_uri(reverse("verify_email", args=[uid, default_token_generator.make_token(user)]))


def send_verification_email(access_request, url):
    """Email the applicant a link that proves the address is theirs. Returns an error text, or ''."""
    days = settings.PASSWORD_RESET_TIMEOUT // (24 * 60 * 60)
    try:
        EmailMessage(
            subject="Confirm your email for the Bark & Ambrosia Beetle Gallery",
            body=(
                f"Hello {access_request.name},\n\n"
                "Thank you for asking for an account. Please confirm this email address by opening this link "
                f"(it works for {days} days):\n{url}\n\n"
                "After that, the people who run the gallery will review your request and email you their decision. "
                "You can sign in with the username and password you chose once it is approved.\n\n"
                "If you did not ask for an account, you can ignore this email."
            ),
            to=[access_request.email],
        ).send(fail_silently=False)
    except Exception as exc:
        logger.exception("Could not send the confirmation email for access request %s", access_request.pk)
        return f"{type(exc).__name__}: {exc}"[:255]
    return ""


def submit_request(request, data):
    """
    Save a request from the form's cleaned_data.

    Someone without an account gets an inactive one with the username and password they chose, and is emailed
    a link to confirm their address; the approvers are told once it is confirmed. Someone already signed in asking
    for more access is already confirmed, so the approvers are told straight away.
    Returns the AccessRequest, or None when this email already has a request waiting (the confirmation email is
    sent again, to that address only).
    """
    User = get_user_model()
    review_url = absolute_url(request, "access_requests")
    signed_in = request.user.is_authenticated
    # A request made before people chose their own password has no account attached and can never be approved:
    # a new one for the same email replaces it.
    AccessRequest.objects.filter(
        email__iexact=data["email"], status=AccessRequest.Status.PENDING, user__isnull=True
    ).update(status=AccessRequest.Status.DENIED, decision_note="Replaced by a newer request.", decided_at=timezone.now())
    try:
        with transaction.atomic():
            if signed_in:
                user, verified = request.user, timezone.now()
            else:
                user = User(username=data["username"], email=data["email"], first_name=data["name"][:150], is_active=False)
                user.set_password(data["password1"])
                user.save()
                verified = None
            access_request = AccessRequest.objects.create(
                name=data["name"], email=data["email"], affiliation=data["affiliation"], reason=data["reason"],
                areas=list(data["areas"]), user=user, email_verified_at=verified,
            )
    except IntegrityError:
        waiting = AccessRequest.objects.filter(
            email__iexact=data["email"], status=AccessRequest.Status.PENDING, email_verified_at__isnull=True,
            user__is_active=False,
        ).select_related("user").first()
        if waiting:
            send_verification_email(waiting, verification_url(request, waiting.user))
        return None
    if signed_in:
        notify_approvers(access_request, review_url)
    else:
        error = send_verification_email(access_request, verification_url(request, user))
        if error:
            access_request.notify_error = f"Confirmation email failed: {error}"
            access_request.save(update_fields=["notify_error"])
    return access_request


def confirm_email(request, uidb64, token):
    """The link in the confirmation email. Returns the AccessRequest (now confirmed), or None for a bad or old link."""
    from django.utils.http import urlsafe_base64_decode

    User = get_user_model()
    try:
        user = User.objects.get(pk=urlsafe_base64_decode(uidb64).decode())
    except (TypeError, ValueError, OverflowError, User.DoesNotExist):
        return None
    if not default_token_generator.check_token(user, token):
        return None
    access_request = AccessRequest.objects.filter(user=user, status=AccessRequest.Status.PENDING).first()
    if access_request is None:
        return None
    if access_request.email_verified_at is None:
        access_request.email_verified_at = timezone.now()
        access_request.save(update_fields=["email_verified_at"])
        notify_approvers(access_request, absolute_url(request, "access_requests"))
    return access_request


def approver_recipients():
    """The configured addresses, plus every active superuser who has one: all superusers can decide requests."""
    recipients = list(settings.ACCESS_REQUEST_RECIPIENTS)
    known = {r.lower() for r in recipients}
    for address in get_user_model().objects.filter(is_superuser=True, is_active=True).exclude(email="").values_list("email", flat=True):
        if address.lower() not in known:
            known.add(address.lower())
            recipients.append(address)
    return recipients


def send_reminder(review_url, older_than_hours=24, dry_run=False):
    """
    One email to the approvers listing the requests that have waited for a decision longer than ``older_than_hours``
    (counted from when the applicant confirmed their email). Returns the requests listed; sends nothing if there are none.
    """
    cutoff = timezone.now() - timedelta(hours=older_than_hours)
    waiting = list(
        AccessRequest.objects.filter(status=AccessRequest.Status.PENDING, email_verified_at__isnull=False,
                                     email_verified_at__lte=cutoff).order_by("email_verified_at")
    )
    recipients = approver_recipients()
    if not waiting or not recipients or dry_run:
        return waiting, recipients
    now = timezone.now()
    lines = []
    for r in waiting:
        hours = int((now - r.email_verified_at).total_seconds() // 3600)
        waited = f"{hours // 24} days" if hours >= 48 else f"{hours} hours"
        lines.append(f"  - {r.name} <{r.email}> ({', '.join(area_labels(r.areas)) or 'nothing selected'}), waiting {waited}")
    count = len(waiting)
    body = (
        f"{count} access request{'s are' if count != 1 else ' is'} still waiting for a decision on the "
        "Bark & Ambrosia Beetle Gallery:\n\n" + "\n".join(lines) +
        f"\n\nApprove or deny them here (superuser sign-in needed):\n{review_url}\n\n"
        "These people have confirmed their email address and cannot sign in until you decide."
    )
    EmailMessage(
        subject=f"Reminder: {count} access request{'s' if count != 1 else ''} waiting", body=body, to=recipients,
    ).send(fail_silently=False)
    return waiting, recipients


def notify_approvers(access_request, review_url):
    recipients = approver_recipients()
    if not recipients:
        access_request.notify_error = "No approvers are configured (ACCESS_REQUEST_RECIPIENTS)."
    else:
        wanted = "\n".join(f"  - {label}" for label in area_labels(access_request.areas)) or "  (nothing selected)"
        body = (
            f"{access_request.name} <{access_request.email}> asked for access to the Bark & Ambrosia Beetle Gallery.\n\n"
            f"Affiliation: {access_request.affiliation or '-'}\n\n"
            f"Wants to use:\n{wanted}\n\n"
            f"Why:\n{access_request.reason or '-'}\n\n"
            f"Approve as a Member or a Curator, or deny it (superuser sign-in needed):\n{review_url}\n\n"
            "Replying to this email writes to the applicant."
        )
        try:
            EmailMessage(
                subject=f"Access request: {access_request.name}", body=body,
                to=recipients, reply_to=[access_request.email],
            ).send(fail_silently=False)
            access_request.notified_at = timezone.now()
        except Exception as exc:  # a mail problem must not lose the request
            logger.exception("Could not email the approvers about access request %s", access_request.pk)
            access_request.notify_error = f"{type(exc).__name__}: {exc}"[:255]
    access_request.save(update_fields=["notified_at", "notify_error"])


# ---------------------------------------------------------------------------
# A request is decided
# ---------------------------------------------------------------------------
@dataclass
class Decision:
    access_request: AccessRequest
    user: object = None
    email_error: str = ""


def _grant(access_request, role):
    """Activate the requester's account (the one they made, or the one they already had) at ``role``, never lowering it."""
    user = access_request.user
    if user is None:
        raise AccessError("This request has no account attached.")
    if access_request.email_verified_at is None:
        raise AccessError(f"{access_request.name} has not confirmed their email address yet.")
    changed = []
    if not user.is_active:
        user.is_active = True
        changed.append("is_active")
    if role == CURATOR and not user.is_staff:
        user.is_staff = True
        changed.append("is_staff")
    if changed:
        user.save(update_fields=changed)
    return user


def _discard_unused_account(access_request):
    """A denied applicant's account (made for this request, never signed in to) is removed so the username is free."""
    user = access_request.user
    if user is not None and not user.is_active and user.last_login is None and not user.is_staff:
        user.delete()


def decide(request_id, choice, decided_by, note, absolute_uri):
    """
    Approve (choice "member" or "curator") or deny (choice "deny") a pending request, then email the applicant.
    ``absolute_uri`` is request.build_absolute_uri. Raises AccessError with a message for the approver.
    """
    if choice not in (MEMBER, CURATOR, DENY):
        raise AccessError("Choose Member, Curator or Deny.")
    note = (note or "").strip()[:1000]

    with transaction.atomic():
        try:
            access_request = AccessRequest.objects.select_for_update().get(pk=request_id)
        except (AccessRequest.DoesNotExist, ValidationError, ValueError):
            raise AccessError("That request no longer exists.")
        if access_request.status != AccessRequest.Status.PENDING:
            raise AccessError(f"{access_request.name}'s request was already {access_request.status}.")
        user = None if choice == DENY else _grant(access_request, choice)
        access_request.status = AccessRequest.Status.DENIED if choice == DENY else AccessRequest.Status.APPROVED
        access_request.granted_role = "" if choice == DENY else choice
        access_request.decided_by = decided_by
        access_request.decided_at = timezone.now()
        access_request.decision_note = note
        access_request.save()
        applicant = access_request.user
        if choice == DENY:
            _discard_unused_account(access_request)

    decision = Decision(access_request, user=user or applicant)
    try:
        _email_applicant(decision, absolute_uri(reverse("login")), absolute_uri(reverse("password_reset")))
    except Exception as exc:  # the decision stands; the approver is told to pass it on
        logger.exception("Could not email the applicant for access request %s", access_request.pk)
        decision.email_error = f"{type(exc).__name__}: {exc}"[:255]
    return decision


def _email_applicant(decision, login_url, reset_url):
    access_request = decision.access_request
    note = f"\nA note from the reviewer:\n{access_request.decision_note}\n" if access_request.decision_note else ""
    if access_request.status == AccessRequest.Status.DENIED:
        subject = "Your access request"
        body = (
            f"Hello {access_request.name},\n\n"
            "Thank you for your interest in the Bark & Ambrosia Beetle Gallery. "
            "We are not able to give you an account right now.\n"
            f"{note}"
        )
    else:
        role = ROLE_LABELS[access_request.granted_role]
        subject = "Your access to the Bark & Ambrosia Beetle Gallery"
        body = f"Hello {access_request.name},\n\nYour access request was approved. You have {role} access: {ROLE_SUMMARY[access_request.granted_role]}.\n"
        body += (
            f"\nSign in at {login_url} with the username you chose: {decision.user.username}\n"
            f"If you forget your password, you can reset it from the sign-in page: {reset_url}\n"
        )
        body += note
    EmailMessage(subject=subject, body=body, to=[access_request.email]).send(fail_silently=False)
