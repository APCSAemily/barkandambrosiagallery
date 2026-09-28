// Tells the server the viewer's timezone so times are shown in local time
// (read by beetles_app.middleware.UserTimezoneMiddleware).
(function () {
  try {
    var tz = Intl.DateTimeFormat().resolvedOptions().timeZone;
    if (tz && document.cookie.indexOf("django_timezone=" + tz) === -1) {
      document.cookie = "django_timezone=" + tz + "; path=/; max-age=31536000; SameSite=Lax";
    }
  } catch (e) {
    // Older browsers without Intl timezone support keep the UTC default
  }
})();
