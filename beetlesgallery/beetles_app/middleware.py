import zoneinfo

from django.utils import timezone

# Set in the browser by static/js/timezone_cookie.js
TIMEZONE_COOKIE = "django_timezone"


class UserTimezoneMiddleware:
    """
    Render datetimes in the viewer's own timezone.

    Reads the IANA timezone name (e.g. "America/New_York") from the cookie set
    by timezone_cookie.js and activates it for this request, so every |date
    filter, form and admin page shows local time. Falls back to
    settings.TIME_ZONE (UTC) when the cookie is missing or not a real timezone.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        tzname = request.COOKIES.get(TIMEZONE_COOKIE)
        try:
            if tzname:
                timezone.activate(zoneinfo.ZoneInfo(tzname))
            else:
                timezone.deactivate()
        except (zoneinfo.ZoneInfoNotFoundError, ValueError):
            timezone.deactivate()
        return self.get_response(request)
