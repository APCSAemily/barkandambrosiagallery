"""The home page points visitors at three things: browse images, identify a beetle, play the game."""
import re

from django.urls import reverse

from beetlesgallery.beetles_app.test_pages import PageTestCase

CARD = re.compile(r'<a href="([^"]+)" class="lp-card"')

EXPECTED = [
    reverse("beetles_image_browser"),
    reverse("tool_classify"),
    reverse("game_home"),
]


class HomePageCardTests(PageTestCase):
    def cards(self):
        return CARD.findall(self.client.get(reverse("image_browser")).content.decode())

    def test_only_the_three_main_cards_for_visitors(self):
        self.assertEqual(self.cards(), EXPECTED)

    def test_staff_get_the_same_three_cards(self):
        # Annotation and data management are still in the sidebar; they are not home page cards.
        self.client.force_login(self.staff)
        self.assertEqual(self.cards(), EXPECTED)

    def test_other_pages_stay_reachable_from_the_sidebar(self):
        self.client.force_login(self.staff)
        page = self.client.get(reverse("image_browser")).content.decode()
        for name in ("taxonomy_browser", "interactions_preview", "tool_annotate", "data_management"):
            with self.subTest(page=name):
                self.assertIn(reverse(name), page)
