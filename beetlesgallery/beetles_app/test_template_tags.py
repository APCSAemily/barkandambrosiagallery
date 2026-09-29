"""
Logic tests for the template tags in templatetags/beetle_tags.py (issue #206, part 2).

remove_filter builds the link behind each "x" on an active-filter chip in the
image browser: the current query string, minus that filter, back on page 1.
"""
import html
import re

from django.http import QueryDict
from django.template import Context, Template
from django.test import RequestFactory, SimpleTestCase

from beetlesgallery.beetles_app.templatetags.beetle_tags import digit_groups, remove_filter

URL = "/beetles/?country=USA&country=Canada&sex=m&page=3&q=ips"


def parse(query_string):
    """Query string -> {key: [values]}, so tests do not depend on key order."""
    return dict(QueryDict(query_string).lists())


class RemoveFilterTests(SimpleTestCase):
    def remove(self, field, value=None, url=URL):
        request = RequestFactory().get(url)
        return parse(remove_filter({"request": request}, field, value))

    def test_removes_only_the_given_value_from_a_multi_value_filter(self):
        self.assertEqual(
            self.remove("country", "USA"),
            {"country": ["Canada"], "sex": ["m"], "q": ["ips"]},
        )

    def test_drops_the_key_when_its_last_value_is_removed(self):
        self.assertEqual(
            self.remove("sex", "m"),
            {"country": ["USA", "Canada"], "q": ["ips"]},
        )

    def test_without_a_value_removes_the_whole_filter(self):
        self.assertEqual(self.remove("country"), {"sex": ["m"], "q": ["ips"]})

    def test_empty_string_value_behaves_like_no_value(self):
        self.assertEqual(self.remove("country", ""), {"sex": ["m"], "q": ["ips"]})

    def test_value_not_in_the_list_leaves_the_filter_alone(self):
        self.assertEqual(
            self.remove("country", "Mexico"),
            {"country": ["USA", "Canada"], "sex": ["m"], "q": ["ips"]},
        )

    def test_unknown_field_changes_nothing_but_pagination(self):
        self.assertEqual(
            self.remove("colour", "red"),
            {"country": ["USA", "Canada"], "sex": ["m"], "q": ["ips"]},
        )

    def test_always_resets_to_page_one(self):
        # Otherwise removing a filter could leave you on a page that no longer exists.
        for field, value in (("country", "USA"), ("country", None), ("colour", None)):
            with self.subTest(field=field, value=value):
                self.assertNotIn("page", self.remove(field, value))

    def test_keeps_the_search_text(self):
        self.assertEqual(self.remove("country")["q"], ["ips"])

    def test_removing_the_only_filter_gives_an_empty_query_string(self):
        request = RequestFactory().get("/beetles/?country=USA&page=2")
        self.assertEqual(remove_filter({"request": request}, "country", "USA"), "")

    def test_does_not_modify_the_request(self):
        request = RequestFactory().get(URL)
        remove_filter({"request": request}, "country", "USA")
        self.assertEqual(request.GET.getlist("country"), ["USA", "Canada"])
        self.assertEqual(request.GET.get("page"), "3")


class RemoveFilterInTemplateTests(SimpleTestCase):
    """The tag as templates actually use it: loaded by name and HTML-escaped."""

    def render(self, source, url=URL):
        request = RequestFactory().get(url)
        return Template("{% load beetle_tags %}" + source).render(Context({"request": request}))

    def test_renders_the_query_string(self):
        out = self.render("{% remove_filter 'country' 'USA' %}")
        self.assertEqual(
            parse(html.unescape(out)),
            {"country": ["Canada"], "sex": ["m"], "q": ["ips"]},
        )

    def test_output_is_html_escaped_for_use_inside_an_href(self):
        out = self.render("{% remove_filter 'country' 'USA' %}")
        self.assertIn("&amp;", out)
        self.assertNotIn("&sex", out)

    def test_accepts_template_variables(self):
        request = RequestFactory().get(URL)
        out = Template(
            "{% load beetle_tags %}{% remove_filter field value %}"
        ).render(Context({"request": request, "field": "sex", "value": "m"}))
        self.assertEqual(
            parse(html.unescape(out)),
            {"country": ["USA", "Canada"], "q": ["ips"]},
        )


class DigitGroupsTests(SimpleTestCase):
    """Landing page counts are shown with the digits in groups of three (#125)."""

    def groups(self, value):
        return re.findall(r">(\d+)<", str(digit_groups(value)))

    def test_groups_of_three_from_the_right(self):
        self.assertEqual(self.groups(70000), ["70", "000"])
        self.assertEqual(self.groups(1234567), ["1", "234", "567"])
        self.assertEqual(self.groups(999), ["999"])
        self.assertEqual(self.groups(0), ["0"])

    def test_only_later_groups_get_extra_space(self):
        html_out = str(digit_groups(12345))
        self.assertEqual(html_out.count("margin-left"), 1)
        self.assertTrue(html_out.startswith('<span class="digit-group">12</span>'))

    def test_non_numbers_pass_through(self):
        self.assertEqual(digit_groups("n/a"), "n/a")
        self.assertIsNone(digit_groups(None))

    def test_in_template(self):
        out = Template("{% load beetle_tags %}{{ n|digit_groups }}").render(Context({"n": "4096"}))
        self.assertIn('<span class="digit-group">4</span>', out)
