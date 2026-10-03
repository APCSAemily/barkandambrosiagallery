"""
Behaviour tests for the landing page: the hero slides come from real specimens
that are identified and have a photo, the page is full-bleed while other pages
keep their normal column, and the forest artwork it relies on ships with the app.
"""
import re
from django.contrib.staticfiles import finders
from django.core.files.uploadedfile import SimpleUploadedFile
from django.template import Context, Template
from django.test import override_settings
from django.urls import reverse

from beetlesgallery.beetles_app.models import Beetles
from beetlesgallery.beetles_app.testing import PageBehaviourCase, make_beetle, make_image, make_taxon


def photo(name="specimen.jpg", **fields):
    return make_image(image_file=SimpleUploadedFile(name, b"fake image bytes"), **fields)


class LandingFeaturedTests(PageBehaviourCase):
    def landing(self):
        response = self.client.get(reverse("image_browser"))
        self.assertEqual(response.status_code, 200)
        return response

    def test_features_identified_specimens_that_have_a_photo(self):
        taxon = make_taxon(
            "T-IPS", scientific_name="Ips typographus", scientific_name_authority="(Linnaeus, 1758)",
            subfamily="Scolytinae", tribe="Ipini", genus="Ips", species="typographus",
        )
        make_beetle(image=photo(photographer="A. Beetle", image_institution="UF"), taxon=taxon, collection_country="Brazil")

        featured = self.landing().context["featured"]

        self.assertEqual(len(featured), 1)
        self.assertEqual(featured[0]["scientific_name"], "Ips typographus")
        self.assertEqual(featured[0]["authority"], "(Linnaeus, 1758)")
        self.assertEqual((featured[0]["subfamily"], featured[0]["tribe"], featured[0]["genus"]), ("Scolytinae", "Ipini", "Ips"))
        self.assertEqual(featured[0]["country"], "Brazil")
        self.assertEqual((featured[0]["photographer"], featured[0]["institution"]), ("A. Beetle", "UF"))
        self.assertTrue(featured[0]["image_url"])

    def test_skips_unidentified_specimens_and_specimens_without_a_photo(self):
        taxon = make_taxon("T-XYL", scientific_name="Xyleborus affinis", genus="Xyleborus")
        make_beetle(image=photo())                      # has a photo, no taxon
        make_beetle(image=make_image(), taxon=taxon)    # has a taxon, no photo file
        self.assertEqual(self.landing().context["featured"], [])

    def test_skips_deleted_specimens_and_deleted_images(self):
        taxon = make_taxon("T-PLA", scientific_name="Platypus cylindrus", genus="Platypus")
        make_beetle(image=photo("a.jpg"), taxon=taxon).delete()
        gone = photo("b.jpg")
        make_beetle(image=gone, taxon=taxon)
        gone.delete()
        self.assertEqual(self.landing().context["featured"], [])

    def test_one_specimen_per_genus_and_at_most_five(self):
        for n in range(7):
            taxon = make_taxon(f"T-{n}", scientific_name=f"Genus{n} species", genus=f"Genus{n}")
            make_beetle(image=photo(f"{n}.jpg"), taxon=taxon)
        # a second specimen of an already featured genus must not take another slide
        make_beetle(image=photo("extra.jpg"), taxon=Beetles.objects.first().taxon)

        featured = self.landing().context["featured"]

        self.assertEqual(len(featured), 5)
        self.assertEqual(len({f["genus"] for f in featured}), 5)

    def test_panel_falls_back_to_a_welcome_when_nothing_qualifies(self):
        html = self.landing().content.decode()
        self.assertIn("The world's largest collection of bark and ambrosia beetle images", html)
        self.assertNotIn("lp-thumb", html)       # no specimen photo to show
        self.assertNotIn("data-lp-next", html)  # nothing to page through

    def test_renders_the_specimen_slides_and_controls(self):
        for n in range(3):
            taxon = make_taxon(f"T-{n}", scientific_name=f"Genus{n} species", genus=f"Genus{n}")
            make_beetle(image=photo(f"{n}.jpg"), taxon=taxon)

        html = self.landing().content.decode()

        self.assertEqual(len(re.findall(r'class="lp-info(?: [^"]*)?"', html)), 3)  # one panel per slide, not the stack around them
        self.assertIn("data-lp-next", html)
        self.assertIn("/ 03", html)
        self.assertEqual(html.count("<figure class=\"lp-thumb\">"), 3)  # each specimen's photo sits in its panel
        self.assertIn(f'{reverse("beetles_image_browser")}?genus=Genus', html)  # the "Explore <genus>" links

    def test_only_the_first_photo_is_loaded_up_front(self):
        for n in range(3):
            taxon = make_taxon(f"T-{n}", scientific_name=f"Genus{n} species", genus=f"Genus{n}")
            make_beetle(image=photo(f"{n}.jpg"), taxon=taxon)
        html = self.landing().content.decode()
        self.assertEqual(html.count("fetchpriority="), 1)
        self.assertEqual(html.count("data-src="), 2)

    def test_featured_set_is_cached(self):
        taxon = make_taxon("T-A", scientific_name="Genusa species", genus="Genusa")
        make_beetle(image=photo("a.jpg"), taxon=taxon)
        self.assertEqual(len(self.landing().context["featured"]), 1)

        make_beetle(image=photo("b.jpg"), taxon=make_taxon("T-B", scientific_name="Genusb species", genus="Genusb"))

        self.assertEqual(len(self.landing().context["featured"]), 1)  # still the cached set


