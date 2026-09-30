"""Superuser page to load proposed interactions from a CSV (the collector's output, or a hand-made file)."""
import os

from django.conf import settings
from django.shortcuts import render

from .interaction_proposals import import_proposals
from .views import _format_size, superuser_required


@superuser_required
def upload_interaction_proposals(request):
    """Everything is checked first; if any row is wrong nothing is saved and each problem is listed."""
    limit = getattr(settings, "MAX_UPLOAD_SIZE_INTERACTIONS", 20 * 1024 * 1024)
    context = {"max_mb": limit // (1024 * 1024)}
    if request.method == "POST":
        csv_file = request.FILES.get("csv_file")
        if not csv_file:
            context["error"] = "Please attach a .csv file."
        elif os.path.splitext(csv_file.name)[1].lower() != ".csv":
            context["error"] = "The proposals file must be a .csv."
        elif csv_file.size and csv_file.size > limit:
            context["error"] = f"The file is too large ({_format_size(csv_file.size)}); the limit is {_format_size(limit)}."
        else:
            context["result"] = import_proposals(
                csv_file, user=request.user, dry_run=bool(request.POST.get("dry_run")),
                default_collector=request.POST.get("collector", "").strip()[:100],
            )
            context["filename"] = csv_file.name
    return render(request, "beetles/upload_interaction_proposals.html", context)
