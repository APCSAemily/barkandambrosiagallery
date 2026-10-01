"""
Points for the Beetle ID game.

Every answer is worth some points (AnswerPoints) and a player's score is the running total, never below zero
(PlayerScore). The rules, all adjustable with GAME_POINTS_* settings:

Beetles we know the answer to (validated) are scored against the truth. These earn the most.
  Name That Beetle   each rank you give earns its weight when right and loses 3/4 of it when wrong:
                     subfamily 1, tribe 2, genus 4, species 8. So naming the exact species is worth the most,
                     and going one rank further than you are sure of costs you if you get it wrong. A wrong
                     subfamily (everything wrong) costs much more than a wrong species in the right genus.
  Family Ties        the right answer earns more the finer the line you had to draw: different subfamilies 1,
                     same subfamily 2, same tribe 3, same genus 5, same species 5, plus up to a quarter more when
                     the two photos are alike (same photographer, place, magnification...). A wrong answer loses
                     1 point per step it is off, so "different subfamily" for two beetles of one genus (3 steps
                     off) costs three times as much as "same species" (1 step off).
  Seen again         a beetle shown again so you can learn it (a retry) earns half.

Beetles nobody has validated yet are scored by agreement, never more than GAME_POINTS_CONSENSUS_CAP (60%)
of what the same answer would earn on a validated beetle, and never less than zero:
  * only players whose rating is at or above the median of rated players are counted as judges;
  * a judge counts fully when they are a proven expert for that part of the tree, otherwise by how their rating
    compares with yours, like an Elo expectation: agreeing with stronger players earns almost full credit,
    agreeing with weaker ones very little;
  * a stronger player who disagrees cancels out agreement from weaker ones, so siding with many weak players
    against one strong one earns nothing;
  * the more judges agree, the closer it gets to the cap.

Not sure / skip costs a little (GAME_POINTS_UNSURE, 0.25). Every real answer earns a small participation point
(GAME_POINTS_PARTICIPATION, 0.5), so the score grows with play.

When a beetle is validated later, or its label is corrected, every answer on it is re-scored against the truth,
up or down (recompute, run for a player when they leave the game and for everyone every night).
"""
import math
from collections import defaultdict

from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone

from . import game
from .game import PAIR_DEPTH, RANKS, game_setting
from .models import AnswerPoints, Beetles, GameAnswer, PlayerScore

RANK_POINTS = {"subfamily": 1.0, "tribe": 2.0, "genus": 4.0, "species": 8.0}
# Family Ties: points for the right answer, by how related the two beetles really are (-1 = different subfamilies)
PAIR_POINTS = {-1: 1.0, 0: 2.0, 1: 3.0, 2: 5.0, 3: 5.0}
DEPTH_NAME = {-1: "different subfamilies", 0: "same subfamily", 1: "same tribe", 2: "same genus", 3: "same species"}


def setting(name, default):
    return game_setting(name, default)


# ---------------------------------------------------------------------------
# The truth
# ---------------------------------------------------------------------------
def is_truth(roi):
    """A beetle whose label can be scored against: validated, a complete taxon, not deleted."""
    t = getattr(roi, "taxon", None) if roi is not None else None
    return bool(roi is not None and roi.bbox_is_validated and not roi.is_deleted and t is not None and t.subfamily and t.genus)


def true_depth(taxon_a, taxon_b):
    """How related two taxa are: 3 same species ... -1 different subfamilies. None if it can't be told."""
    shared = game.shared_ranks(taxon_a, taxon_b)
    depth = -1
    for i, r in enumerate(RANKS):
        if shared[r] is True:
            depth = i
        elif shared[r] is False:
            return depth
        else:
            return None   # e.g. one of them is only known to genus: "same genus" or "same species" can't be told
    return depth


SIMILARITY_FIELDS = (
    ("image_asset", "photographer"), ("image_asset", "image_institution"), ("image_asset", "resolution_in_ppmm"),
    (None, "collection_country"), (None, "aspect"),
)


