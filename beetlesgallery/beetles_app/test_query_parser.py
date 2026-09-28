"""
Logic tests for the image-browser search box: the query parser in utils.py
(issue #206, part 2).

Three layers, cheapest first:
  * pure helpers (tokenizer, operator precedence, value parsers) - no database
  * build_query_q run against a few real rows - proves the Q objects it builds
    pick the right specimens, not just that they have the right shape
  * guards that FIELD_MAP / FREE_TEXT_FIELDS still point at real model fields,
    so renaming a column cannot silently break search
"""
import uuid
from datetime import date
from decimal import Decimal

from django.test import SimpleTestCase, TestCase

from beetlesgallery.beetles_app import utils
from beetlesgallery.beetles_app.models import Beetles, Taxon
from beetlesgallery.beetles_app.testing import make_beetle, make_image, make_taxon


class TokenizerTests(SimpleTestCase):
    """_tokenize_query: raw search text -> field/value, operator and free-text tokens."""

    def tokens(self, text):
        return utils._tokenize_query(text)

    def test_empty_input_gives_no_tokens(self):
        self.assertEqual(self.tokens(""), [])
        self.assertEqual(self.tokens(None), [])

    def test_field_with_colon(self):
        self.assertEqual(self.tokens("genus:Ips"), [{"field": "genus", "value": "Ips"}])

    def test_field_with_equals(self):
        self.assertEqual(self.tokens("genus=Ips"), [{"field": "genus", "value": "Ips"}])

    def test_value_may_be_the_next_word(self):
        self.assertEqual(self.tokens("genus: Ips"), [{"field": "genus", "value": "Ips"}])

    def test_multi_word_field_name(self):
        self.assertEqual(
            self.tokens("type status:holotype"),
            [{"field": "type status", "value": "holotype"}],
        )

    def test_quotes_keep_spaces_in_a_value(self):
        self.assertEqual(
            self.tokens('photographer:"John Smith"'),
            [{"field": "photographer", "value": "John Smith"}],
        )

    def test_plain_words_become_one_free_text_token(self):
        self.assertEqual(self.tokens("ips typographus"), [{"free_text": "ips typographus"}])

    def test_operators_are_recognised_in_any_case_and_uppercased(self):
        self.assertEqual(
            self.tokens("a:1 and b:2 Or c:3 not d:4"),
            [
                {"field": "a", "value": "1"}, {"op": "AND"},
                {"field": "b", "value": "2"}, {"op": "OR"},
                {"field": "c", "value": "3"}, {"op": "NOT"},
                {"field": "d", "value": "4"},
            ],
        )

    def test_field_with_no_value_at_the_end(self):
        self.assertEqual(self.tokens("genus:"), [{"field": "genus", "value": ""}])

    def test_operator_after_a_colon_is_not_swallowed_as_the_value(self):
        self.assertEqual(
            self.tokens("genus: AND country:USA"),
            [
                {"field": "genus", "value": ""},
                {"op": "AND"},
                {"field": "country", "value": "USA"},
            ],
        )

    def test_unbalanced_quote_does_not_raise(self):
        # shlex fails on the stray quote; the tokenizer falls back to splitting on spaces.
        tokens = self.tokens('genus:"Ips typ')
        self.assertEqual(tokens[0]["field"], "genus")


class OperatorPrecedenceTests(SimpleTestCase):
    """_to_rpn: NOT binds tighter than AND, which binds tighter than OR."""

    def rpn(self, *items):
        parts = [
            {"op": i} if i in utils.OPERATORS else {"field": i, "value": "x"}
            for i in items
        ]
        return [n["op"] if "op" in n else n["field"] for n in utils._to_rpn(parts)]

    def test_no_operators_keeps_order(self):
        self.assertEqual(self.rpn("a", "b"), ["a", "b"])
        self.assertEqual(self.rpn(), [])

    def test_and_binds_tighter_than_or(self):
        # a OR (b AND c)
        self.assertEqual(self.rpn("a", "OR", "b", "AND", "c"), ["a", "b", "c", "AND", "OR"])

    def test_and_before_or_reads_left_to_right(self):
        # (a AND b) OR c
        self.assertEqual(self.rpn("a", "AND", "b", "OR", "c"), ["a", "b", "AND", "c", "OR"])

    def test_same_operator_is_left_associative(self):
        self.assertEqual(self.rpn("a", "AND", "b", "AND", "c"), ["a", "b", "AND", "c", "AND"])
        self.assertEqual(self.rpn("a", "OR", "b", "OR", "c"), ["a", "b", "OR", "c", "OR"])

    def test_not_applies_to_the_next_term_only(self):
        # (NOT a) AND b
        self.assertEqual(self.rpn("NOT", "a", "AND", "b"), ["a", "NOT", "b", "AND"])


