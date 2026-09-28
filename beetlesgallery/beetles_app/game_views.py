"""
Pages and JSON endpoints for the Beetle ID game. The game logic is in game.py.

Item payloads carry only an image URL and a bounding box: never the ROI id, its
label, or whether the item is a check, so the player cannot tell which answers are scored.
"""
import csv
import json

from django.contrib.admin.views.decorators import staff_member_required
from django.contrib.auth.decorators import login_required
from django.core.cache import cache
from django.db.models import Max
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from . import game
from .models import Beetles, GameAnswer, GameRound, Taxon

MODES = {m.value: m.label for m in GameRound.Mode}
PAIR_CHOICES = [(c.value, c.label) for c in GameAnswer.PairAnswer]
TAXA_CACHE_SECONDS = 600


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@login_required
def game_home(request):
    sort = "accuracy" if request.GET.get("sort") == "accuracy" else "labelled"
    return render(request, "beetles/game_home.html", {
        "summary": game.player_summary(request.user),
        "leaderboard": game.leaderboard(limit=25, sort=sort),
        "sort": sort,
    })


@login_required
def game_play(request, mode):
    if mode not in MODES:
        raise Http404("Unknown game mode")
    return render(request, "beetles/game_play.html", {
        "mode": mode,
        "mode_label": MODES[mode],
        "pair_choices": PAIR_CHOICES,
    })


# ---------------------------------------------------------------------------
# Round API
# ---------------------------------------------------------------------------
def _json_body(request):
    try:
        return json.loads(request.body or b"{}")
    except (ValueError, UnicodeDecodeError):
        return None


def _box(roi):
    return [roi.bbox_x, roi.bbox_y, roi.bbox_width, roi.bbox_height]


def _item_rois(item):
    """The Beetles rows of a round item, (a, b) with b None for classify. None if any is gone."""
    ids = [item["a"]] + ([item["b"]] if item.get("b") else [])
    found = {str(k): v for k, v in Beetles.objects.select_related("image_asset", "taxon").in_bulk(ids).items()}
    if any(i not in found for i in ids):
        return None
    return found[item["a"]], found.get(item.get("b"))


def _next_index(rnd, start=None):
    """First item at or after ``start`` (default: after the last answer) whose ROIs still exist."""
    if start is None:
        last = rnd.answers.aggregate(m=Max("index"))["m"]
        start = 0 if last is None else last + 1
    for i in range(start, len(rnd.items)):
        if _item_rois(rnd.items[i]) is not None:
            return i
    return None


def _item_payload(rnd, index):
    a, b = _item_rois(rnd.items[index])
    rois = [a] if b is None else ([b, a] if rnd.items[index].get("flip") else [a, b])
    return {
        "index": index,
        "position": rnd.answers.count() + 1,
        "total": len(rnd.items),
        "images": [{"url": r.display_url, "box": _box(r)} for r in rois],
    }


def _finish(rnd):
    if rnd.finished_at is None:
        rnd.finished_at = timezone.now()
        rnd.save(update_fields=["finished_at"])
    summary = game.player_summary(rnd.player)
    summary["round_labelled"] = rnd.answers.filter(skipped=False).count()
    return {"done": True, "summary": summary}


@login_required
@require_POST
def game_start(request):
    body = _json_body(request)
    mode = (body or {}).get("mode")
    if mode not in MODES:
        return JsonResponse({"error": "Unknown game mode."}, status=400)
    rnd = game.start_round(request.user, mode)
    index = _next_index(rnd, 0) if rnd else None
    if index is None:
        return JsonResponse({
            "error": "There are no images ready for this game yet. Please check back later."
        }, status=404)
    return JsonResponse({"round": str(rnd.id), "item": _item_payload(rnd, index)})


def _clean_classification(body):
    """The rank values from a classify answer, or None if they don't match the taxonomy."""
    answer = {r: (body.get(r) or "").strip()[:100] for r in game.RANKS}
    if answer["species"] and not answer["genus"]:
        return None
    if not any(answer.values()):
        return None
    filters = {f"{r}__iexact": v for r, v in answer.items() if v}
    if not Taxon.objects.filter(**filters).exists():
        return None
    return answer


@login_required
@require_POST
def game_answer(request, round_id):
    rnd = get_object_or_404(GameRound, id=round_id, player=request.user)
    body = _json_body(request)
    if body is None:
        return JsonResponse({"error": "Invalid request."}, status=400)
    if rnd.finished_at is not None:
        return JsonResponse({"error": "This round is already finished."}, status=409)

    index = _next_index(rnd)
    if index is None:
        return JsonResponse(_finish(rnd))
    if body.get("index") != index:
        return JsonResponse({"error": "Out of step with the round; please reload."}, status=409)

    item = rnd.items[index]
    roi_a, roi_b = _item_rois(item)
    record = GameAnswer(
        round=rnd, player=request.user, mode=rnd.mode, index=index,
        is_check=bool(item.get("check")), roi=roi_a, roi_b=roi_b,
        skipped=bool(body.get("skipped")),
    )
    scores = {}
    if not record.skipped:
        if rnd.mode == GameRound.Mode.CLASSIFY:
            answer = _clean_classification(body)
            if answer is None:
                return JsonResponse({"error": "Please choose a name from the lists."}, status=400)
            for r, v in answer.items():
                setattr(record, r, v)
            if record.is_check:
                scores = game.score_classification(answer, roi_a.taxon)
        else:
            choice = body.get("pair_answer")
            if choice not in dict(PAIR_CHOICES):
                return JsonResponse({"error": "Please choose an answer."}, status=400)
            record.pair_answer = choice
            if record.is_check:
                scores = game.score_pair(choice, roi_a.taxon, roi_b.taxon)
    for r, ok in scores.items():
        setattr(record, f"correct_{r}", ok)
    record.save()

    nxt = _next_index(rnd, index + 1)
    if nxt is None:
        return JsonResponse(_finish(rnd))
    return JsonResponse({"item": _item_payload(rnd, nxt)})


