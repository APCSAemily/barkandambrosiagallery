"""
Beetle ID game: picking items, scoring answers, player reliability and label consensus.

Two modes:
  classify  one region of interest (ROI) is shown; the player names its subfamily,
            tribe, genus and species, stopping at any rank.
  pair      two ROIs are shown; the player says the deepest rank they share.

Some items in every round are "checks": items with a validated answer
(``Beetles.bbox_is_validated``). Only checks are scored, and the player is never told
which items they were. Answers on the other items are the labels we collect; staff see
them combined by each player's reliability.

The views live in game_views.py; this module has no request handling.
"""
import random
from collections import defaultdict

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Count, Q

from .models import Beetles, GameAnswer, GameRound

RANKS = ("subfamily", "tribe", "genus", "species")

# Pair answers ordered by depth: the index is the deepest shared rank (-1 = none shared).
PAIR_DEPTH = {"different": -1, "subfamily": 0, "tribe": 1, "genus": 2, "species": 3}


def game_setting(name, default):
    return getattr(settings, name, default)


# ---------------------------------------------------------------------------
# Item pools
# ---------------------------------------------------------------------------
def playable_rois():
    """ROIs that can be shown: a live bounding box on a live image with a file."""
    return (
        Beetles.objects.filter(
            is_deleted=False,
            bbox_x__isnull=False,
            bbox_y__isnull=False,
            bbox_width__gt=0,
            bbox_height__gt=0,
            image_asset__isnull=False,
            image_asset__is_deleted=False,
        )
        .exclude(image_asset__image_file="")
        .exclude(image_asset__image_file__isnull=True)
    )


def check_rois():
    """ROIs with a validated label to score against."""
    return playable_rois().filter(bbox_is_validated=True, taxon__isnull=False)


def open_rois():
    """ROIs whose label has not been validated: the ones we collect labels for."""
    return playable_rois().filter(bbox_is_validated=False)


def _random_ids(qs, n, exclude=()):
    if n <= 0:
        return []
    if exclude:
        qs = qs.exclude(id__in=list(exclude))
    return list(qs.order_by("?").values_list("id", flat=True)[:n])


def _pick_fresh(qs, n, seen):
    """Up to n random ids from qs, preferring ones the player has not seen yet."""
    ids = _random_ids(qs, n, exclude=seen)
    if len(ids) < n:
        ids += _random_ids(qs, n - len(ids), exclude=set(ids))
    return ids


# ---------------------------------------------------------------------------
# Round building
# ---------------------------------------------------------------------------
def check_ratio(player, mode):
    """
    Share of check items for this player's next round.

    New players get more checks until their reliability can be estimated, then fewer
    so most of their effort goes into new labels.
    """
    scored = GameAnswer.objects.filter(
        player=player, mode=mode, is_check=True, skipped=False
    ).count()
    if scored < game_setting("GAME_CALIBRATION_CHECKS", 20):
        return game_setting("GAME_CHECK_RATIO_NEW", 0.6)
    return game_setting("GAME_CHECK_RATIO_KNOWN", 0.2)


def _split_round(player, mode, size):
    """How many check and open items go into a round of ``size``."""
    n_checks = max(1, round(size * check_ratio(player, mode)))
    return n_checks, size - n_checks


def _seen_roi_ids(player, mode, is_check):
    return set(
        GameAnswer.objects.filter(player=player, mode=mode, is_check=is_check)
        .values_list("roi_id", flat=True)
    )


def _fill(n_checks, n_open, pick_checks, pick_open):
    """Pick checks and open items, topping up from the other pool when one runs short."""
    checks = pick_checks(n_checks)
    opens = pick_open(n_open + (n_checks - len(checks)))
    if len(opens) < n_open:
        checks += pick_checks(n_open - len(opens), exclude={c["a"] for c in checks})
    return checks, opens


def build_classify_items(player, size):
    n_checks, n_open = _split_round(player, "classify", size)
    seen_checks = _seen_roi_ids(player, "classify", True)
    seen_open = _seen_roi_ids(player, "classify", False)

    def pick_checks(n, exclude=()):
        ids = _pick_fresh(check_rois(), n, seen_checks | set(exclude))
        return [{"a": str(i), "b": None, "check": True} for i in ids]

    def pick_open(n):
        ids = _pick_fresh(open_rois(), n, seen_open)
        return [{"a": str(i), "b": None, "check": False} for i in ids]

    checks, opens = _fill(n_checks, n_open, pick_checks, pick_open)
    return checks + opens


