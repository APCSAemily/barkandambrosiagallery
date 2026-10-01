"""
Beetle ID game: picking items, scoring answers, player statistics and label consensus.

Two modes:
  classify  one region of interest (ROI) is shown; the player names its subfamily,
            tribe, genus and species, stopping at any rank.
  pair      two ROIs are shown; the player says the deepest rank they share.

Some items in every round are "checks": items with a validated answer
(``Beetles.bbox_is_validated``). Only checks are scored, and the player is never told
which items they were. Answers on the other items are the labels we collect.

Items are matched to players by difficulty (RoiDifficulty): newer or weaker players
get easier images, and the target rises as they play. Some checks are aimed at the
branches a player has been labelling, so they get the chance to prove themselves
there (see game_trust.py for how expertise and trusted labels work).

The views live in game_views.py; this module has no request handling.
"""
import math
import random
import uuid
from collections import defaultdict
from datetime import datetime, timedelta

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Count, Q
from django.utils import timezone

from .models import Beetles, GameAnswer, GameRound, RoiDifficulty

RANKS = ("subfamily", "tribe", "genus", "species")

# Pair answers ordered by depth: the index is the deepest shared rank (-1 = none shared).
PAIR_DEPTH = {"different": -1, "subfamily": 0, "tribe": 1, "genus": 2, "species": 3}


def game_setting(name, default):
    return getattr(settings, name, default)


# ---------------------------------------------------------------------------
# Item pools
# ---------------------------------------------------------------------------
# A usable reference taxon has at least subfamily and genus. This also drops the
# handful of malformed rows in the species list whose columns are shifted (their
# "subfamily" is a species epithet and genus is blank).
COMPLETE_TAXON = ~Q(subfamily__isnull=True) & ~Q(subfamily="") & ~Q(genus__isnull=True) & ~Q(genus="")


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
    """
    ROIs with a validated, well-formed label to score against. ROIs with an open
    player report are left out until staff have looked at them.
    """
    return playable_rois().filter(bbox_is_validated=True, taxon__isnull=False).exclude(
        Q(taxon__subfamily="") | Q(taxon__subfamily__isnull=True)
        | Q(taxon__genus="") | Q(taxon__genus__isnull=True)
        | Q(game_reports__status="open")
    )


def open_rois():
    """ROIs whose label has not been validated: the ones we collect labels for."""
    return playable_rois().filter(bbox_is_validated=False)


def _random_ids(qs, n):
    """
    Up to n ids from qs in random order.

    Starts at a random UUID and walks the primary key index, wrapping around, instead
    of ORDER BY random(), which has to sort the whole table. Beetles ids are random
    UUIDs, so the window is a random sample.
    """
    if n <= 0:
        return []
    pivot = uuid.uuid4()
    ids = list(qs.filter(id__gte=pivot).order_by("id").values_list("id", flat=True)[:n])
    if len(ids) < n:
        ids += list(qs.filter(id__lt=pivot).order_by("id").values_list("id", flat=True)[: n - len(ids)])
    random.shuffle(ids)
    return ids


def _seen(player, mode, is_check):
    """Subquery of ROIs this player already answered in this mode."""
    return GameAnswer.objects.filter(player=player, mode=mode, is_check=is_check).values("roi_id")


def revealed_ids(player):
    """
    Validated ROIs whose answer this player has been shown in round feedback: every
    scored item, and every validated partner in a pair. They are never scored for this
    player again, so feedback can't be memorised into a better score.
    """
    ids = set(GameAnswer.objects.filter(player=player, is_check=True).values_list("roi_id", flat=True))
    ids |= set(
        GameAnswer.objects.filter(player=player, mode="pair", roi_b__isnull=False).values_list("roi_b_id", flat=True)
    )
    return ids


# ---------------------------------------------------------------------------
# Difficulty
# ---------------------------------------------------------------------------
UNKNOWN_DIFFICULTY = 0.5