# ---------------------------------------------------------------------------
# Taxonomy pickers
# ---------------------------------------------------------------------------
@login_required
@require_GET
def game_taxa(request):
    """
    Options for one picker. ``rank`` is the list wanted; the chosen higher ranks
    narrow it. Genus options also carry their subfamily and tribe so picking a genus
    can fill those in.
    """
    rank = request.GET.get("rank")
    if rank not in game.RANKS:
        return JsonResponse({"error": "Unknown rank."}, status=400)
    parents = {r: (request.GET.get(r) or "").strip() for r in game.RANKS[: game.RANKS.index(rank)]}
    if rank == "species" and not parents.get("genus"):
        return JsonResponse({"options": []})

    key = "game_taxa:" + rank + ":" + "|".join(parents.get(r, "") for r in game.RANKS[:3])
    options = cache.get(key)
    if options is None:
        qs = Taxon.objects.exclude(**{f"{rank}__isnull": True}).exclude(**{rank: ""})
        qs = qs.filter(**{f"{r}__iexact": v for r, v in parents.items() if v})
        if rank == "genus":
            seen = {}
            for genus, subfamily, tribe in qs.values_list("genus", "subfamily", "tribe").order_by("genus"):
                seen.setdefault(genus, {"value": genus, "subfamily": subfamily or "", "tribe": tribe or ""})
            options = list(seen.values())
        else:
            values = qs.values_list(rank, flat=True).distinct().order_by(rank)
            options = [{"value": v} for v in values]
        cache.set(key, options, TAXA_CACHE_SECONDS)
    return JsonResponse({"options": options})


@login_required
@require_GET
def game_taxa_search(request):
    """Jump straight to a genus or species by typing part of its name."""
    q = (request.GET.get("q") or "").strip()
    if len(q) < 2:
        return JsonResponse({"results": []})
    results = []
    for genus, subfamily, tribe in (
        Taxon.objects.filter(genus__istartswith=q)
        .values_list("genus", "subfamily", "tribe").distinct().order_by("genus")[:5]
    ):
        results.append({
            "label": genus, "kind": "genus",
            "subfamily": subfamily or "", "tribe": tribe or "", "genus": genus, "species": "",
        })
    species_qs = (
        Taxon.objects.filter(scientific_name__icontains=q)
        .exclude(species__isnull=True).exclude(species="")
        .values_list("scientific_name", "subfamily", "tribe", "genus", "species")
        .order_by("scientific_name")[:15]
    )
    for name, subfamily, tribe, genus, species in species_qs:
        results.append({
            "label": name, "kind": "species",
            "subfamily": subfamily or "", "tribe": tribe or "", "genus": genus or "", "species": species,
        })
    return JsonResponse({"results": results})


# ---------------------------------------------------------------------------
# Staff review
# ---------------------------------------------------------------------------
def _player_rows():
    from django.contrib.auth import get_user_model

    reliability = game.player_reliability()
    labelled = {row["player_id"]: row["labelled"] for row in game.leaderboard(limit=None)}
    ids = set(reliability) | set(labelled)
    users = get_user_model().objects.in_bulk(ids)
    rows = []
    for pid in ids:
        rel = reliability.get(pid) or {m: game.default_weight() for m in ("classify", "pair", "all")}
        rows.append({
            "username": users[pid].username if pid in users else "?",
            "labelled": labelled.get(pid, 0),
            "classify": [rel["classify"][r] for r in game.RANKS],
            "pair": [rel["pair"][r] for r in game.RANKS],
        })
    rows.sort(key=lambda r: (-r["labelled"], r["username"]))
    return rows


@staff_member_required
def game_review(request):
    return render(request, "beetles/game_review.html", {
        "ranks": game.RANKS,
        "players": _player_rows(),
        "consensus": game.consensus(limit=200),
        "rounds": GameRound.objects.count(),
        "answers": GameAnswer.objects.count(),
    })


def _pct(value):
    return "" if value is None else f"{value:.3f}"


@staff_member_required
def game_export(request, kind):
    response = HttpResponse(content_type="text/csv")
    writer = csv.writer(response)
    if kind == "labels":
        response["Content-Disposition"] = 'attachment; filename="game_label_consensus.csv"'
        header = ["roi_id", "image_id", "current_valid_name_id", "answers", "players"]
        for r in game.RANKS:
            header += [f"{r}", f"{r}_support", f"{r}_votes"]
        writer.writerow(header)
        for entry in game.consensus():
            roi = entry["roi"]
            row = [roi.id, roi.image_asset_id, roi.depicts_valid_name_id or "", entry["answers"], entry["players"]]
            for r in game.RANKS:
                vote = entry["ranks"][r]
                row += [vote["value"], _pct(vote["support"]), vote["votes"]] if vote else ["", "", ""]
            writer.writerow(row)
    elif kind == "players":
        response["Content-Disposition"] = 'attachment; filename="game_player_reliability.csv"'
        header = ["username", "labelled"]
        for mode in ("classify", "pair"):
            for r in game.RANKS:
                header += [f"{mode}_{r}_correct", f"{mode}_{r}_judged", f"{mode}_{r}_accuracy"]
        writer.writerow(header)
        for p in _player_rows():
            row = [p["username"], p["labelled"]]
            for mode in ("classify", "pair"):
                for cell in p[mode]:
                    row += [cell["ok"], cell["n"], _pct(cell["accuracy"])]
            writer.writerow(row)
    else:
        raise Http404("Unknown export")
    return response
