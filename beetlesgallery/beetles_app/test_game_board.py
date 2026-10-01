"""The leaderboard (score, accuracy, beetles seen; search; this week; per branch) and player profiles."""
from django.urls import reverse

from beetlesgallery.beetles_app import game_board
from beetlesgallery.beetles_app.models import PlayerScore, PlayerSkill
from beetlesgallery.beetles_app.test_game_scoring import ScoringCase


class BoardTests(ScoringCase):
    def setUp(self):
        super().setUp()
        self.ann = self.player("ann")
        self.bob = self.player("bob")
        self.cy = self.player("cy")
        PlayerScore.objects.create(player=self.ann, score=500, rating=0.6, accuracy=0.6, judged=40, viewed=50)
        PlayerScore.objects.create(player=self.bob, score=200, rating=0.8, accuracy=0.9, judged=40, viewed=90)
        PlayerScore.objects.create(player=self.cy, score=900, rating=0.3, accuracy=0.5, judged=3, viewed=20)

    def names(self, rows):
        return [r["username"] for r in rows]

    def test_it_sorts_by_score_accuracy_or_beetles_seen(self):
        self.assertEqual(self.names(game_board.board()), ["cy", "ann", "bob"])
        # cy has too few judged answers for an accuracy, so goes last on that board
        self.assertEqual(self.names(game_board.board(sort="accuracy")), ["bob", "ann", "cy"])
        self.assertEqual(self.names(game_board.board(sort="viewed")), ["bob", "ann", "cy"])
        self.assertIsNone(game_board.board()[0]["accuracy"])

    def test_it_can_be_searched(self):
        self.assertEqual(self.names(game_board.board(q="BO")), ["bob"])

    def test_players_who_have_not_played_are_left_out(self):
        PlayerScore.objects.create(player=self.player("idle"), score=0, viewed=0)
        self.assertNotIn("idle", self.names(game_board.board()))

    def test_the_branch_board_ranks_experts_in_one_part_of_the_tree(self):
        PlayerSkill.objects.create(player=self.ann, rank="species", branch="Xyleborus", correct=18, judged=20, lower_bound=0.7)
        PlayerSkill.objects.create(player=self.bob, rank="species", branch="Xyleborus", correct=30, judged=30, lower_bound=0.92, proven=True)
        PlayerSkill.objects.create(player=self.cy, rank="species", branch="Xyleborus", correct=2, judged=2, lower_bound=0.2)
        rows = game_board.branch_board("genus", "xyleborus")
        self.assertEqual(self.names(rows), ["bob", "ann"])   # cy has too few answers there
        self.assertTrue(rows[0]["is_expert"])
        self.assertEqual(game_board.branch_board("species", "x"), [])

    def test_the_leaderboard_page_links_names_to_profiles(self):
        self.client.force_login(self.ann)
        page = self.client.get(reverse("game_leaderboard"), {"sort": "viewed", "q": "a"}).content.decode()
        self.assertIn(reverse("game_profile", args=[self.ann.id]), page)
        self.assertNotIn(">bob<", page)
        home = self.client.get(reverse("game_home")).content.decode()
        self.assertIn(reverse("game_profile", args=[self.bob.id]), home)
        self.assertIn(reverse("game_leaderboard"), home)

    def test_the_branch_filter_on_the_page(self):
        PlayerSkill.objects.create(player=self.bob, rank="species", branch="Xyleborus", correct=30, judged=30, lower_bound=0.92, proven=True)
        self.client.force_login(self.ann)
        ctx = self.client.get(reverse("game_leaderboard"), {"rank": "genus", "branch": "Xyleborus"}).context
        self.assertEqual(self.names(ctx["branch_rows"]), ["bob"])

    def test_a_profile_shows_level_score_and_expertise(self):
        PlayerSkill.objects.create(player=self.bob, rank="species", branch="Xyleborus", correct=30, judged=30, lower_bound=0.92, proven=True)
        self.client.force_login(self.ann)
        response = self.client.get(reverse("game_profile", args=[self.bob.id]))
        page = response.content.decode()
        self.assertIn("bob", page)
        self.assertIn("Xyleborus", page)
        self.assertEqual(response.context["p"]["level"]["name"], "Pupa")
        self.assertEqual(self.client.get(reverse("game_profile", args=[99999])).status_code, 404)

    def test_pages_need_a_login(self):
        for url in (reverse("game_leaderboard"), reverse("game_profile", args=[self.ann.id])):
            self.assertEqual(self.client.get(url).status_code, 302)