def target_difficulty(player):
    """
    The difficulty this player's next items should sit around, from 0 (easy) to 1.

    Starts easy, rises with every finished round, and rises faster for accurate
    players, so the game always gets harder over time. GAME_DIFFICULTY_* settings.
    """
    rounds = GameRound.objects.filter(player=player, finished_at__isnull=False).count()
    summary = player_summary(player)
    skill = max(0.0, (summary["accuracy"] or 0.5) - 0.5) * 2  # 0 at coin-flip, 1 at perfect
    target = (
        game_setting("GAME_DIFFICULTY_START", 0.2)
        + game_setting("GAME_DIFFICULTY_PER_ROUND", 0.02) * rounds
        + game_setting("GAME_DIFFICULTY_SKILL_WEIGHT", 0.3) * skill
    )
    return min(game_setting("GAME_DIFFICULTY_MAX", 0.9), target)


def _difficulties(ids):
    known = {
        d.roi_id: d.value for d in RoiDifficulty.objects.filter(roi_id__in=ids)
    }
    return {i: (known.get(i) if known.get(i) is not None else UNKNOWN_DIFFICULTY) for i in ids}


def _pick_near(candidates, n, target):
    """Pick n of the candidate ids, favouring those whose difficulty is near target."""
    if len(candidates) <= n:
        return list(candidates)
    diff = _difficulties(candidates)
    pool = list(candidates)
    chosen = []
    for _ in range(n):
        weights = [math.exp(-((diff[i] - target) / 0.2) ** 2) + 1e-3 for i in pool]
        pick = random.choices(pool, weights=weights)[0]
        pool.remove(pick)
        chosen.append(pick)
    return chosen


def _sample(qs, n, target, seen=None, exclude=(), allow_seen=True):
    """
    n ids from qs near the target difficulty, preferring ones the player hasn't seen.
    Falls back to seen items when the pool is too small to fill the round, unless
    ``allow_seen`` is off (scored items are never repeated).
    """
    if n <= 0:
        return []
    oversample = game_setting("GAME_CANDIDATE_OVERSAMPLE", 6)
    fresh = qs.exclude(id__in=list(exclude))
    if seen is not None:
        fresh = fresh.exclude(id__in=seen)
    ids = _pick_near(_random_ids(fresh, n * oversample), n, target)
    if len(ids) < n and allow_seen:
        rest = qs.exclude(id__in=list(exclude) + ids)
        ids += _pick_near(_random_ids(rest, (n - len(ids)) * oversample), n - len(ids), target)
    return ids


def update_difficulty(roi_ids):
    """Recompute game_difficulty for these ROIs from all answers on them."""
    roi_ids = list(set(roi_ids))
    rows = defaultdict(lambda: {"ok": 0, "n": 0, "answers": 0, "genus": defaultdict(int)})
    for ans in GameAnswer.objects.filter(roi_id__in=roi_ids, skipped=False, mode="classify"):
        row = rows[ans.roi_id]
        row["answers"] += 1
        for r in RANKS:
            ok = getattr(ans, f"correct_{r}")
            if ok is not None:
                row["n"] += 1
                row["ok"] += int(ok)
        if ans.genus:
            row["genus"][ans.genus.lower()] += 1
    for roi_id in roi_ids:
        row = rows.get(roi_id)
        if not row or not row["answers"]:
            continue
        if row["n"]:
            # Scored item: smoothed error rate across judged ranks.
            value = 1 - (row["ok"] + 1) / (row["n"] + 2)
        elif row["genus"]:
            # Unvalidated item: how much players disagree on the genus.
            value = 1 - max(row["genus"].values()) / sum(row["genus"].values())
        else:
            continue
        RoiDifficulty.objects.update_or_create(
            roi_id=roi_id, defaults={"game_difficulty": round(value, 4), "game_answers": row["answers"]}
        )


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
        player=player, mode=mode, is_check=True, skipped=False, is_retry=False
    ).count()
    if scored < game_setting("GAME_CALIBRATION_CHECKS", 20):
        return game_setting("GAME_CHECK_RATIO_NEW", 0.6)
    return game_setting("GAME_CHECK_RATIO_KNOWN", 0.2)


