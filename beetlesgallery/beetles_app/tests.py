from django.http import HttpResponse
from django.test import RequestFactory, SimpleTestCase
from django.utils import timezone

from beetlesgallery.beetles_app.middleware import TIMEZONE_COOKIE, UserTimezoneMiddleware


class UserTimezoneMiddlewareTests(SimpleTestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.seen = None

        def view(request):
            self.seen = timezone.get_current_timezone_name()
            return HttpResponse()

        self.middleware = UserTimezoneMiddleware(view)

    def tearDown(self):
        timezone.deactivate()

    def run_with_cookie(self, value=None):
        request = self.factory.get("/")
        if value is not None:
            request.COOKIES[TIMEZONE_COOKIE] = value
        self.middleware(request)
        return self.seen

    def test_no_cookie_uses_default(self):
        self.assertEqual(self.run_with_cookie(), "UTC")

    def test_valid_cookie_activates_timezone(self):
        self.assertEqual(self.run_with_cookie("America/New_York"), "America/New_York")

    def test_unknown_timezone_falls_back_to_default(self):
        self.assertEqual(self.run_with_cookie("Not/AZone"), "UTC")

    def test_malformed_value_falls_back_to_default(self):
        self.assertEqual(self.run_with_cookie("../../etc/passwd"), "UTC")

    def test_timezone_does_not_leak_into_next_request(self):
        self.run_with_cookie("Asia/Tokyo")
        self.assertEqual(self.run_with_cookie(), "UTC")
