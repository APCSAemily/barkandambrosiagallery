"""
Round feedback and player reports for the Beetle ID game.

After a round, players see every item with their answer next to what the database
says: the validated label (with a tick or cross per rank) or, for items that aren't
verified yet, the current unverified label and what other players have said.

Players can report an ROI that looks wrong. While a report is open the ROI is kept out
of scoring (game.check_rois) and the reporter's own scored answers on it are held
(GameAnswer.score_hold), so reporting a bad reference never costs them. Staff then
resolve it: "corrected" re-scores every answer on the ROI against its fixed label (or
voids them if it is no longer validated); "confirmed" releases the held answers.
"""
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from . import game
from .models import Beetles, GameAnswer, GameReport


# ---------------------------------------------------------------------------
# Feedback
# ---------------------------------------------------------------------------
def _label(taxon):
    """Display ranks for a taxon (or None)."""
    if taxon is None:
        return None
    return {
        "subfamily": taxon.subfamily or "", "tribe": taxon.tribe or "", "genus": taxon.genus or "",
        "species": f"{taxon.genus} {taxon.species}" if taxon.genus and taxon.species else "",
        "name": taxon.scientific_name or "",
    }


def _answer_label(answer):
    return {
        "subfamily": answer.subfamily, "tribe": answer.tribe, "genus": answer.genus,
        "species": f"{answer.genus} {answer.species}" if answer.genus and answer.species else "",
    }


def _results(answer):
    """Per rank: True/False when judged, None otherwise."""
    return {r: getattr(answer, f"correct_{r}") for r in game.RANKS}


def _verdict(answer):
    """ "right", "partly", "wrong" or None for an answer that wasn't judged."""
    judged = [v for v in _results(answer).values() if v is not None]
    if not judged:
        return None
    if all(judged):
        return "right"
    return "partly" if any(judged) else "wrong"


def _side(roi, player_reports, others):
    """What the database says about one ROI in a feedback item."""
    verified = bool(roi.bbox_is_validated and roi.taxon)
    report = player_reports.get(roi.id)
    return {
        "roi_id": str(roi.id),
        "url": roi.display_url,
        "box": [roi.bbox_x, roi.bbox_y, roi.bbox_width, roi.bbox_height],
        "verified": verified,
        "label": _label(roi.taxon),
        "others": others.get(roi.id),
        "report": None if report is None else {"status": report.status, "reason": report.get_reason_display()},
    }


def round_feedback(rnd):
    """Every answered item of a finished round with the database's view of it."""
    answers = list(
        rnd.answers.select_related("roi__taxon", "roi__image_asset", "roi_b__taxon", "roi_b__image_asset")
        .order_by("index")
    )
    roi_ids = {a.roi_id for a in answers} | {a.roi_b_id for a in answers if a.roi_b_id}
    player_reports = {
        r.roi_id: r for r in GameReport.objects.filter(reporter=rnd.player, roi_id__in=roi_ids).order_by("created_at")
    }
    # What other players have said about the unverified ones (weighted consensus).
    others = {}
    for entry in game.consensus(roi_ids=[a.roi_id for a in answers if not a.is_check]):
        ranks = entry["ranks"]
        deepest = next((ranks[r] for r in reversed(game.RANKS) if ranks[r]), None)
        if deepest:
            others[entry["roi"].id] = {"value": deepest["value"], "answers": entry["answers"]}

    items = []
    right = scored = 0
    for a in answers:
        sides = [_side(a.roi, player_reports, others)]
        if a.roi_b_id:
            sides.append(_side(a.roi_b, player_reports, others))
            if rnd.items[a.index].get("flip"):
                sides.reverse()
        verdict = _verdict(a) if a.is_check and not a.score_hold else None
        if verdict:
            scored += 1
            right += verdict == "right"
        truth_pair = None
        if a.mode == "pair" and a.roi.taxon and a.roi_b and a.roi_b.taxon and a.is_check:
            same = game.shared_ranks(a.roi.taxon, a.roi_b.taxon)
            deepest = next((r for r in reversed(game.RANKS) if same[r]), None)
            truth_pair = dict(game.GameAnswer.PairAnswer.choices).get(deepest or "different")
        items.append({
            "index": a.index,
            "mode": a.mode,
            "skipped": a.skipped,
            "scored": a.is_check,
            "held": a.score_hold,
            "verdict": verdict,
            "results": _results(a),
            "answer": _answer_label(a) if a.mode == "classify" else a.get_pair_answer_display(),
            "truth_pair": truth_pair,
            "sides": sides,
        })
    return {"items": items, "right": right, "scored": scored}


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------
def _hold_filter(roi, player=None):
    q = Q(is_check=True) & (Q(roi=roi) | Q(roi_b=roi))
    if player is not None:
        q &= Q(player=player)
    return q


