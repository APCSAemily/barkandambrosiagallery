"""Guards for the phone layouts (the real check is a browser at 375px wide; these catch the code being undone)."""
from django.conf import settings
from django.test import SimpleTestCase

TEMPLATES = settings.BASE_DIR / "beetlesgallery" / "templates"


def read(*parts):
    return (TEMPLATES.joinpath(*parts)).read_text(encoding="utf-8")


class PhoneLayoutTests(SimpleTestCase):
    def test_every_page_scales_to_the_device(self):
        self.assertIn('name="viewport" content="width=device-width, initial-scale=1.0"', read("base.html"))

    def test_touch_screens_get_finger_sized_controls_and_no_iphone_zoom(self):
        base = read("base.html")
        self.assertIn("@media (pointer: coarse)", base)
        self.assertIn("font-size: 16px", base)          # under 16px, iPhones zoom in when a field is tapped
        self.assertIn("min-height: 2.5rem", base)

    def test_the_annotation_tool_is_three_tabs_on_a_phone_and_takes_a_finger(self):
        page = read("beetles", "tool_annotate.html")
        self.assertIn('id="annot-tabs"', page)
        for tab in ("left", "center", "right"):
            self.assertIn(f"mobileTab('{tab}')", page)
        self.assertIn("addEventListener('pointerdown', onMouseDown)", page)   # mouse, pen and finger
        self.assertNotIn("addEventListener('mousedown', onMouseDown)", page)
        self.assertIn("touch-action: none", page)

    def test_the_taxonomy_browser_stacks_on_a_phone(self):
        page = read("beetles", "taxonomy_browser.html")
        self.assertIn("@media (max-width: 899px)", page)
        self.assertIn("flex-direction: column", page)
        self.assertIn("width: 100%;           /* it sits in a plain wrapper", page)

    def test_the_game_is_one_fixed_screen(self):
        page = read("beetles", "game_play.html")
        self.assertIn("100dvh", page)
        self.assertIn("safe-area-inset-bottom", page)