def _partner_for(anchor, exclude_ids):
    """
    A validated ROI to pair with ``anchor``, at a randomly chosen relation
    (same species / genus / tribe / subfamily / different), so answers are spread
    across ranks rather than being mostly "different subfamily".

    For an unvalidated anchor its current (unchecked) label is only used to aim the
    pairing; the answer is what we record.
    """
    pool = check_rois().exclude(id__in=list(exclude_ids))
    if anchor.image_asset_id:
        pool = pool.exclude(image_asset_id=anchor.image_asset_id)
    taxon = anchor.taxon
    if taxon is None:
        ids = _random_ids(pool, 1)
        return ids[0] if ids else None

    # relation: (filter, whether the anchor has the ranks the filter needs)
    relations = {
        "species": (Q(taxon_id=taxon.id), True),
        "genus": (Q(taxon__genus=taxon.genus) & ~Q(taxon_id=taxon.id), bool(taxon.genus)),
        "tribe": (Q(taxon__tribe=taxon.tribe) & ~Q(taxon__genus=taxon.genus),
                  bool(taxon.tribe and taxon.genus)),
        "subfamily": (Q(taxon__subfamily=taxon.subfamily) & ~Q(taxon__tribe=taxon.tribe),
                      bool(taxon.subfamily and taxon.tribe)),
        "different": (~Q(taxon__subfamily=taxon.subfamily), bool(taxon.subfamily)),
    }
    order = list(relations)
    random.shuffle(order)
    for rel in order:
        condition, usable = relations[rel]
        if not usable:
            continue
        ids = _random_ids(pool.filter(condition), 1)
        if ids:
            return ids[0]
    ids = _random_ids(pool, 1)
    return ids[0] if ids else None


def build_pair_items(player, size):
    n_checks, n_open = _split_round(player, "pair", size)
    seen_checks = _seen_roi_ids(player, "pair", True)
    seen_open = _seen_roi_ids(player, "pair", False)

    def make_pairs(anchor_qs, n, seen, is_check, exclude=()):
        items = []
        anchors = Beetles.objects.select_related("taxon").filter(
            id__in=_pick_fresh(anchor_qs, n, seen | set(exclude))
        )
        for anchor in anchors:
            partner = _partner_for(anchor, {anchor.id})
            if partner is None:
                continue
            items.append({
                "a": str(anchor.id), "b": str(partner), "check": is_check,
                "flip": random.random() < 0.5,
            })
        return items

    checks, opens = _fill(
        n_checks, n_open,
        lambda n, exclude=(): make_pairs(check_rois(), n, seen_checks, True, exclude),
        lambda n: make_pairs(open_rois(), n, seen_open, False),
    )
    return checks + opens


def start_round(player, mode, size=None):
    """Create a round with freshly picked items in random order. Returns None if nothing is playable."""
    size = size or game_setting("GAME_ROUND_SIZE", 10)
    builder = build_classify_items if mode == GameRound.Mode.CLASSIFY else build_pair_items
    items = builder(player, size)
    if not items:
        return None
    random.shuffle(items)
    return GameRound.objects.create(player=player, mode=mode, items=items)


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------
def _norm(value):
    return (value or "").strip().lower()


def rank_values(taxon):
    """{rank: value} for a Taxon; species is 'genus species' so the epithet alone never matches."""
    if taxon is None:
        return {r: "" for r in RANKS}
    species = f"{_norm(taxon.genus)} {_norm(taxon.species)}" if taxon.genus and taxon.species else ""
    return {
        "subfamily": _norm(taxon.subfamily),
        "tribe": _norm(taxon.tribe),
        "genus": _norm(taxon.genus),
        "species": species,
    }


def answer_values(answer):
    """{rank: value} for a classification answer dict (same shape as rank_values)."""
    species = ""
    if answer.get("species") and answer.get("genus"):
        species = f"{_norm(answer['genus'])} {_norm(answer['species'])}"
    return {
        "subfamily": _norm(answer.get("subfamily")),
        "tribe": _norm(answer.get("tribe")),
        "genus": _norm(answer.get("genus")),
        "species": species,
    }


