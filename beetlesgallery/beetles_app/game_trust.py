"""
Player expertise and trusted game labels.

Expertise is measured per rank *within a branch* of the taxonomy, from a player's scored
"Name the beetle" answers (validated ROIs they didn't know were being scored):

    species   within a genus        e.g. species ID in Xyleborus
    genus     within a tribe        e.g. genus ID in Xyleborini
    tribe     within a subfamily
    subfamily overall

A player is *proven* at a rank in a branch once they have answered at least
GAME_TRUST_MIN_JUDGED distinct validated ROIs there and the Wilson lower bound of their
accuracy (GAME_TRUST_Z, 1.96 = 95% confidence) is at least GAME_TRUST_MIN_LOWER_BOUND.

A game label on an unvalidated ROI is *trusted* at a rank when a player proven for it
supports it and no player proven for it disagrees. Trust is checked for the branch the
label itself falls in, and every rank above must be trusted too. When the branch can't be
tested (too few validated ROIs to ever prove competence there), proof in at least
GAME_TRUST_SIBLINGS related branches counts instead: species in an untested genus needs
species-level proof in other genera of the same tribe, genus in an untested tribe needs
genus-level proof in other tribes of the same subfamily, and so on.

Trusted labels are proposals only: staff accept them on the annotation page.
"""
import math
from collections import defaultdict
from datetime import timedelta

from django.core.cache import cache
from django.db.models import Count, Q
from django.db.models.functions import TruncMonth
from django.utils import timezone

from .game import COMPLETE_TAXON, RANKS, check_rois, game_setting
from .models import GameAnswer, PlayerSkill, Taxon

# The rank whose value names the branch a skill is measured in.
BRANCH_OF = {"subfamily": None, "tribe": "subfamily", "genus": "tribe", "species": "genus"}

INDEX_CACHE_KEY = "game:trust_index:v1"
INDEX_CACHE_SECONDS = 600


def wilson_lower_bound(ok, n, z=None):
    """Lower end of the Wilson score interval for ok successes out of n."""
    if n == 0:
        return 0.0
    z = game_setting("GAME_TRUST_Z", 1.96) if z is None else z
    p = ok / n
    z2 = z * z
    centre = p + z2 / (2 * n)
    spread = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n))
    return (centre - spread) / (1 + z2 / n)


def answers_needed():
    """Fewest answers with which a perfect record proves competence at current settings."""
    n = game_setting("GAME_TRUST_MIN_JUDGED", 15)
    while n < 10000 and wilson_lower_bound(n, n) < game_setting("GAME_TRUST_MIN_LOWER_BOUND", 0.9):
        n += 1
    return n


def is_proven(ok, n):
    return (
        n >= game_setting("GAME_TRUST_MIN_JUDGED", 15)
        and wilson_lower_bound(ok, n) >= game_setting("GAME_TRUST_MIN_LOWER_BOUND", 0.9)
    )


def branch_for(rank, labels):
    parent = BRANCH_OF[rank]
    return (labels.get(parent) or "") if parent else ""


# ---------------------------------------------------------------------------
# Skills
# ---------------------------------------------------------------------------
def skill_counts(player):
    """
    {(rank, branch_lower): [correct, judged, branch_display]} from scored classify answers.
    Each validated ROI counts once per rank (the first answer), so replayed items can't
    pad a record.
    """
    stats = {}
    seen = set()
    answers = (
        GameAnswer.objects.filter(player=player, mode="classify", is_check=True, skipped=False)
        .order_by("answered_at")
        .values("roi_id", "ref_subfamily", "ref_tribe", "ref_genus",
                *[f"correct_{r}" for r in RANKS])
    )
    for a in answers:
        labels = {"subfamily": a["ref_subfamily"], "tribe": a["ref_tribe"], "genus": a["ref_genus"]}
        for r in RANKS:
            ok = a[f"correct_{r}"]
            if ok is None or (r, a["roi_id"]) in seen:
                continue
            seen.add((r, a["roi_id"]))
            branch = branch_for(r, labels)
            if BRANCH_OF[r] and not branch:
                continue
            row = stats.setdefault((r, branch.lower()), [0, 0, branch])
            row[0] += int(ok)
            row[1] += 1
    return stats