def _split_round(player, mode, size):
    """How many check and open items go into a round of ``size``."""
    n_checks = max(1, round(size * check_ratio(player, mode)))
    return n_checks, size - n_checks


def _fill(n_checks, n_open, pick_checks, pick_open):
    """Pick checks and open items, topping up from the other pool when one runs short."""
    checks = pick_checks(n_checks)
    opens = pick_open(n_open + (n_checks - len(checks)))
    if len(opens) < n_open:
        checks += pick_checks(n_open - len(opens), exclude=checks)
    return checks, opens


def focus_filter(player):
    """
    Checks aimed at the branches this player has been labelling but hasn't proven
    themselves in yet, so their labels there can become trusted. None if there are none.
    """
    from .game_trust import skills_for

    recent = (
        GameAnswer.objects.filter(player=player, mode="classify", is_check=False, skipped=False)
        .order_by("-answered_at").values_list("tribe", "genus")[:100]
    )
    proven = {(s.rank, s.branch.lower()) for s in skills_for(player) if s.proven}
    genera = {g for _, g in recent if g and ("species", g.lower()) not in proven}
    tribes = {t for t, _ in recent if t and ("genus", t.lower()) not in proven}
    if not genera and not tribes:
        return None
    return Q(taxon__genus__in=genera) | Q(taxon__tribe__in=tribes)


def player_focus(player):
    """
    (rank, value) when the player has chosen to see only one subfamily, tribe or genus and their level still
    allows it (levels can go down), else None.
    """
    from .game_levels import FOCUS_PERK, for_player
    from .models import GamePreference

    pref = GamePreference.objects.filter(player=player).first()
    if pref is None or not pref.focus_rank or not pref.focus_value:
        return None
    if FOCUS_PERK[pref.focus_rank] not in for_player(player)["perks"]:
        return None
    return pref.focus_rank, pref.focus_value


def _focused(qs, focus):
    return qs.filter(**{f"taxon__{focus[0]}__iexact": focus[1]}) if focus else qs


def pools(player):
    """
    The beetles to choose from: (validated, not validated). With a focus, only that part of the tree, as long as it
    has beetles left in both pools; otherwise everything, so the feed never runs dry because of a focus.
    """
    focus = player_focus(player)
    checks, opens = _focused(check_rois(), focus), _focused(open_rois(), focus)
    if focus and not (checks.exists() and opens.exists()):
        return check_rois(), open_rois()
    return checks, opens


def build_classify_items(player, size, fresh_only=False):
    n_checks, n_open = _split_round(player, "classify", size)
    check_pool, open_pool = pools(player)
    target = target_difficulty(player)
    revealed = list(revealed_ids(player))
    seen_open = _seen(player, "classify", False)
    focus = focus_filter(player)

    def pick_checks(n, exclude=()):
        exclude = [c["a"] for c in exclude] + revealed
        ids = []
        n_focus = n // 2 if focus is not None else 0
        if n_focus:
            ids = _sample(check_pool.filter(focus), n_focus, target, exclude=exclude, allow_seen=False)
        ids += _sample(check_pool, n - len(ids), target, exclude=exclude + ids, allow_seen=False)
        return [{"a": str(i), "b": None, "check": True} for i in ids]

    def pick_open(n):
        ids = _sample(open_pool, n, target, seen_open, allow_seen=not fresh_only)
        return [{"a": str(i), "b": None, "check": False} for i in ids]

    checks, opens = _fill(n_checks, n_open, pick_checks, pick_open)
    # Beetles they got wrong before come back now and then, so they can learn them (see retry_ids)
    retries = [{"a": str(i), "b": None, "check": True, "retry": True} for i in retry_ids(player, len(checks))]
    if retries:
        checks = retries + checks[len(retries):] if len(checks) > len(retries) else retries
    return checks + opens