def photo_similarity(roi_a, roi_b):
    """How alike two photos are from what we know about them, 0 to 1 (0 when there is too little to compare)."""
    def value(roi, owner, field):
        holder = getattr(roi, owner, None) if owner else roi
        return getattr(holder, field, None) if holder is not None else None

    same = compared = 0
    for owner, field in SIMILARITY_FIELDS:
        a, b = value(roi_a, owner, field), value(roi_b, owner, field)
        if a in (None, "") or b in (None, ""):
            continue
        compared += 1
        if field == "resolution_in_ppmm":
            same += abs(float(a) - float(b)) <= 0.1 * max(float(a), float(b))
        else:
            same += str(a).strip().lower() == str(b).strip().lower()
    return round(same / compared, 3) if compared >= 2 else 0.0


def classify_truth(answer, taxon):
    """(points, detail) for a Name That Beetle answer on a validated beetle."""
    wrong = setting("GAME_POINTS_WRONG_FACTOR", 0.75)
    given = {r: getattr(answer, r) for r in RANKS}
    results = game.score_classification(given, taxon)
    points, ranks = 0.0, {}
    for r in RANKS:
        ok = results[r]
        if ok is None:
            continue
        p = RANK_POINTS[r] if ok else -RANK_POINTS[r] * wrong
        ranks[r] = {"right": ok, "points": round(p, 2)}
        points += p
    return points, {"ranks": ranks}


def pair_truth(answer, roi_a, roi_b):
    """(points, detail) for a Family Ties answer when both beetles are validated, or None if it can't be told."""
    truth = true_depth(roi_a.taxon, roi_b.taxon)
    given = PAIR_DEPTH.get(answer.pair_answer)
    if truth is None or given is None:
        return None
    if given == truth:
        sim = photo_similarity(roi_a, roi_b)
        points = PAIR_POINTS[truth] * (1 + setting("GAME_POINTS_SIMILARITY_BONUS", 0.25) * sim)
        return points, {"right": True, "truth": DEPTH_NAME[truth], "similarity": sim}
    steps = abs(given - truth)
    return -setting("GAME_POINTS_PAIR_STEP", 1.0) * steps, {"right": False, "truth": DEPTH_NAME[truth], "steps": steps}


# ---------------------------------------------------------------------------
# Agreement, for beetles not validated yet
# ---------------------------------------------------------------------------
def wilson(ok, n, z=1.0):
    if n == 0:
        return 0.0
    p = ok / n
    z2 = z * z
    return (p + z2 / (2 * n) - z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))) / (1 + z2 / n)


RATINGS_CACHE = "game:ratings:v1"


def cached_ratings():
    """ratings(), kept for a few minutes: used while playing, where a slightly old table is fine."""
    from django.core.cache import cache
    table = cache.get(RATINGS_CACHE)
    if table is None:
        table = ratings()
        cache.set(RATINGS_CACHE, table, setting("GAME_RATINGS_CACHE_SECONDS", 300))
    return table


def ratings():
    """
    {player_id: (rating, accuracy, judged)} from the first time each player saw each validated beetle.
    The rating is a cautious estimate of their accuracy (the lower end of a Wilson interval), so a few lucky
    answers don't make anyone an authority.
    """
    tallies = defaultdict(lambda: [0, 0])
    first = set()
    rows = (
        GameAnswer.objects.filter(is_check=True, is_retry=False, skipped=False, score_hold=False)
        .order_by("answered_at")
        .values_list("player_id", "mode", "roi_id", "roi_b_id", *[f"correct_{r}" for r in RANKS])
    )
    for pid, mode, a, b, *oks in rows:
        key = (pid, mode, a, b)
        if key in first:
            continue
        first.add(key)
        for ok in oks:
            if ok is not None:
                tallies[pid][0] += int(ok)
                tallies[pid][1] += 1
    return {pid: (wilson(ok, n), (ok / n if n else None), n) for pid, (ok, n) in tallies.items()}