def recompute_skills(player):
    """Refresh the player's PlayerSkill rows from their answers."""
    now = timezone.now()
    existing = {(s.rank, s.branch.lower()): s for s in PlayerSkill.objects.filter(player=player)}
    create, update = [], []
    for key, (ok, n, display) in skill_counts(player).items():
        skill = existing.get(key)
        if skill is None:
            skill = PlayerSkill(player=player, rank=key[0], branch=display)
            create.append(skill)
        else:
            update.append(skill)
        proven = is_proven(ok, n)
        if proven and not skill.proven:
            skill.proven_at = now
        skill.correct, skill.judged, skill.proven = ok, n, proven
        skill.lower_bound = round(wilson_lower_bound(ok, n), 4)
        skill.updated_at = now
    PlayerSkill.objects.bulk_create(create)
    PlayerSkill.objects.bulk_update(update, ["correct", "judged", "proven", "proven_at", "lower_bound", "updated_at"])


def skills_for(player):
    return list(PlayerSkill.objects.filter(player=player).order_by("rank", "branch"))


# ---------------------------------------------------------------------------
# Taxonomy index: parents of branches, and which branches can be tested
# ---------------------------------------------------------------------------
def trust_index():
    """
    Cached lookups:
      parent[rank][branch]  the branch one level up (genus -> tribe, tribe -> subfamily)
      validated[rank]       {branch: validated ROIs available to test that rank there}
    """
    index = cache.get(INDEX_CACHE_KEY)
    if index is not None:
        return index
    parent = {"genus": {}, "tribe": {}}
    for subfamily, tribe, genus in Taxon.objects.filter(COMPLETE_TAXON).values_list(
        "subfamily", "tribe", "genus"
    ).distinct():
        if genus and tribe:
            parent["genus"].setdefault(genus.lower(), tribe.lower())
        if tribe:
            parent["tribe"].setdefault(tribe.lower(), subfamily.lower())

    validated = {}
    for rank, field in BRANCH_OF.items():
        if field is None:
            validated[rank] = {"": check_rois().count()}
            continue
        validated[rank] = {
            (row[f"taxon__{field}"] or "").lower(): row["n"]
            for row in check_rois().values(f"taxon__{field}").annotate(n=Count("id"))
        }
    index = {"parent": parent, "validated": validated}
    cache.set(INDEX_CACHE_KEY, index, INDEX_CACHE_SECONDS)
    return index


def branch_parent(rank, branch, index):
    """
    The branch above ``branch`` for skills at ``rank``: the tribe of a genus (species
    skills), the subfamily of a tribe (genus skills), or "" for subfamilies (tribe skills).
    None when unknown or there is nothing above.
    """
    field = BRANCH_OF[rank]
    if field is None:
        return None
    if field == "subfamily":
        return ""
    return index["parent"][field].get(branch)


def is_testable(rank, branch, index):
    return index["validated"][rank].get(branch, 0) >= answers_needed()


def complete_labels(labels, index):
    """Fill in missing higher ranks from the taxonomy (e.g. the tribe of a genus)."""
    labels = dict(labels)
    if labels.get("genus") and not labels.get("tribe"):
        labels["tribe"] = index["parent"]["genus"].get(labels["genus"].lower(), "")
    if labels.get("tribe") and not labels.get("subfamily"):
        labels["subfamily"] = index["parent"]["tribe"].get(labels["tribe"].lower(), "")
    return labels


class TrustContext:
    """Answers "is this player trusted for this label?" for a set of players."""

    def __init__(self, player_ids):
        self.index = trust_index()
        self.proven = defaultdict(set)
        for pid, rank, branch in PlayerSkill.objects.filter(
            player_id__in=list(player_ids), proven=True
        ).values_list("player_id", "rank", "branch"):
            self.proven[pid].add((rank, branch.lower()))

    def how_trusted(self, player_id, rank, labels):
        """
        "direct" if proven in this label's branch, "siblings" if the branch is untestable
        and the player is proven in enough related branches, else None.
        """
        branch = branch_for(rank, labels).lower()
        if BRANCH_OF[rank] and not branch:
            return None
        proven = self.proven.get(player_id, set())
        if (rank, branch) in proven:
            return "direct"
        if is_testable(rank, branch, self.index):
            return None
        parent = branch_parent(rank, branch, self.index)
        if parent is None:
            return None
        siblings = [
            b for (r, b) in proven
            if r == rank and b != branch and branch_parent(rank, b, self.index) == parent
        ]
        return "siblings" if len(siblings) >= game_setting("GAME_TRUST_SIBLINGS", 2) else None

    def trusted_through(self, player_id, rank, labels):
        """Trusted at ``rank`` and at every rank above it."""
        labels = complete_labels(labels, self.index)
        for r in RANKS[: RANKS.index(rank) + 1]:
            if not labels.get(r) or not self.how_trusted(player_id, r, labels):
                return False
        return True

    def verdict(self, votes, ranks):
        """
        Which consensus ranks are backed by trusted players.

        votes: [(player_id, {rank: value})], ranks: {rank: {"value", ...} or None}.
        A rank is trusted when at least GAME_TRUST_MIN_VOTES trusted players give the
        winning value (with the winning values above it), no trusted player gives a
        different value, and the rank above is trusted.
        """
        min_votes = game_setting("GAME_TRUST_MIN_VOTES", 1)
        out = {r: {"trusted": False, "trusted_votes": 0} for r in RANKS}
        trusted_rank = ""
        for i, r in enumerate(RANKS):
            if not ranks.get(r):
                break
            winners = {rr: ranks[rr]["value"].lower() for rr in RANKS[: i + 1]}
            support, dissent = 0, False
            for pid, labels in votes:
                if r not in labels or not self.trusted_through(pid, r, labels):
                    continue
                if all((labels.get(rr) or "").lower() == winners[rr] for rr in winners):
                    support += 1
                else:
                    dissent = True
            out[r]["trusted_votes"] = support
            if support < min_votes or dissent:
                break
            out[r]["trusted"] = True
            trusted_rank = r
        return {"ranks": out, "trusted_rank": trusted_rank, "taxon": species_taxon(ranks.get("species"))}


