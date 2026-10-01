"""
Rewards that keep the Beetle ID game fun: a daily goal, a streak of days and badges (levels are in game_levels.py).

Everything is worked out from the player's answers (nothing new is stored), and nothing here says how
*accurate* they are. A count, a streak or a level can be shown while they play; accuracy-based badges are only
shown once they leave (see recap) so the score stays out of sight during play.
"""
from collections import OrderedDict
from datetime import timedelta

from django.conf import settings
from django.db.models import Count
from django.db.models.functions import TruncDate
from django.utils import timezone

from .models import GameAnswer

def daily_goal():
    return getattr(settings, "GAME_DAILY_GOAL", 20)


def labelled(player, **filters):
    return GameAnswer.objects.filter(player=player, skipped=False, **filters)


def active_days(player):
    """The set of calendar days (server time) on which the player answered something."""
    days = (
        GameAnswer.objects.filter(player=player, skipped=False)
        .annotate(day=TruncDate("answered_at", tzinfo=timezone.get_current_timezone()))
        .values_list("day", flat=True).distinct()
    )
    return set(days)


def streak_days(days, today=None):
    """Consecutive days ending today (or yesterday: the streak is still alive until the day is over)."""
    today = today or timezone.localdate()
    day = today if today in days else today - timedelta(days=1)
    n = 0
    while day in days:
        n += 1
        day -= timedelta(days=1)
    return n


def progress(player):
    """What the home page and the feed's little chip show."""
    from . import game_levels
    total = labelled(player).count()
    today = labelled(player, answered_at__date=timezone.localdate()).count()
    level = game_levels.for_player(player)
    return {
        "total": total, "today": today, "goal": daily_goal(), "goal_met": today >= daily_goal(),
        "streak": streak_days(active_days(player)),
        "level": level["level"], "level_name": level["name"], "proposals": level["proposals"],
        "perks": sorted(level["perks"]),
        "to_next": level["next"]["points_needed"] if level["next"] else None,
        "next": level["next"],
        "level_progress": round(level["progress"], 3),
    }


# ---------------------------------------------------------------------------
# Badges
# ---------------------------------------------------------------------------
BADGES = OrderedDict([
    # key: (name, how to get it, icon, shown_during_play)
    ("first", ("First steps", "Label your first beetle", "fi-rr-flag", True)),
    ("ten", ("Warming up", "Label 10 beetles", "fi-rr-fire-flame-curved", True)),
    ("hundred", ("Centurion", "Label 100 beetles", "fi-rr-medal", True)),
    ("thousand", ("Thousand eyes", "Label 1,000 beetles", "fi-rr-eye", True)),
    ("streak3", ("On a roll", "Play 3 days in a row", "fi-rr-calendar", True)),
    ("streak7", ("Week warrior", "Play 7 days in a row", "fi-rr-calendar-check", True)),
    ("streak30", ("Habitat regular", "Play 30 days in a row", "fi-rr-trophy", True)),
    ("goal", ("Goal getter", "Reach the daily goal", "fi-rr-bullseye-arrow", True)),
    ("both", ("All-rounder", "Play both games", "fi-rr-apps", True)),
    ("species1", ("Species spotter", "Name a species we know the answer to", "fi-rr-search", False)),
    ("species25", ("Sharp eyes", "Name 25 species we know the answer to", "fi-rr-star", False)),
    ("expert", ("Trusted expert", "Prove yourself on a branch of the tree", "fi-rr-shield-check", False)),
])