def retry_ids(player, room):
    """
    Validated beetles this player got wrong, ready to be shown again: last seen at least GAME_RETRY_AFTER_DAYS
    ago, not yet answered right since, and shown again at most GAME_RETRY_MAX times. At most
    GAME_RETRY_PER_BATCH of them, never more than ``room``.
    """
    want = min(game_setting("GAME_RETRY_PER_BATCH", 1), room)
    if want <= 0:
        return []
    cutoff = timezone.now() - timedelta(days=game_setting("GAME_RETRY_AFTER_DAYS", 2))
    last, retries = {}, defaultdict(int)
    rows = (
        GameAnswer.objects.filter(player=player, mode="classify", is_check=True, skipped=False, score_hold=False)
        .order_by("answered_at").values("roi_id", "answered_at", "is_retry", *[f"correct_{r}" for r in RANKS])
    )
    for row in rows:   # the latest answer on each beetle wins
        last[row["roi_id"]] = (row["answered_at"], any(row[f"correct_{r}"] is False for r in RANKS))
        retries[row["roi_id"]] += int(row["is_retry"])
    candidates = [
        roi_id for roi_id, (when, wrong) in last.items()
        if wrong and when <= cutoff and retries[roi_id] < game_setting("GAME_RETRY_MAX", 3)
    ]
    if not candidates:
        return []
    usable = list(check_rois().filter(id__in=candidates).values_list("id", flat=True))
    random.shuffle(usable)
    return usable[:want]


def _partner_for(anchor, target, exclude=()):
    """
    A validated ROI to pair with ``anchor``, at a randomly chosen relation
    (same species / genus / tribe / subfamily / different), so answers are spread
    across ranks rather than being mostly "different subfamily".

    For an unvalidated anchor its current (unchecked) label is only used to aim the
    pairing; the answer is what we record.
    """
    pool = check_rois().exclude(id=anchor.id).exclude(id__in=list(exclude))
    if anchor.image_asset_id:
        pool = pool.exclude(image_asset_id=anchor.image_asset_id)
    taxon = anchor.taxon

    def one(qs):
        ids = _sample(qs, 1, target)
        return ids[0] if ids else None

    if taxon is None:
        return one(pool)

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
        if usable:
            partner = one(pool.filter(condition))
            if partner:
                return partner
    return one(pool)


def build_pair_items(player, size, fresh_only=False):
    n_checks, n_open = _split_round(player, "pair", size)
    check_pool, open_pool = pools(player)
    target = target_difficulty(player)
    revealed = list(revealed_ids(player))
    seen_open = _seen(player, "pair", False)

    def make_pairs(anchor_qs, n, seen, is_check, exclude=()):
        items = []
        exclude = [c["a"] for c in exclude]
        if is_check:
            anchor_ids = _sample(anchor_qs, n, target, exclude=exclude + revealed, allow_seen=False)
        else:
            anchor_ids = _sample(anchor_qs, n, target, seen, exclude, allow_seen=not fresh_only)
        for anchor in Beetles.objects.select_related("taxon").filter(id__in=anchor_ids):
            # A scored pair must not lean on a partner whose label the player has been shown.
            partner = _partner_for(anchor, target, revealed if is_check else ())
            if partner is None:
                continue
            items.append({
                "a": str(anchor.id), "b": str(partner), "check": is_check,
                "flip": random.random() < 0.5,
            })
        return items

    checks, opens = _fill(
        n_checks, n_open,
        lambda n, exclude=(): make_pairs(check_pool, n, None, True, exclude),
        lambda n: make_pairs(open_pool, n, seen_open, False),
    )
    return checks + opens


def resumable_round(player, mode):
    """The player's latest unfinished round in this mode, if recent enough to pick up again."""
    since = timezone.now() - timedelta(hours=game_setting("GAME_RESUME_HOURS", 12))
    return (
        GameRound.objects.filter(player=player, mode=mode, finished_at__isnull=True, started_at__gte=since)
        .order_by("-started_at").first()
    )


