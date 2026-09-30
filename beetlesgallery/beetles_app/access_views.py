"""Views for requesting and granting access (see beetles_app/access.py)."""
from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.views import PasswordResetConfirmView
from django.db.models.functions import Lower
from django.shortcuts import redirect, render
from django.urls import reverse_lazy

from . import access
from .forms import AccessRequestForm
from .models import AccessRequest
from .views import superuser_required

RECENT_DECISIONS = 25


def request_access(request):
    """Public form. The person is told the same thing whether or not a request was already waiting."""
    if request.method == "POST":
        if request.POST.get("leave_blank"):  # the hidden field: a bot
            return redirect("request_access_sent")
        form = AccessRequestForm(request.POST)
        if form.is_valid():
            if access.throttled(request):
                form.add_error(None, "There have been too many requests just now. Please try again in an hour.")
            else:
                access.submit_request(form.cleaned_data, access.absolute_url(request, "access_requests"))
                return redirect("request_access_sent")
    else:
        initial = {}
        if request.user.is_authenticated:
            initial = {"name": request.user.get_full_name(), "email": request.user.email}
        form = AccessRequestForm(initial=initial)
    return render(request, "accounts/request_access.html", {"form": form, "areas": access.AREAS})


def request_access_sent(request):
    return render(request, "accounts/request_access_sent.html")


class SetPasswordView(PasswordResetConfirmView):
    """The link in an approval email: the person chooses their own password."""

    template_name = "accounts/password_set.html"
    success_url = reverse_lazy("login")
    post_reset_login = False

    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, "Your password is set. You can sign in now.")
        return response


def _with_context(requests):
    """Attach the readable areas, the role they need and any account already using the email."""
    users = {}
    emails = {r.email.lower() for r in requests}
    for user in get_user_model().objects.annotate(email_lower=Lower("email")).filter(email_lower__in=emails):
        users.setdefault(user.email_lower, []).append(user)
    for r in requests:
        r.area_labels = access.area_labels(r.areas)
        r.suggested_role = access.role_needed(r.areas)
        r.existing_users = users.get(r.email.lower(), [])
    return requests


@superuser_required
def access_requests(request):
    if request.method == "POST":
        try:
            decision = access.decide(
                request.POST.get("request_id"), request.POST.get("decision"), request.user,
                request.POST.get("note"), request.build_absolute_uri,
            )
        except access.AccessError as exc:
            messages.error(request, str(exc))
        else:
            who = decision.access_request.name
            if decision.access_request.status == AccessRequest.Status.DENIED:
                messages.success(request, f"Denied {who}'s request.")
            else:
                role = access.ROLE_LABELS[decision.access_request.granted_role]
                messages.success(request, f"Approved {who} as {role}.")
            if decision.email_error:
                extra = f" Send them this link to set a password: {decision.setup_url}" if decision.setup_url else ""
                messages.error(request, f"{who} could not be emailed ({decision.email_error}).{extra}")
        return redirect("access_requests")

    pending = list(AccessRequest.objects.filter(status=AccessRequest.Status.PENDING).order_by("created_at"))
    decided = list(
        AccessRequest.objects.exclude(status=AccessRequest.Status.PENDING)
        .select_related("decided_by", "user").order_by("-decided_at")[:RECENT_DECISIONS]
    )
    return render(request, "beetles/access_requests.html", {
        "pending": _with_context(pending),
        "decided": decided,
        "roles": access.ROLE_LABELS,
    })
