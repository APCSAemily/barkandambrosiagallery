"""
Finer access than the three roles, set per person on the My Account page.

The roles stay the defaults: a Standard user has the gallery, classifier and game; Staff also have every area below;
Superusers have everything. An area grant adds one of these areas to a person on top of their role (it never removes
anything), so a Standard user can be given, say, the interactions review without becoming Staff.
"""
from functools import wraps

from django.contrib.auth.views import redirect_to_login

ANNOTATE, UPLOAD, INTERACTIONS = "annotate", "upload", "interactions"
AREAS = [
    (ANNOTATE, "Annotate and validate", "The annotation page, validating and editing ROI and image records."),
    (UPLOAD, "Upload and update images", "Upload new images and metadata, and update existing records from a CSV."),
    (INTERACTIONS, "Ecological interactions", "Review proposed interactions, and upload or update interactions."),
]
KEYS = [key for key, _, _ in AREAS]


def granted_areas(user):
    """The areas explicitly granted to this user (not counting their role)."""
    if not getattr(user, "is_authenticated", False):
        return set()
    cached = getattr(user, "_granted_areas", None)
    if cached is None:
        from .models import AreaGrant
        cached = user._granted_areas = set(AreaGrant.objects.filter(user=user).values_list("area", flat=True))
    return cached


def has_area(user, area):
    """Active staff and superusers have every area; anyone else has the areas granted to them."""
    if not (getattr(user, "is_authenticated", False) and user.is_active):
        return False
    return user.is_staff or user.is_superuser or area in granted_areas(user)


def area_required(area):
    """Like staff_member_required, for one area: sends people without it to the login page."""
    def decorator(view):
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            if has_area(request.user, area):
                return view(request, *args, **kwargs)
            return redirect_to_login(request.get_full_path(), "login")
        return wrapped
    return decorator


def areas_for_templates(request):
    """{'areas': {'annotate': bool, ...}} for templates, e.g. {% if areas.annotate %}."""
    user = getattr(request, "user", None)
    return {"areas": {key: has_area(user, key) for key in KEYS}}