def score_classification(answer, truth_taxon):
    """
    Per-rank correctness of a classification against the validated taxon.
    None for a rank the player did not answer or that has no reference value.
    """
    given = answer_values(answer)
    truth = rank_values(truth_taxon)
    return {
        r: (given[r] == truth[r]) if given[r] and truth[r] else None
        for r in RANKS
    }


def shared_ranks(taxon_a, taxon_b):
    """
    Per rank: do the two taxa share it? True/False, or None when either lacks the rank.
    Sharing a deeper rank implies sharing the ones above it even where those are blank.
    """
    a, b = rank_values(taxon_a), rank_values(taxon_b)
    same = {r: (a[r] == b[r]) if a[r] and b[r] else None for r in RANKS}
    for i, r in enumerate(RANKS):
        if same[r] is None and any(same[deeper] for deeper in RANKS[i + 1:]):
            same[r] = True
    return same


def score_pair(pair_answer, taxon_a, taxon_b):
    """
    Per-rank correctness of a pair answer. "Same genus" claims the pair shares
    subfamily, tribe and genus but not species; each of those claims is scored.
    """
    if pair_answer not in PAIR_DEPTH:
        return {r: None for r in RANKS}
    depth = PAIR_DEPTH[pair_answer]
    truth = shared_ranks(taxon_a, taxon_b)
    return {
        r: ((i <= depth) == truth[r]) if truth[r] is not None else None
        for i, r in enumerate(RANKS)
    }


# ---------------------------------------------------------------------------
# Player statistics
# ---------------------------------------------------------------------------
def _rank_counts():
    """Aggregate expressions: correct and judged counts for each rank."""
    exprs = {}
    for r in RANKS:
        exprs[f"{r}_ok"] = Count("id", filter=Q(**{f"correct_{r}": True}))
        exprs[f"{r}_n"] = Count("id", filter=Q(**{f"correct_{r}__isnull": False}))
    return exprs


def _accuracy(row):
    ok = sum(row[f"{r}_ok"] for r in RANKS)
    n = sum(row[f"{r}_n"] for r in RANKS)
    return (ok / n if n else None), n


def player_summary(player):
    """Items labelled and overall accuracy (share of judged ranks correct on checks)."""
    labelled = GameAnswer.objects.filter(player=player, skipped=False).count()
    row = GameAnswer.objects.filter(player=player, is_check=True).aggregate(**_rank_counts())
    accuracy, judged = _accuracy(row)
    min_judged = game_setting("GAME_MIN_JUDGED_FOR_ACCURACY", 10)
    return {
        "labelled": labelled,
        "accuracy": accuracy if judged >= min_judged else None,
    }


def leaderboard(limit=50, sort="labelled"):
    labelled = {
        row["player"]: row["n"]
        for row in GameAnswer.objects.filter(skipped=False)
        .values("player").annotate(n=Count("id"))
    }
    names = dict(
        get_user_model().objects.filter(id__in=labelled).values_list("id", "username")
    )
    min_judged = game_setting("GAME_MIN_JUDGED_FOR_ACCURACY", 10)
    accuracy = {}
    for row in (
        GameAnswer.objects.filter(is_check=True)
        .values("player").annotate(**_rank_counts())
    ):
        acc, judged = _accuracy(row)
        if judged >= min_judged:
            accuracy[row["player"]] = acc

    rows = [
        {"player_id": pid, "username": names.get(pid, "?"), "labelled": n, "accuracy": accuracy.get(pid)}
        for pid, n in labelled.items()
    ]
    if sort == "accuracy":
        rows.sort(key=lambda r: (r["accuracy"] is None, -(r["accuracy"] or 0), -r["labelled"]))
    else:
        rows.sort(key=lambda r: (-r["labelled"], r["username"]))
    for i, row in enumerate(rows, start=1):
        row["position"] = i
    return rows[:limit]