def spread(items):
    """
    Put the checks at even spacing among the open items, with a random start, instead of a plain shuffle.

    The player sees one continuous feed, so scored items should turn up now and then, not in a clump and not
    in a predictable rhythm: the gaps between them are the same on average but the first one is random.
    """
    checks = [i for i in items if i["check"]]
    opens = [i for i in items if not i["check"]]
    random.shuffle(checks)
    random.shuffle(opens)
    if not checks or not opens:
        return checks + opens
    total = len(items)
    # positions of the checks: evenly spread over the batch, shifted by a random fraction of one gap
    gap = total / len(checks)
    offset = random.random() * gap
    slots = {min(total - 1, int(offset + k * gap)) for k in range(len(checks))}
    k = 0
    while len(slots) < len(checks):          # two checks landed on one slot: take the next free one
        if k not in slots:
            slots.add(k)
        k += 1
    feed, c, o = [], iter(checks), iter(opens)
    for position in range(total):
        feed.append(next(c) if position in slots else next(o))
    return feed


def start_round(player, mode, size=None, fresh_only=False):
    """
    Create a batch of items for the player's continuous feed. Returns None if nothing is playable.

    ``fresh_only`` leaves out unscored items the player has already answered: used to carry on from one batch
    into the next, where running out of new beetles should end the feed rather than repeat what they've seen.
    """
    size = size or game_setting("GAME_ROUND_SIZE", 10)
    builder = build_classify_items if mode == GameRound.Mode.CLASSIFY else build_pair_items
    items = builder(player, size, fresh_only=fresh_only)
    if not items:
        return None
    return GameRound.objects.create(player=player, mode=mode, items=spread(items))


def close_idle_rounds(player, idle_minutes=10):
    """
    Finish the player's feed batches that were left open (they closed the tab, or their phone went to sleep),
    so their answers reach their skills and the difficulty of the images without waiting for them to come back.
    """
    cutoff = timezone.now() - timedelta(minutes=idle_minutes)
    for rnd in GameRound.objects.filter(player=player, finished_at__isnull=True, started_at__lt=cutoff):
        last = rnd.answers.order_by("-answered_at").values_list("answered_at", flat=True).first()
        if last is None or last < cutoff:
            finish_round(rnd)


def finish_round(rnd):
    """Close a round and refresh everything derived from its answers."""
    from .game_trust import recompute_skills

    from .game_scoring import recompute

    if rnd.finished_at is None:
        rnd.finished_at = timezone.now()
        rnd.save(update_fields=["finished_at"])
    recompute_skills(rnd.player)
    update_difficulty(rnd.answers.values_list("roi_id", flat=True))
    recompute([rnd.player_id])
    from .game_trust import auto_apply_expert_labels
    auto_apply_expert_labels(list(rnd.answers.filter(is_check=False).values_list("roi_id", flat=True)))


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
        exprs[f"{r}_ok"] = Count("id", filter=Q(score_hold=False, **{f"correct_{r}": True}))
        exprs[f"{r}_n"] = Count("id", filter=Q(score_hold=False, **{f"correct_{r}__isnull": False}))
    return exprs


def _accuracy(row):
    ok = sum(row[f"{r}_ok"] for r in RANKS)
    n = sum(row[f"{r}_n"] for r in RANKS)
    return (ok / n if n else None), n


def player_summary(player):
    """Items labelled and overall accuracy (share of judged ranks correct on checks)."""
    labelled = GameAnswer.objects.filter(player=player, skipped=False).count()
    row = GameAnswer.objects.filter(player=player, is_check=True, is_retry=False).aggregate(**_rank_counts())
    accuracy, judged = _accuracy(row)
    min_judged = game_setting("GAME_MIN_JUDGED_FOR_ACCURACY", 10)
    return {
        "labelled": labelled,
        "accuracy": accuracy if judged >= min_judged else None,
    }


def week_start(now=None):
    """Midnight on the Monday of the current week (server time): the leaderboard's "this week" begins here."""
    today = (now or timezone.now()).astimezone(timezone.get_current_timezone()).date()
    monday = today - timedelta(days=today.weekday())
    return timezone.make_aware(datetime(monday.year, monday.month, monday.day))