@transaction.atomic
def create_report(player, roi, reason, note="", answer=None):
    """
    Record a report and hold the reporter's scored answers on this ROI. Returns the
    report (the existing one if this player already has an open report on the ROI).
    """
    existing = GameReport.objects.filter(roi=roi, reporter=player, status=GameReport.Status.OPEN).first()
    if existing:
        return existing
    report = GameReport.objects.create(
        roi=roi, reporter=player, answer=answer, reason=reason, note=note[:1000],
        was_validated=roi.bbox_is_validated, label_at_report=roi.depicts_valid_name_id or "",
    )
    GameAnswer.objects.filter(_hold_filter(roi, player)).update(score_hold=True)
    _refresh(player_ids=[player.id], roi_ids=[roi.id])
    return report


def rescore_roi(roi):
    """
    Re-score every scored answer on this ROI against its current label. If the ROI is
    no longer a usable reference (unvalidated or no taxon), its answers are voided.
    Returns the ids of the players affected.
    """
    roi = Beetles.objects.select_related("taxon").get(id=roi.id)
    usable = roi.bbox_is_validated and roi.taxon is not None and not roi.is_deleted
    players = set()
    for ans in GameAnswer.objects.filter(_hold_filter(roi)).select_related("roi__taxon", "roi_b__taxon"):
        players.add(ans.player_id)
        if not usable:
            ans.score_hold = True
            ans.save(update_fields=["score_hold"])
            continue
        if ans.mode == "classify":
            scores = game.score_classification(
                {"subfamily": ans.subfamily, "tribe": ans.tribe, "genus": ans.genus, "species": ans.species},
                roi.taxon,
            ) if not ans.skipped else {r: None for r in game.RANKS}
            ans.ref_subfamily = roi.taxon.subfamily or ""
            ans.ref_tribe = roi.taxon.tribe or ""
            ans.ref_genus = roi.taxon.genus or ""
            ans.ref_species = roi.taxon.species or ""
        else:
            other = ans.roi_b if ans.roi_id == roi.id else ans.roi
            if other is None or other.taxon is None:
                ans.score_hold = True
                ans.save(update_fields=["score_hold"])
                continue
            a_taxon = roi.taxon if ans.roi_id == roi.id else ans.roi.taxon
            b_taxon = roi.taxon if ans.roi_b_id == roi.id else ans.roi_b.taxon
            scores = game.score_pair(ans.pair_answer, a_taxon, b_taxon) if not ans.skipped else {
                r: None for r in game.RANKS
            }
        for r, ok in scores.items():
            setattr(ans, f"correct_{r}", ok)
        ans.score_hold = False
        ans.save()
    return players


@transaction.atomic
def resolve_reports(roi, outcome, staff, note=""):
    """
    Close every open report on this ROI.

    corrected  staff fixed the ROI: re-score all answers on it against the new label
               (voided if it is no longer validated).
    confirmed  the label was right: the reporters' held answers count again.
    """
    reports = list(GameReport.objects.filter(roi=roi, status=GameReport.Status.OPEN))
    if outcome == GameReport.Status.CORRECTED:
        players = rescore_roi(roi)
    elif outcome == GameReport.Status.CONFIRMED:
        players = {r.reporter_id for r in reports}
        GameAnswer.objects.filter(_hold_filter(roi), player_id__in=players).update(score_hold=False)
    else:
        raise ValueError(outcome)
    GameReport.objects.filter(id__in=[r.id for r in reports]).update(
        status=outcome, resolved_by=staff, resolved_at=timezone.now(), staff_note=note[:1000],
    )
    _refresh(player_ids=players, roi_ids=[roi.id])
    return len(reports)


def _refresh(player_ids, roi_ids):
    """Recompute what depends on scores after holds or re-scoring changed them."""
    from django.contrib.auth import get_user_model
    from django.core.cache import cache

    from .game_trust import INDEX_CACHE_KEY, recompute_skills

    for player in get_user_model().objects.filter(id__in=list(player_ids)):
        recompute_skills(player)
    game.update_difficulty(roi_ids)
    cache.delete(INDEX_CACHE_KEY)  # testable-branch counts change with the check pool