class ValueParserTests(SimpleTestCase):
    """The small parsers that turn a typed value into something queryable."""

    def test_numeric_operators(self):
        cases = {
            ">=10": ("gte", 10.0),
            "<= 3": ("lte", 3.0),
            ">5": ("gt", 5.0),
            "< 5.5": ("lt", 5.5),
            "=12": ("exact", 12.0),
            "12": ("exact", 12.0),
            " -3.5 ": ("exact", -3.5),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(utils._parse_numeric(text), expected)

    def test_numeric_rejects_garbage(self):
        for text in ("abc", "", None, ">=", "1.2.3", "10mm", "==5"):
            with self.subTest(text=text):
                self.assertEqual(utils._parse_numeric(text), (None, None))

    def test_full_date_is_an_exact_day(self):
        self.assertEqual(utils._parse_date_prefix("2020-05-17"), (date(2020, 5, 17), None))

    def test_year_month_covers_that_whole_month(self):
        self.assertEqual(utils._parse_date_prefix("2020-05"), (date(2020, 5, 1), date(2020, 6, 1)))

    def test_december_rolls_over_into_the_next_year(self):
        self.assertEqual(utils._parse_date_prefix("2020-12"), (date(2020, 12, 1), date(2021, 1, 1)))

    def test_year_covers_the_whole_year(self):
        self.assertEqual(utils._parse_date_prefix("2020"), (date(2020, 1, 1), date(2021, 1, 1)))

    def test_date_rejects_other_formats(self):
        for text in ("05/17/2020", "May 2020", "20-5-17", "", None):
            with self.subTest(text=text):
                self.assertEqual(utils._parse_date_prefix(text), (None, None))

    def test_impossible_calendar_dates_are_rejected(self):
        results = [utils._parse_date_prefix(t) for t in ("2020-13-45", "2020-13", "0000")]
        self.assertEqual(results, [(None, None)] * 3)

    def test_bool_words(self):
        for text in ("1", "true", "YES", " y "):
            with self.subTest(text=text):
                self.assertIs(utils._normalize_bool(text), True)
        for text in ("0", "false", "No", "n"):
            with self.subTest(text=text):
                self.assertIs(utils._normalize_bool(text), False)
        for text in ("maybe", "", None):
            with self.subTest(text=text):
                self.assertIsNone(utils._normalize_bool(text))

    def test_sex_words(self):
        for text in ("m", "Male", " MALE "):
            self.assertEqual(utils._normalize_sex(text), "m")
        for text in ("f", "female", "FEMALE"):
            self.assertEqual(utils._normalize_sex(text), "f")
        for text in ("x", "", None):
            self.assertIsNone(utils._normalize_sex(text))


class BuildQueryTests(TestCase):
    """build_query_q against real rows: the Q it returns must select the right ones."""

    @classmethod
    def setUpTestData(cls):
        ips = make_taxon(
            "T-IPS", scientific_name="Ips typographus",
            subfamily="Scolytinae", tribe="Ipini", genus="Ips", species="typographus",
        )
        xyl = make_taxon(
            "T-XYL", scientific_name="Xyleborus affinis",
            subfamily="Scolytinae", tribe="Xyleborini", genus="Xyleborus", species="affinis",
        )
        cls.rows = {
            "ips": make_beetle(
                image=make_image(
                    photographer="Jane Doe", image_institution="UF",
                    image_date_taken=date(2020, 5, 17), resolution_in_ppmm=Decimal("12.5"),
                    image_has_multiple_individuals=True, image_notes="found under bark",
                ),
                taxon=ips, collection_country="USA", specimen_sex="m",
                specimen_type_status="holotype", depicts_specimen="SP-1",
            ),
            "xyl": make_beetle(
                image=make_image(
                    photographer="John Smith", image_institution="BYU",
                    image_date_taken=date(2021, 1, 2), resolution_in_ppmm=Decimal("5"),
                    image_has_multiple_individuals=False,
                ),
                taxon=xyl, collection_country="Brazil", specimen_sex="f",
                specimen_type_status="paratype", depicts_specimen="SP-2",
            ),
            # Nothing filled in: no taxon, country, date, resolution...
            "bare": make_beetle(),
        }

    def search(self, text):
        """Run a search; return (names of the rows that matched, ignored-clause notes)."""
        q, ignored = utils.build_query_q(text)
        by_pk = {row.pk: name for name, row in self.rows.items()}
        return {by_pk[b.pk] for b in Beetles.objects.filter(q)}, ignored

    # --- empty / id searches ---------------------------------------------------

    def test_empty_search_matches_everything(self):
        for text in ("", "   ", None):
            with self.subTest(text=text):
                self.assertEqual(self.search(text), ({"ips", "xyl", "bare"}, []))

    def test_exact_uuid_matches_the_roi_or_every_roi_on_its_image(self):
        ips = self.rows["ips"]
        sibling = make_beetle(image=ips.image_asset)  # a second ROI on the same image

        q, ignored = utils.build_query_q(str(ips.pk))
        self.assertEqual(set(Beetles.objects.filter(q)), {ips})
        self.assertEqual(ignored, [])

        q, _ = utils.build_query_q(str(ips.image_asset_id))
        self.assertEqual(set(Beetles.objects.filter(q)), {ips, sibling})

    def test_unknown_uuid_matches_nothing(self):
        q, _ = utils.build_query_q(str(uuid.uuid4()))
        self.assertFalse(Beetles.objects.filter(q).exists())

    # --- field:value clauses ---------------------------------------------------

    def test_text_fields_match_case_insensitive_substrings(self):
        self.assertEqual(self.search("country:usa")[0], {"ips"})
        self.assertEqual(self.search("country=Bra")[0], {"xyl"})
        self.assertEqual(self.search("photographer:smith")[0], {"xyl"})

    def test_quoted_multi_word_value(self):
        self.assertEqual(self.search('photographer:"Jane Doe"')[0], {"ips"})

    def test_id_fields_need_an_exact_match(self):
        self.assertEqual(self.search("name id:t-ips")[0], {"ips"})  # any case
        self.assertEqual(self.search("name id:T-IP")[0], set())  # but not a prefix

    def test_sex_accepts_words_and_letters(self):
        self.assertEqual(self.search("sex:male")[0], {"ips"})
        self.assertEqual(self.search("sex:F")[0], {"xyl"})

    def test_unknown_sex_is_ignored_with_a_note(self):
        rows, ignored = self.search("sex:banana")
        self.assertEqual(rows, {"ips", "xyl", "bare"})  # the clause is dropped, not "no results"
        self.assertEqual(ignored, ["unknown sex value 'banana'"])

    def test_boolean_field(self):
        self.assertEqual(self.search("multiple individuals:yes")[0], {"ips"})
        self.assertEqual(self.search("multiple individuals:no")[0], {"xyl"})

    def test_invalid_boolean_is_ignored_with_a_note(self):
        rows, ignored = self.search("multiple individuals:maybe")
        self.assertEqual(rows, {"ips", "xyl", "bare"})
        self.assertEqual(len(ignored), 1)
        self.assertIn("invalid boolean 'maybe'", ignored[0])

    def test_date_by_year_month_or_day(self):
        self.assertEqual(self.search("image date:2020")[0], {"ips"})
        self.assertEqual(self.search("image date:2020-05")[0], {"ips"})
        self.assertEqual(self.search("image date:2020-05-17")[0], {"ips"})
        self.assertEqual(self.search("image date:2021-01")[0], {"xyl"})
        self.assertEqual(self.search("image date:2021-02")[0], set())

    def test_unparseable_date_is_ignored_with_a_note(self):
        rows, ignored = self.search("image date:sometime")
        self.assertEqual(rows, {"ips", "xyl", "bare"})
        self.assertIn("invalid date 'sometime'", ignored[0])

    def test_impossible_date_is_ignored_not_a_crash(self):
        _, ignored = utils.build_query_q("image date:2020-13-45")
        self.assertTrue(any("invalid date" in note for note in ignored))

    def test_resolution_comparisons(self):
        self.assertEqual(self.search("resolution:>=10")[0], {"ips"})
        self.assertEqual(self.search("resolution:<10")[0], {"xyl"})
        self.assertEqual(self.search("resolution:=5")[0], {"xyl"})
        self.assertEqual(self.search("resolution:5")[0], {"xyl"})

    def test_invalid_resolution_is_ignored_with_a_note(self):
        rows, ignored = self.search("resolution:fast")
        self.assertEqual(rows, {"ips", "xyl", "bare"})
        self.assertIn("invalid numeric 'fast'", ignored[0])

    def test_empty_value_finds_blank_values(self):
        self.assertEqual(self.search("country:")[0], {"bare"})
        self.assertEqual(self.search("image date:")[0], {"bare"})

    def test_none_keyword_finds_blank_values(self):
        self.assertEqual(self.search("country:None")[0], {"bare"})
        self.assertEqual(self.search("image date:None")[0], {"bare"})

    def test_taxonomy_fields_match_exactly_through_the_taxon(self):
        self.assertEqual(self.search("genus:ips")[0], {"ips"})
        self.assertEqual(self.search("genus:Xyleborus")[0], {"xyl"})
        self.assertEqual(self.search("genus:Ip")[0], set())  # exact, not a substring
        self.assertEqual(self.search('scientific name:"Ips typographus"')[0], {"ips"})

    def test_empty_taxonomy_value_is_ignored_with_a_note(self):
        rows, ignored = self.search("genus:")
        self.assertEqual(rows, {"ips", "xyl", "bare"})
        self.assertEqual(ignored, ["empty value for 'genus'"])

    def test_taxonomy_none_keyword_finds_specimens_with_no_taxon(self):
        self.assertEqual(self.search("genus:None")[0], {"bare"})

    def test_unknown_field_is_ignored_with_a_note(self):
        rows, ignored = self.search("colour:red")
        self.assertEqual(rows, {"ips", "xyl", "bare"})
        self.assertEqual(ignored, ["unknown field 'colour'"])

    # --- free text -------------------------------------------------------------

    def test_free_text_searches_many_fields(self):
        self.assertEqual(self.search("typographus")[0], {"ips"})  # taxon species
        self.assertEqual(self.search("smith")[0], {"xyl"})  # photographer
        self.assertEqual(self.search("bark")[0], {"ips"})  # image notes
        self.assertEqual(self.search("scolytinae")[0], {"ips", "xyl"})  # taxon subfamily
        self.assertEqual(self.search("zzz-no-such-thing")[0], set())

    # --- combining clauses -----------------------------------------------------

    def test_clauses_with_no_operator_are_anded(self):
        self.assertEqual(self.search("country:USA sex:m")[0], {"ips"})
        self.assertEqual(self.search("country:USA sex:f")[0], set())

    def test_and(self):
        self.assertEqual(self.search("sex:m AND country:USA")[0], {"ips"})
        self.assertEqual(self.search("sex:m AND country:Brazil")[0], set())

    def test_or(self):
        self.assertEqual(self.search("country:USA OR country:Brazil")[0], {"ips", "xyl"})

    def test_and_binds_tighter_than_or(self):
        # country:USA OR (country:Brazil AND sex:f) -> ips (USA) and xyl (Brazil+f).
        # Reading it left to right, (USA OR Brazil) AND f, would wrongly drop ips.
        rows, _ = self.search("country:USA OR country:Brazil AND sex:f")
        self.assertEqual(rows, {"ips", "xyl"})

    def test_not_excludes_matches_but_keeps_rows_with_no_value(self):
        self.assertEqual(self.search("NOT country:USA")[0], {"xyl", "bare"})

    def test_dangling_operator_is_ignored_with_a_note(self):
        rows, ignored = self.search("AND country:USA")
        self.assertEqual(rows, {"ips"})
        self.assertEqual(ignored, ["dangling AND"])

    def test_lone_not_matches_everything_with_a_note(self):
        rows, ignored = self.search("NOT")
        self.assertEqual(rows, {"ips", "xyl", "bare"})
        self.assertEqual(ignored, ["dangling NOT"])


class SearchFieldMappingGuardTests(TestCase):
    """Fail loudly if a model field is renamed and search would quietly stop working."""

    def test_every_searchable_field_exists_on_the_model(self):
        for label, field in utils.FIELD_MAP.items():
            with self.subTest(label=label):
                Beetles.objects.filter(**{f"{field}__isnull": True}).exists()

    def test_every_free_text_field_exists_on_the_model(self):
        for field in utils.FREE_TEXT_FIELDS:
            with self.subTest(field=field):
                Beetles.objects.filter(**{f"{field}__icontains": "x"}).exists()

    def test_taxonomy_labels_map_to_real_taxon_fields(self):
        for field in ("scientific_name", "genus", "species"):
            Taxon._meta.get_field(field)
        self.assertEqual(utils.REF_FIELD_LABELS, {"scientific name", "genus", "species"})
