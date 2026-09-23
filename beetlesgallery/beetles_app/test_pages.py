"""
Smoke tests: every page loads for the users allowed to see it, and turns
away the users who are not.

These only check status codes and redirects, not page content. Per-page
behaviour tests build on the same users and helpers.
"""
import uuid

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse

# Plain static storage so templates using {% static %} render without running
# collectstatic first (the manifest storage used in production needs it).
PLAIN_STATIC = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}

# Pages anyone can open
PUBLIC_PAGES = [
    "image_browser",
    "beetles_image_browser",
    "interactions_preview",
    "tool_classify",
    "login",
]

# Pages that need a logged-in user
LOGIN_PAGES = [
    "my_account",
    "data_management",
    "taxonomy_browser",
]

# Pages that need a staff user (staff create contributor accounts via signup)
STAFF_PAGES = [
    "tool_annotate",
    "signup",
]

# Staff-only form handlers: they accept POST and send a plain GET back to My Uploads
STAFF_POST_ONLY = [
    "upload",
    "update_upload",
]

# Pages that need a superuser
SUPERUSER_PAGES = [
    "admin_valid_species",
    "admin_described_names",
]


@override_settings(STORAGES=PLAIN_STATIC)
class PageTestCase(TestCase):
    """Base class with one user of each access level."""

    @classmethod
    def setUpTestData(cls):
        User = get_user_model()
        cls.user = User.objects.create_user("user", password="pw")
        cls.staff = User.objects.create_user("staff", password="pw", is_staff=True)
        cls.superuser = User.objects.create_superuser("super", password="pw")

    def assertRedirectsToLogin(self, response):
        self.assertEqual(response.status_code, 302)
        self.assertIn("login", response["Location"])


class PublicPageTests(PageTestCase):
    def test_public_pages_load_when_logged_out(self):
        for name in PUBLIC_PAGES:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_beetle_detail_requires_login(self):
        url = reverse("beetle_detail", args=[uuid.uuid4()])
        self.assertRedirectsToLogin(self.client.get(url))

    def test_unknown_beetle_returns_404(self):
        self.client.force_login(self.user)
        url = reverse("beetle_detail", args=[uuid.uuid4()])
        self.assertEqual(self.client.get(url).status_code, 404)


class LoginRequiredPageTests(PageTestCase):
    def test_redirect_to_login_when_logged_out(self):
        for name in LOGIN_PAGES:
            with self.subTest(page=name):
                self.assertRedirectsToLogin(self.client.get(reverse(name)))

    def test_load_for_logged_in_user(self):
        self.client.force_login(self.user)
        for name in LOGIN_PAGES:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)


class StaffPageTests(PageTestCase):
    def test_turn_away_non_staff_user(self):
        self.client.force_login(self.user)
        for name in STAFF_PAGES:
            with self.subTest(page=name):
                self.assertRedirectsToLogin(self.client.get(reverse(name)))

    def test_load_for_staff_user(self):
        self.client.force_login(self.staff)
        for name in STAFF_PAGES:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)

    def test_post_only_handlers_turn_away_non_staff_user(self):
        self.client.force_login(self.user)
        for name in STAFF_POST_ONLY:
            with self.subTest(page=name):
                self.assertRedirectsToLogin(self.client.post(reverse(name)))

    def test_post_only_handlers_send_get_back_to_my_uploads(self):
        self.client.force_login(self.staff)
        for name in STAFF_POST_ONLY:
            with self.subTest(page=name):
                response = self.client.get(reverse(name))
                self.assertRedirects(response, reverse("data_management"), fetch_redirect_response=False)


class SuperuserPageTests(PageTestCase):
    def test_turn_away_staff_user(self):
        self.client.force_login(self.staff)
        for name in SUPERUSER_PAGES:
            with self.subTest(page=name):
                self.assertRedirectsToLogin(self.client.get(reverse(name)))

    def test_load_for_superuser(self):
        self.client.force_login(self.superuser)
        for name in SUPERUSER_PAGES + ["admin:index"]:
            with self.subTest(page=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 200)


class TaxonomyEndpointTests(PageTestCase):
    def test_species_endpoints_require_species_id(self):
        for name in ["described_names_for_species", "species_images"]:
            with self.subTest(endpoint=name):
                self.assertEqual(self.client.get(reverse(name)).status_code, 400)


class ApiPermissionTests(PageTestCase):
    def test_beetles_api_rejects_logged_out_and_non_staff(self):
        url = "/api/v1/beetles/"
        self.assertIn(self.client.get(url).status_code, (401, 403))
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(url).status_code, 403)

    def test_beetles_api_loads_for_staff(self):
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get("/api/v1/beetles/").status_code, 200)