def leaderboard(limit=50, sort="labelled", since=None):
    """The players ranked by beetles labelled (or accuracy). ``since`` limits it to answers from then on."""
    in_period = GameAnswer.objects.all() if since is None else GameAnswer.objects.filter(answered_at__gte=since)
    labelled = {
        row["player"]: row["n"]
        for row in in_period.filter(skipped=False)
        .values("player").annotate(n=Count("id"))
    }
    names = dict(
        get_user_model().objects.filter(id__in=labelled).values_list("id", "username")
    )
    min_judged = game_setting("GAME_MIN_JUDGED_FOR_ACCURACY", 10)
    accuracy = {}
    for row in (
        in_period.filter(is_check=True, is_retry=False)
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


def player_reliability(player_ids=None):
    """
    {player_id: {mode: {rank: {"ok", "n", "accuracy", "weight"}}}} from check answers,
    plus mode "all" combining both modes.

    ``weight`` is the Laplace-smoothed accuracy (ok + 1) / (n + 2): a player with no
    checks at a rank counts as a coin flip, and the weight moves toward their real
    accuracy as they answer more checks.
    """
    out = defaultdict(dict)
    qs = GameAnswer.objects.filter(is_check=True, is_retry=False)
    if player_ids is not None:
        qs = qs.filter(player_id__in=list(player_ids))
    for row in qs.values("player", "mode").annotate(**_rank_counts()):
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
    Species values are "Genus species".
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


def consensus(limit=None, roi_ids=None, voters=None):
    """
    Reliability-weighted votes for each unvalidated ROI that has answers, with the
    trusted-expert verdict from game_trust.

    Returns a list of dicts sorted by number of answers (most first):
    {"roi", "answers", "players", "ranks": {rank: {"value", "support", "votes",
    "trusted", "trusted_votes"}}, "trusted_rank", "taxon"}
    ``support`` is the winning value's share of the total vote weight at that rank.
    ``voters``, when given, limits it to the answers of those players (see game_levels.suggestion_voters).
    """
    from .game_trust import TrustContext

    answers = (
        GameAnswer.objects.filter(is_check=False, skipped=False)
        .select_related("roi", "roi__taxon", "roi_b__taxon")
        .order_by("roi_id", "answered_at")
    )
    if roi_ids is not None:
        answers = answers.filter(roi_id__in=list(roi_ids))
    if voters is not None:
        answers = answers.filter(player_id__in=list(voters))
    answers = list(answers)
    player_ids = {a.player_id for a in answers}
    reliability = player_reliability(player_ids)
    trust = TrustContext(player_ids)

    per_roi = {}
    for ans in answers:
        entry = per_roi.setdefault(ans.roi_id, {
            "roi": ans.roi, "answers": 0, "players": set(), "votes": [],
        })
        entry["answers"] += 1
        entry["players"].add(ans.player_id)
        labels = implied_labels(ans)
        if labels:
            entry["votes"].append((ans.player_id, labels))

    results = []
    for entry in per_roi.values():
        ranks = {}
        for r in RANKS:
            tally, count = defaultdict(float), defaultdict(int)
            display = {}
            for pid, labels in entry["votes"]:
                if r not in labels:
                    continue
                key = labels[r].lower()
                display.setdefault(key, labels[r])
                weights = reliability.get(pid, {}).get("all") or default_weight()
                tally[key] += weights[r]["weight"]
                count[key] += 1
            if not tally:
                ranks[r] = None
                continue
            key = max(tally, key=tally.get)
            ranks[r] = {
                "value": display[key],
                "support": tally[key] / sum(tally.values()),
                "votes": count[key],
            }
        verdict = trust.verdict(entry["votes"], ranks)
        for r in RANKS:
            if ranks[r]:
                ranks[r].update(verdict["ranks"][r])
        results.append({
            "roi": entry["roi"], "answers": entry["answers"],
            "players": len(entry["players"]), "ranks": ranks, "votes": entry["votes"],
            "rank_list": [(r, ranks[r]) for r in RANKS],
            "trusted_rank": verdict["trusted_rank"],
            "taxon": verdict["taxon"],
        })
    results.sort(key=lambda e: (-e["answers"], str(e["roi"].id)))
    return results[:limit] if limit else results