class Judges:
    """Who counts as a judge for agreement points, and how much each counts for a given player."""

    def __init__(self, rating_table):
        from .game_trust import TrustContext

        self.rating = {pid: r for pid, (r, _, _) in rating_table.items()}
        min_judged = setting("GAME_RATER_MIN_JUDGED", 10)
        rated = sorted(r for pid, (r, _, n) in rating_table.items() if n >= min_judged)
        median = rated[len(rated) // 2] if rated else 1.0
        self.qualified = {
            pid for pid, (r, _, n) in rating_table.items() if n >= min_judged and r >= median and r > 0
        }
        self.trust = TrustContext(self.qualified)
        self.spread = setting("GAME_RATER_SPREAD", 0.1)

    def weight(self, judge_id, player_id, rank, labels):
        if judge_id not in self.qualified or judge_id == player_id:
            return 0.0
        if self.trust.trusted_through(judge_id, rank, labels):
            return 1.0  # a proven expert for this part of the tree
        diff = self.rating.get(judge_id, 0.0) - self.rating.get(player_id, 0.0)
        return 1.0 / (1.0 + math.exp(-diff / self.spread))


def agreement(player_id, claims, votes, judges):
    """
    {rank: c} for each rank the player claims, c from -1 (strong judges disagree) to +1 (many strong judges agree).
    ``votes`` is [(judge_id, {rank: value})] for the beetle, one per judge.
    """
    out = {}
    for rank, value in claims.items():
        agree = disagree = 0.0
        for judge_id, labels in votes:
            if rank not in labels:
                continue
            w = judges.weight(judge_id, player_id, rank, labels)
            if not w:
                continue
            if labels[rank].strip().lower() == value.strip().lower():
                agree += w
            else:
                disagree += w
        out[rank] = (agree - disagree) / (agree + disagree + 1.0)
    return out


def consensus_points(answer, votes, judges):
    """(points, detail) for an answer on a beetle not validated yet: agreement only, never negative."""
    claims = game.implied_labels(answer)
    if not claims:
        return 0.0, {"agreement": {}}
    cap = setting("GAME_POINTS_CONSENSUS_CAP", 0.6)
    c = agreement(answer.player_id, claims, votes, judges)
    if answer.mode == "classify":
        points = sum(cap * RANK_POINTS[r] * max(0.0, c[r]) for r in claims)
    else:
        depth = PAIR_DEPTH[answer.pair_answer]
        deepest = RANKS[depth]
        points = cap * PAIR_POINTS[depth] * max(0.0, c.get(deepest, 0.0))
    return points, {"agreement": {r: round(v, 3) for r, v in c.items()}}


# ---------------------------------------------------------------------------
# One answer
# ---------------------------------------------------------------------------
def score(answer, votes_for, judges):
    """
    (points, basis, detail). ``votes_for(roi_id)`` gives the judges' votes on an unvalidated beetle.
    Every real answer also earns a small participation point (GAME_POINTS_PARTICIPATION), so the score grows the
    more you play; accuracy still decides most of it.
    """
    points, basis, detail = _score(answer, votes_for, judges)
    if basis in (AnswerPoints.Basis.TRUTH, AnswerPoints.Basis.CONSENSUS, AnswerPoints.Basis.NONE) and not answer.skipped \
            and not answer.score_hold:
        bonus = setting("GAME_POINTS_PARTICIPATION", 0.5)
        points += bonus
        detail = dict(detail, participation=bonus)
    return points, basis, detail


def _score(answer, votes_for, judges):
    if answer.score_hold:
        return 0.0, AnswerPoints.Basis.NONE, {"held": True}
    if answer.skipped or (answer.mode == "pair" and answer.pair_answer == "unsure"):
        return -setting("GAME_POINTS_UNSURE", 0.25), AnswerPoints.Basis.UNSURE, {}
    retry = setting("GAME_POINTS_RETRY_FACTOR", 0.5) if answer.is_retry else 1.0
    if answer.mode == "classify":
        if is_truth(answer.roi):
            points, detail = classify_truth(answer, answer.roi.taxon)
            return points * retry, AnswerPoints.Basis.TRUTH, dict(detail, retry=answer.is_retry)
        points, detail = consensus_points(answer, votes_for(answer.roi_id), judges)
        return points, AnswerPoints.Basis.CONSENSUS, detail
    a, b = answer.roi, answer.roi_b
    if b is not None and is_truth(a) and is_truth(b):
        scored = pair_truth(answer, a, b)
        if scored:
            return scored[0] * retry, AnswerPoints.Basis.TRUTH, dict(scored[1], retry=answer.is_retry)
        return 0.0, AnswerPoints.Basis.NONE, {}
    if b is not None and is_truth(b):
        points, detail = consensus_points(answer, votes_for(answer.roi_id), judges)
        return points, AnswerPoints.Basis.CONSENSUS, detail
    return 0.0, AnswerPoints.Basis.NONE, {}


def votes_on(roi_ids):
    """{roi_id: [(player_id, {rank: value})]} from every answer on these beetles, the latest per player."""
    latest = {}
    answers = (
        GameAnswer.objects.filter(roi_id__in=list(roi_ids), skipped=False)
        .select_related("roi_b__taxon").order_by("answered_at")
    )
    for ans in answers:
        labels = game.implied_labels(ans)
        if labels:
            latest[(ans.roi_id, ans.player_id)] = labels
    out = defaultdict(list)
    for (roi_id, pid), labels in latest.items():
        out[roi_id].append((pid, labels))
    return out


# ---------------------------------------------------------------------------
# Recomputing
# ---------------------------------------------------------------------------
def recompute(player_ids=None):
    """
    Re-score every answer of these players (everyone when None) and refresh their totals. Safe to run any time;
    this is how validations, label corrections and other players' later answers reach a score.
    Returns the number of players updated.
    """
    from django.core.cache import cache
    table = ratings()
    cache.set(RATINGS_CACHE, table, setting("GAME_RATINGS_CACHE_SECONDS", 300))
    judges = Judges(table)
    answers = GameAnswer.objects.select_related(
        "roi__taxon", "roi__image_asset", "roi_b__taxon", "roi_b__image_asset",
    ).order_by("player_id", "answered_at", "index")
    if player_ids is not None:
        answers = answers.filter(player_id__in=list(player_ids))
    answers = list(answers)
    open_rois = {a.roi_id for a in answers if not is_truth(a.roi)}
    votes = votes_on(open_rois)

    rows, by_player = [], defaultdict(list)
    for ans in answers:
        points, basis, detail = score(ans, lambda rid: votes.get(rid, []), judges)
        rows.append(AnswerPoints(answer=ans, points=round(points, 3), basis=basis, detail=detail))
        by_player[ans.player_id].append((ans, points))

    players = list(by_player) if player_ids is None else list(player_ids)
    existing = set(get_user_model().objects.filter(id__in=players).values_list("id", flat=True))
    with transaction.atomic():
        AnswerPoints.objects.filter(answer__player_id__in=players).delete()
        AnswerPoints.objects.bulk_create(rows, batch_size=1000)
        for pid in players:
            total = 0.0
            for _, p in by_player.get(pid, []):
                total = max(0.0, total + p)  # a bad start never leaves anyone in debt
            rating, accuracy, judged = table.get(pid, (0.0, None, 0))
            entries = by_player.get(pid, [])
            if pid not in existing:
                continue
            PlayerScore.objects.update_or_create(player_id=pid, defaults=dict(
                score=round(total, 2), rating=round(rating, 4), accuracy=accuracy, judged=judged,
                viewed=len(entries), labelled=sum(1 for a, _ in entries if not a.skipped),
            ))
    return len(players)


def score_new_answer(answer):
    """
    Points for an answer just given, straight away, and the player's total moved with it. The full recompute
    (when they leave, and every night) tidies this up with everything that has changed since.
    """
    roi_ids = {answer.roi_id}
    votes = votes_on(roi_ids) if not is_truth(answer.roi) else {}
    judges = Judges(cached_ratings()) if votes else _NoJudges()
    points, basis, detail = score(answer, lambda rid: votes.get(rid, []), judges)
    AnswerPoints.objects.update_or_create(answer=answer, defaults=dict(points=round(points, 3), basis=basis, detail=detail))
    current, _ = PlayerScore.objects.get_or_create(player_id=answer.player_id)
    current.score = round(max(0.0, current.score + points), 2)
    current.viewed += 1
    current.labelled += 0 if answer.skipped else 1
    current.save(update_fields=["score", "viewed", "labelled", "updated_at"])
    return points, basis


class _NoJudges:
    qualified = set()

    def weight(self, *args):
        return 0.0


def score_for(player):
    """The player's PlayerScore, created (and computed) the first time it is asked for."""
    found = PlayerScore.objects.filter(player=player).first()
    if found is None and GameAnswer.objects.filter(player=player).exists():
        recompute([player.id])
        found = PlayerScore.objects.filter(player=player).first()
    return found or PlayerScore(player=player)
