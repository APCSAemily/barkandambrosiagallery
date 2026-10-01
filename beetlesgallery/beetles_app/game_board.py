"""
The leaderboard and player profiles for the Beetle ID game.

The main board ranks by score, accuracy or beetles seen, all time or this week, and can be searched by name.
The expertise board ranks players inside one part of the tree (a subfamily, tribe or genus) by how well they
identify what is in it, which is where people specialise and compete.
"""
from django.contrib.auth import get_user_model
from django.db.models import Count, Q, Sum

from . import game, game_levels, game_rewards
from .models import AnswerPoints, GameAnswer, PlayerScore, PlayerSkill

SORTS = {"score": "Score", "accuracy": "Accuracy", "viewed": "Beetles seen"}
# a branch of the tree -> the skill that measures it (see game_trust.BRANCH_OF)
BRANCH_SKILL = {"subfamily": "tribe", "tribe": "genus", "genus": "species"}


def board(sort="score", period="all", q="", limit=50):
    """Rows: position, player_id, username, level, level_name, score, accuracy, viewed, is_expert."""
    scores = {s.player_id: s for s in PlayerScore.objects.all()}
    names = dict(get_user_model().objects.filter(id__in=scores).values_list("id", "username"))
    experts = set(PlayerSkill.objects.filter(proven=True).values_list("player_id", flat=True))
    if period == "week":
        since = game.week_start()
        week_points = dict(
            AnswerPoints.objects.filter(answer__answered_at__gte=since).values("answer__player")
            .annotate(s=Sum("points")).values_list("answer__player", "s")
        )
        week_viewed = dict(
            GameAnswer.objects.filter(answered_at__gte=since).values("player").annotate(n=Count("id")).values_list("player", "n")
        )
    rows = []
    for pid, s in scores.items():
        if pid not in names or (s.viewed == 0):
            continue
        if q and q.lower() not in names[pid].lower():
            continue
        level = game_levels.describe(s.score, s.rating)
        score, viewed = (max(0.0, week_points.get(pid, 0.0)), week_viewed.get(pid, 0)) if period == "week" else (s.score, s.viewed)
        if period == "week" and not viewed:
            continue
        rows.append({
            "player_id": pid, "username": names[pid], "level": level["level"], "level_name": level["name"],
            "score": round(score), "accuracy": s.accuracy if s.judged >= game.game_setting("GAME_MIN_JUDGED_FOR_ACCURACY", 10) else None,
            "viewed": viewed, "is_expert": pid in experts,
        })
    if sort == "accuracy":
        rows.sort(key=lambda r: (r["accuracy"] is None, -(r["accuracy"] or 0), -r["score"]))
    elif sort == "viewed":
        rows.sort(key=lambda r: (-r["viewed"], -r["score"]))
    else:
        rows.sort(key=lambda r: (-r["score"], r["username"]))
    for i, row in enumerate(rows, start=1):
        row["position"] = i
    return rows[:limit] if limit else rows


def branch_board(rank, value, limit=50):
    """
    Players ranked inside one part of the tree: for a genus, how well they name its species; for a tribe, its
    genera; for a subfamily, its tribes. Proven experts first, then by the cautious estimate of their accuracy.
    """
    skill_rank = BRANCH_SKILL.get(rank)
    if not skill_rank or not value:
        return []
    min_shown = game.game_setting("GAME_REPORT_MIN_JUDGED", 5)
    skills = PlayerSkill.objects.filter(rank=skill_rank, branch__iexact=value, judged__gte=min_shown).select_related("player")
    rows = [{
        "player_id": s.player_id, "username": s.player.username, "correct": s.correct, "judged": s.judged,
        "accuracy": s.correct / s.judged if s.judged else None, "lower_bound": s.lower_bound, "is_expert": s.proven,
    } for s in skills]
    rows.sort(key=lambda r: (not r["is_expert"], -r["lower_bound"], -r["judged"]))
    for i, row in enumerate(rows, start=1):
        row["position"] = i
    return rows[:limit]


def profile(player):
    """What anyone signed in can see about a player."""
    from .game_scoring import score_for

    s = score_for(player)
    proven = list(PlayerSkill.objects.filter(player=player, proven=True).order_by("rank", "branch"))
    return {
        "score": s, "level": game_levels.describe(s.score, s.rating), "badges": game_rewards.badge_cards(player),
        "streak": game_rewards.streak_days(game_rewards.active_days(player)),
        "accuracy": s.accuracy if s.judged >= game.game_setting("GAME_MIN_JUDGED_FOR_ACCURACY", 10) else None,
        "expert_in": [
            {"what": {"tribe": "tribes of", "genus": "genera of", "species": "species of", "subfamily": "subfamilies"}[k.rank],
             "branch": k.branch} for k in proven
        ],
        "discoveries": list(player.species_discoveries.order_by("genus", "species")),
        "modes": dict(GameAnswer.objects.filter(player=player, skipped=False).values_list("mode").annotate(n=Count("id"))),
    }
