"""The daily goal adapts to how much a player plays, like a fitness watch's step goal, and never drops below 20."""
from datetime import date, timedelta

from django.test import SimpleTestCase, override_settings

from beetlesgallery.beetles_app.game_rewards import goal_history

D0 = date(2026, 9, 1)


def days(*counts):
    """{day: count} for consecutive days from D0."""
    return {D0 + timedelta(days=i): n for i, n in enumerate(counts)}


def goals(*counts):
    history = goal_history(days(*counts), D0 + timedelta(days=len(counts)))
    return [history[D0 + timedelta(days=i)] for i in range(len(counts) + 1)]


@override_settings(GAME_DAILY_GOAL=20, GAME_DAILY_GOAL_MAX=300)
class GoalTests(SimpleTestCase):
    def test_a_new_player_starts_at_20(self):
        self.assertEqual(goal_history({}, D0), {D0: 20})
        self.assertEqual(goals(5)[0], 20)

    def test_beating_the_goal_raises_it_gradually(self):
        g = goals(60, 60, 60, 60, 60, 60)
        self.assertEqual(g[:3], [20, 25, 30])                  # +25% at most each day, in steps of 5
        self.assertTrue(all(b >= a for a, b in zip(g, g[1:])))
        self.assertLessEqual(g[-1], 60)                        # heads towards what they really do

    def test_falling_short_or_resting_eases_it_but_never_below_20(self):
        busy = goals(*[100] * 15)
        self.assertGreater(busy[-1], 80)
        after = goal_history({**days(*[100] * 15), **{D0 + timedelta(days=15 + i): 10 for i in range(10)}},
                             D0 + timedelta(days=25))
        self.assertLess(after[D0 + timedelta(days=25)], busy[-1])
        rested = goal_history(days(*[100] * 15), D0 + timedelta(days=60))
        self.assertEqual(rested[D0 + timedelta(days=60)], 20)

    def test_it_stays_put_during_the_day(self):
        # today's goal only depends on the days before today
        before = goal_history(days(40, 40), D0 + timedelta(days=2))
        during = goal_history({**days(40, 40), D0 + timedelta(days=2): 500}, D0 + timedelta(days=2))
        self.assertEqual(before[D0 + timedelta(days=2)], during[D0 + timedelta(days=2)])

    def test_never_above_the_cap(self):
        self.assertEqual(goals(*[5000] * 40)[-1], 300)


from beetlesgallery.beetles_app import game_rewards  # noqa: E402
from beetlesgallery.beetles_app.test_game_rewards import RewardsCase  # noqa: E402


class PlayerGoalTests(RewardsCase):
    def test_the_home_and_the_feed_show_the_adapted_goal(self):
        self.assertEqual(game_rewards.progress(self.user)["goal"], 20)
        self.answers(60, days_ago=1)
        self.assertEqual(game_rewards.progress(self.user)["goal"], 25)   # beat it yesterday: up a little today