def earned_badges(player, before=None):
    """The set of badge keys the player has earned, counting only answers before ``before`` if given."""
    answers = GameAnswer.objects.filter(player=player)
    if before is not None:
        answers = answers.filter(answered_at__lt=before)
    done = answers.filter(skipped=False)
    total = done.count()
    days = set(
        done.annotate(day=TruncDate("answered_at", tzinfo=timezone.get_current_timezone())).values_list("day", flat=True).distinct()
    )
    # the streak they had at the end of their last day of play before the cut-off
    best = _best_streak(days)
    per_day = done.annotate(day=TruncDate("answered_at", tzinfo=timezone.get_current_timezone())).values("day").annotate(n=Count("id"))
    goal_days = any(row["n"] >= daily_goal() for row in per_day)
    right_species = answers.filter(is_check=True, is_retry=False, correct_species=True, score_hold=False).count()
    have = set()
    for key, needed in (("first", 1), ("ten", 10), ("hundred", 100), ("thousand", 1000)):
        if total >= needed:
            have.add(key)
    for key, needed in (("streak3", 3), ("streak7", 7), ("streak30", 30)):
        if best >= needed:
            have.add(key)
    if goal_days:
        have.add("goal")
    if done.values("mode").distinct().count() >= 2:
        have.add("both")
    if right_species >= 1:
        have.add("species1")
    if right_species >= 25:
        have.add("species25")
    if before is None:
        from .game_trust import skills_for
        if any(s.proven for s in skills_for(player)):
            have.add("expert")
    return have


def _best_streak(days):
    best = run = 0
    previous = None
    for day in sorted(days):
        run = run + 1 if previous is not None and day - previous == timedelta(days=1) else 1
        best = max(best, run)
        previous = day
    return best


def badge_cards(player):
    """All badges for display: earned or not."""
    have = earned_badges(player)
    return [
        {"key": key, "name": name, "how": how, "icon": icon, "earned": key in have}
        for key, (name, how, icon, _) in BADGES.items()
    ]


# ---------------------------------------------------------------------------
# While playing: little celebrations that say nothing about accuracy
# ---------------------------------------------------------------------------
MILESTONES = (10, 25, 50, 100, 250, 500, 1000)


def play_events(player, before):
    """
    What to celebrate after an answer, given ``before`` (the dict progress() returned before it) .
    Returns a list of {"kind", "title", "text"} for the feed to show as a toast.
    """
    now = progress(player)
    events = []
    if now["level"] > before["level"]:
        from .game_levels import PERKS, PROPOSALS
        gained = [p for p in now["perks"] if p not in before["perks"]]
        unlocked = " Unlocked: " + ", ".join(PERKS[p][0].lower() for p in gained) + "." if gained else ""
        events.append({"kind": "level", "title": f"Level {now['level']}", "text": f"You are now a {now['level_name']}.{unlocked}"})
        if PROPOSALS in gained:
            events.append({"kind": "proposals", "title": "Your labels now count", "text": PERKS[PROPOSALS][1]})
    if now["goal_met"] and not before["goal_met"]:
        events.append({"kind": "goal", "title": "Daily goal reached", "text": f"{now['goal']} beetles today. Keep going!"})
    if now["streak"] > before["streak"] and now["today"] == 1:
        events.append({"kind": "streak", "title": f"{now['streak']}-day streak", "text": "Come back tomorrow to keep it alive."})
    for milestone in MILESTONES:
        if before["total"] < milestone <= now["total"]:
            events.append({"kind": "milestone", "title": f"{milestone:,} beetles", "text": "That's a lot of beetles."})
    return events


def recap(player, since):
    """
    What to show when they leave: this sitting's numbers, the streak, any badge they earned, and (only now)
    how many of the beetles we know the answer to they got right.
    """
    sitting = GameAnswer.objects.filter(player=player, answered_at__gte=since)
    done = sitting.filter(skipped=False)
    scored = sitting.filter(is_check=True, skipped=False, score_hold=False).exclude(mode="pair", pair_answer="unsure")
    right = scored.exclude(correct_subfamily=False).exclude(correct_tribe=False).exclude(correct_genus=False).exclude(correct_species=False).count()
    new = [
        {"key": key, "name": BADGES[key][0], "how": BADGES[key][1], "icon": BADGES[key][2]}
        for key in BADGES if key in earned_badges(player) and key not in earned_badges(player, before=since)
    ]
    state = progress(player)
    from django.db.models import Sum
    from .models import AnswerPoints
    points = AnswerPoints.objects.filter(answer__in=sitting).aggregate(s=Sum("points"))["s"] or 0.0
    return {
        "points": round(points, 1),
        "labelled": done.count(), "skipped": sitting.filter(skipped=True).count(),
        "scored": scored.count(), "right": right,
        "streak": state["streak"], "level": state["level"], "level_name": state["level_name"],
        "today": state["today"], "goal": state["goal"], "badges": new,
    }