def player_reliability():
    """
    {player_id: {mode: {rank: {"ok", "n", "accuracy", "weight"}}}} from check answers,
    plus mode "all" combining both modes.

    ``weight`` is the Laplace-smoothed accuracy (ok + 1) / (n + 2): a player with no
    checks at a rank counts as a coin flip, and the weight moves toward their real
    accuracy as they answer more checks.
    """
    out = defaultdict(dict)
    rows = GameAnswer.objects.filter(is_check=True).values("player", "mode").annotate(**_rank_counts())
    for row in rows:
        out[row["player"]][row["mode"]] = row
    result = {}
    for pid, modes in out.items():
        result[pid] = {}
        for mode in ("classify", "pair", "all"):
            result[pid][mode] = {}
            for r in RANKS:
                if mode == "all":
                    ok = sum(m[f"{r}_ok"] for m in modes.values())
                    n = sum(m[f"{r}_n"] for m in modes.values())
                else:
                    ok = modes.get(mode, {}).get(f"{r}_ok", 0)
                    n = modes.get(mode, {}).get(f"{r}_n", 0)
                result[pid][mode][r] = {
                    "ok": ok, "n": n,
                    "accuracy": ok / n if n else None,
                    "weight": (ok + 1) / (n + 2),
                }
    return result


def default_weight():
    return {r: {"ok": 0, "n": 0, "accuracy": None, "weight": 0.5} for r in RANKS}


# ---------------------------------------------------------------------------
# Consensus on unvalidated ROIs
# ---------------------------------------------------------------------------
def implied_labels(answer):
    """
    The rank values an answer on an unvalidated item says the ROI has.

    Classify: what the player picked. Pair: the ranks the player says the unvalidated
    ROI shares with its validated partner, taken from the partner's taxon. "Different
    subfamily" and "not sure" say nothing positive, so they imply nothing.
    """
    if answer.skipped:
        return {}
    if answer.mode == "classify":
        values = {
            "subfamily": answer.subfamily, "tribe": answer.tribe, "genus": answer.genus,
            "species": f"{answer.genus} {answer.species}" if answer.genus and answer.species else "",
        }
        return {r: v for r, v in values.items() if v}
    depth = PAIR_DEPTH.get(answer.pair_answer, -1)
    partner = answer.roi_b.taxon if answer.roi_b_id and answer.roi_b else None
    if depth < 0 or partner is None:
        return {}
    values = {
        "subfamily": partner.subfamily, "tribe": partner.tribe, "genus": partner.genus,
        "species": f"{partner.genus} {partner.species}" if partner.genus and partner.species else "",
    }
    return {r: values[r] for r in RANKS[: depth + 1] if values[r]}


def consensus(limit=None):
    """
    Reliability-weighted votes for each unvalidated ROI that has answers.

    Returns a list of dicts sorted by number of answers (most first):
    {"roi": Beetles, "answers": int, "ranks": {rank: {"value", "support", "votes"}}}
    ``support`` is the winning value's share of the total vote weight at that rank.
    """
    reliability = player_reliability()
    answers = (
        GameAnswer.objects.filter(is_check=False, skipped=False)
        .select_related("roi", "roi__taxon", "roi_b__taxon")
        .order_by("roi_id")
    )
    per_roi = {}
    for ans in answers.iterator(chunk_size=2000):
        entry = per_roi.setdefault(ans.roi_id, {
            "roi": ans.roi, "answers": 0, "players": set(),
            "tally": {r: defaultdict(float) for r in RANKS},
            "count": {r: defaultdict(int) for r in RANKS},
        })
        entry["answers"] += 1
        entry["players"].add(ans.player_id)
        weights = reliability.get(ans.player_id, {}).get("all") or default_weight()
        for rank, value in implied_labels(ans).items():
            entry["tally"][rank][value] += weights[rank]["weight"]
            entry["count"][rank][value] += 1

    results = []
    for entry in per_roi.values():
        ranks = {}
        for r in RANKS:
            tally = entry["tally"][r]
            if not tally:
                ranks[r] = None
                continue
            value = max(tally, key=tally.get)
            ranks[r] = {
                "value": value,
                "support": tally[value] / sum(tally.values()),
                "votes": entry["count"][r][value],
            }
        results.append({
            "roi": entry["roi"], "answers": entry["answers"],
            "players": len(entry["players"]), "ranks": ranks,
            "rank_list": [(r, ranks[r]) for r in RANKS],
        })
    results.sort(key=lambda e: (-e["answers"], str(e["roi"].id)))
    return results[:limit] if limit else results
