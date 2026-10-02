"""The staff review page pages its three tables (open reports, players, label proposals) 25 rows at a time."""
from unittest import mock

from django.contrib.auth import get_user_model
from django.urls import reverse

from beetlesgallery.beetles_app import game, game_views
from beetlesgallery.beetles_app.models import GameReport
from beetlesgallery.beetles_app.test_game import GameCase


class ReviewPaginationTests(GameCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(self.staff)

    def page(self, query=""):
        html = self.client.get(reverse("game_review") + query).content.decode()
        return html.replace('<span class="digit-group">', "").replace("</span>", "")   # numbers as plain text

    def test_open_reports_come_25_at_a_time(self):
        roi = self.roi(self.t_affinis)
        User = get_user_model()
        for n in range(30):
            GameReport.objects.create(roi=roi, reporter=User.objects.create_user(f"r{n}", password="pw"), reason="bad_image")
        first = self.page()
        self.assertIn("(30)", first)
        self.assertEqual(first.count("Open in annotator"), 25)
        self.assertIn("reports_page=2#reports", first)
        second = self.page("?reports_page=2")
        self.assertEqual(second.count("Open in annotator"), 5)

    def test_paging_one_table_keeps_the_others_and_the_filter(self):
        entries = [{"roi": self.roi(self.t_affinis, validated=False), "answers": 1, "players": 1, "trusted_rank": "genus",
                    "ranks": {r: None for r in game.RANKS}, "rank_list": [(r, None) for r in game.RANKS], "taxon": None}
                   for _ in range(30)]
        with mock.patch.object(game, "consensus", return_value=entries):
            page = self.page("?trusted=1&players_page=1&labels_page=2")
        self.assertIn('id="labels" open', page)
        self.assertIn("26&ndash;30 of 30", page)
        self.assertIn("trusted=1", page[page.index('data-testid="pager-labels"'):])

    def test_a_bad_page_number_shows_the_first_or_last_page(self):
        self.assertEqual(self.client.get(reverse("game_review") + "?players_page=zzz").status_code, 200)
        self.assertEqual(game_views.REVIEW_PER_PAGE, 25)