class LandingLayoutTests(PageBehaviourCase):
    def test_landing_page_is_full_bleed_and_other_pages_keep_their_column(self):
        landing = self.client.get(reverse("image_browser")).content.decode()
        other = self.client.get(reverse("login")).content.decode()

        self.assertIn('<main class="lp-main">', landing)
        self.assertNotIn("max-w-7xl", landing.split("<main", 1)[1].split(">", 1)[0])
        self.assertIn('<main class="max-w-7xl mx-auto px-6 py-8 min-h-[80vh]">', other)

    def test_landing_keeps_its_existing_content(self):
        html = self.client.get(reverse("image_browser")).content.decode()
        for text in (
            "Welcome to the world of Entomology",
            "Welcome to the Bark &amp; Ambrosia Beetle Gallery",
            "Image Attribution",
            "University of Florida Forest Entomology Lab.",
            "Browse Images",
            "AI Identification",
            "Created by:",
            "Contributing Institutions to the Image Database",
            "Visit GitHub Discussions",
        ):
            with self.subTest(text=text):
                self.assertIn(text, html)

    def test_forest_artwork_and_stylesheets_ship_with_the_app(self):
        for path in (
            "css/landing.css", "css/theme.css", "js/landing.js",
            "img/forest/far.svg", "img/forest/mid.svg", "img/forest/near.svg",
        ):
            with self.subTest(path=path):
                self.assertIsNotNone(finders.find(path))

    def test_shared_theme_is_loaded_on_every_page(self):
        for name in ("image_browser", "login"):
            with self.subTest(page=name):
                self.assertIn("css/theme.css", self.client.get(reverse(name)).content.decode())

    def test_every_sidebar_tab_has_its_own_colour(self):
        css = open(finders.find("css/theme.css"), encoding="utf-8").read()
        colours = {}
        for href in ("/beetles/", "/taxonomy/", "/interactions/", "/tools/classify/", "/game/",
                     "/tools/annotate/", "/my-uploads/", "/accounts/me/"):
            marker = f'a[href="{href}"] {{ --tab-rgb: '
            self.assertIn(marker, css, f"no colour for the {href} tab")
            colours[href] = css.split(marker, 1)[1].split(";", 1)[0]
        self.assertEqual(len(set(colours.values())), len(colours), "two tabs share a colour")