def species_taxon(species_vote):
    """The Taxon for a "Genus species" consensus value, preferring the nominal (no subspecies) row."""
    if not species_vote:
        return None
    genus, _, species = species_vote["value"].partition(" ")
    if not species:
        return None
    return (
        Taxon.objects.filter(COMPLETE_TAXON, genus__iexact=genus, species__iexact=species)
        .order_by("subspecies", "valid_species_id").first()
    )


# ---------------------------------------------------------------------------
# Player report
# ---------------------------------------------------------------------------
def player_report(player):
    """Everything the performance page shows about one player."""
    from . import game

    needed = answers_needed()
    reliability = game.player_reliability([player.id]).get(player.id) or {
        m: game.default_weight() for m in ("classify", "pair", "all")
    }
    skills = skills_for(player)
    proven = [s for s in skills if s.proven]

    # Skill branches come from the reference labels of scored items, so listing every
    # one would tell the player what a beetle they got wrong really was (and which
    # items were scored). Only show progress in groups the player has named themselves,
    # once there's enough of it that a single item can't be singled out.
    claimed = {"subfamily": {""}, "tribe": set(), "genus": set(), "species": set()}
    for subfamily, tribe, genus in GameAnswer.objects.filter(
        player=player, mode="classify", skipped=False
    ).values_list("subfamily", "tribe", "genus").distinct():
        claimed["tribe"].add(subfamily.lower())
        claimed["genus"].add(tribe.lower())
        claimed["species"].add(genus.lower())
    min_shown = game_setting("GAME_REPORT_MIN_JUDGED", 5)
    progressing = sorted(
        (s for s in skills if not s.proven and s.judged >= min_shown and s.branch.lower() in claimed[s.rank]),
        key=lambda s: (-min(s.judged / needed, 1) * s.lower_bound, s.rank),
    )
    for s in skills:
        s.progress = min(1.0, s.judged / needed)
        s.accuracy = s.correct / s.judged if s.judged else None

    since = timezone.now() - timedelta(days=183)
    monthly = []
    rows = (
        GameAnswer.objects.filter(player=player, answered_at__gte=since)
        .annotate(month=TruncMonth("answered_at"))
        .values("month")
        .annotate(
            labelled=Count("id", filter=Q(skipped=False)),
            **{k: v for k, v in game._rank_counts().items()},
        )
        .order_by("month")
    )
    for row in rows:
        accuracy, judged = game._accuracy(row)
        monthly.append({"month": row["month"], "labelled": row["labelled"], "accuracy": accuracy, "judged": judged})

    return {
        "summary": game.player_summary(player),
        "rounds": player.game_rounds.filter(finished_at__isnull=False).count(),
        "challenge": game.target_difficulty(player),
        "by_rank": [
            {"rank": r, "classify": reliability["classify"][r], "pair": reliability["pair"][r]}
            for r in RANKS
        ],
        "proven": proven,
        "progressing": progressing[:12],
        "monthly": monthly,
        "needed": needed,
        "min_lower_bound": game_setting("GAME_TRUST_MIN_LOWER_BOUND", 0.9),
        "siblings": game_setting("GAME_TRUST_SIBLINGS", 2),
    }
