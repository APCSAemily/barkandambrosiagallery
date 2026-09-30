"""
Requests for access to the site.

Anyone can fill in the form at /accounts/request-access/ saying who they are and which parts of the
site they want. The approvers (settings.ACCESS_REQUEST_RECIPIENTS) are emailed a link to
Data Management -> Access requests, where a superuser approves the request as a Member or a Curator,
or denies it. Nothing is created until a request is approved, and nobody is emailed except the
approvers (when a request arrives) and the applicant (when it is decided).

Roles are the ones the site already has:

    Member    a normal account: gallery and downloads, taxonomy, AI classifier, Beetle ID game
    Curator   is_staff: also annotate and validate, upload and update data

Superuser is never granted through a request.

A new account is created without a password. The approval email carries a link, valid for
settings.PASSWORD_RESET_TIMEOUT, where the person sets their own.
"""
import logging
import re
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
def submit_request(data, review_url):
    """
    Save a request and email the approvers. ``data`` is a form's cleaned_data.
    Returns the AccessRequest, or None when this email already has a request waiting.
    The request is kept even if the email cannot be sent (see AccessRequest.notify_error).
    """
    try:
        with transaction.atomic():
            access_request = AccessRequest.objects.create(
                name=data["name"], email=data["email"], affiliation=data["affiliation"],
                reason=data["reason"], areas=list(data["areas"]),
            )
    except IntegrityError:
        return None
    notify_approvers(access_request, review_url)
    return access_request


def notify_approvers(access_request, review_url):
    recipients = list(settings.ACCESS_REQUEST_RECIPIENTS)
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
    created: bool = False
    setup_url: str = ""
    email_error: str = ""


def _free_username(email):
    User = get_user_model()
    base = re.sub(r"[^\w.@+-]", "", email.split("@")[0])[:140] or "user"
    username, n = base, 1
    while User.objects.filter(username__iexact=username).exists():
        n += 1
        username = f"{base}{n}"
    return username


def _grant(access_request, role):
    """Create the account, or raise an existing one to ``role`` (never lowering it). Returns (user, created)."""
    User = get_user_model()
    matches = list(User.objects.filter(email__iexact=access_request.email))
    if len(matches) > 1:
        raise AccessError(
            f"Several accounts use {access_request.email}. Change the right one under My Account instead."
        )
    if matches:
        user = matches[0]
        if not user.is_active:
            raise AccessError(
                f"{access_request.email} belongs to a deactivated account ({user.username}). "
                "Reactivate it under My Account if you want them back."
            )
        if role == CURATOR and not user.is_staff:
            user.is_staff = True
            user.save(update_fields=["is_staff"])
        return user, False

    user = User(username=_free_username(access_request.email), email=access_request.email,
                first_name=access_request.name[:150], is_staff=(role == CURATOR))
    user.set_unusable_password()
    user.save()
    return user, True


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
        user, created = (None, False) if choice == DENY else _grant(access_request, choice)
        access_request.status = AccessRequest.Status.DENIED if choice == DENY else AccessRequest.Status.APPROVED
        access_request.granted_role = "" if choice == DENY else choice
        access_request.user = user
        access_request.decided_by = decided_by
        access_request.decided_at = timezone.now()
        access_request.decision_note = note
        access_request.save()

    decision = Decision(access_request, user=user, created=created)
    if created:
        uid = urlsafe_base64_encode(force_bytes(user.pk))
        decision.setup_url = absolute_uri(reverse("password_set", args=[uid, default_token_generator.make_token(user)]))
    try:
        _email_applicant(decision, absolute_uri(reverse("login")))
    except Exception as exc:  # the decision stands; the approver is told to pass it on
        logger.exception("Could not email the applicant for access request %s", access_request.pk)
        decision.email_error = f"{type(exc).__name__}: {exc}"[:255]
    return decision


def _email_applicant(decision, login_url):
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
        if decision.created:
            days = settings.PASSWORD_RESET_TIMEOUT // (24 * 60 * 60)
            body += (
                f"\nYour username is: {decision.user.username}\n"
                f"Choose your password here (the link works for {days} days):\n{decision.setup_url}\n"
                f"\nThen sign in at {login_url}\n"
            )
        else:
            body += f"\nYour existing account ({decision.user.username}) now has this access. Sign in at {login_url}\n"
        body += note
    EmailMessage(subject=subject, body=body, to=[access_request.email]).send(fail_silently=False)