class LandingBeetleTests(PageBehaviourCase):
    """The interactive beetle is the hero, with or without featured specimens."""

    def html(self):
        return self.client.get(reverse("image_browser")).content.decode()

    def test_the_beetle_is_always_there_and_described_for_screen_readers(self):
        html = self.html()
        self.assertIn('class="lp-beetle"', html)
        self.assertIn('aria-label="An iridescent green beetle that turns to face your cursor"', html)
        self.assertEqual(html.count("lp-beetle-sprite"), 2)  # two layers, so frames can cross-fade

    def test_the_beetle_does_not_depend_on_any_specimen(self):
        taxon = make_taxon("T-IPS", scientific_name="Ips typographus", genus="Ips")
        make_beetle(image=photo(), taxon=taxon)
        self.assertIn('class="lp-beetle"', self.html())

    def test_sprite_sheet_ships_and_is_preloaded(self):
        self.assertIsNotNone(finders.find("img/hero/beetle-sheet.webp"))
        self.assertIn("img/hero/beetle-sheet.webp", self.html())

    def test_sprite_sheet_is_a_4_by_3_grid_of_square_frames(self):
        from PIL import Image
        with Image.open(finders.find("img/hero/beetle-sheet.webp")) as sheet:
            width, height = sheet.size
            self.assertEqual(sheet.mode, "RGBA")  # transparent, so it sits in the forest
        self.assertEqual((width // 4, height // 3), (width / 4, height / 3))
        self.assertEqual(width // 4, height // 3)

    def test_script_maps_every_compass_direction_to_a_frame_in_the_sheet(self):
        js = open(finders.find("js/landing.js"), encoding="utf-8").read()
        mapping = re.search(r"var FACING = \{([^}]*)\}", js).group(1)
        frames = {k.strip(): int(v) for k, v in (pair.split(":") for pair in mapping.split(","))}
        self.assertEqual(set(frames), {"E", "SE", "S", "SW", "W", "NW", "N", "NE"})
        self.assertEqual(len(set(frames.values())), 8)            # eight different views
        self.assertTrue(all(0 <= n < 12 for n in frames.values()))  # all inside the 12-frame sheet

class LandingHeadlineTests(PageBehaviourCase):
    """The headline and header sit in the room left of the specimen panel, so the panel can never cover them."""

    def css(self):
        return open(finders.find("css/landing.css"), encoding="utf-8").read()

    def test_welcome_line_and_full_name_are_on_the_page(self):
        html = self.client.get(reverse("image_browser")).content.decode()
        self.assertIn('<span class="lp-welcome">Welcome to the world of Entomology</span>', html)
        self.assertLess(html.index("lp-title-main"), html.index("lp-welcome"))  # the welcome line is under the headline
        self.assertIn("Bark &amp; Ambrosia Beetle Gallery", html)

    def test_headline_is_sized_and_boxed_to_the_room_left_of_the_panel(self):
        desktop = self.css().split("@media (min-width: 1024px) {", 1)[1].split("}", 1)[0:1][0]
        css = self.css()
        self.assertIn("--lp-left: calc(100% - min(25rem, 30vw) - 3vw - 1rem);", css)
        self.assertIn(".lp-brand, .lp-title { width: var(--lp-left); }", css)
        # the panel is min(25rem, 30vw) wide plus 3vw from the edge: the same numbers keep the headline clear of it
        self.assertIn("min(25rem, 30vw) - 3vw - 1rem) / 5.15)", css)
        self.assertTrue(desktop)


class StaticFreshTests(PageBehaviourCase):
    """A stylesheet you have just edited must never be served stale from the browser cache while developing."""

    def render(self, path="css/landing.css"):
        return Template("{% load beetle_tags %}{% static_fresh '" + path + "' %}").render(Context())

    def test_adds_the_file_modified_time_while_developing(self):
        with override_settings(DEBUG=True):
            self.assertRegex(self.render(), r"landing\.css\?v=\d+$")

    def test_leaves_the_address_alone_in_production(self):
        with override_settings(DEBUG=False):
            self.assertNotIn("?v=", self.render())

    def test_unknown_files_do_not_break_the_page(self):
        with override_settings(DEBUG=True):
            self.assertNotIn("?v=", self.render("css/does-not-exist.css"))

    def test_landing_page_uses_it_for_its_own_files_only(self):
        with override_settings(DEBUG=True):
            html = self.client.get(reverse("image_browser")).content.decode()
        for name in ("landing.css", "landing.js", "theme.css"):
            with self.subTest(file=name):
                self.assertRegex(html, name.replace(".", r"\.") + r"\?v=\d+")
        self.assertNotRegex(html, r"style\.css\?v=")
