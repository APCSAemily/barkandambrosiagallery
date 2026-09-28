"""
Tests for the Beetle ID game: scoring rules, round building, the round API
(including that it never reveals which items are scored), and staff consensus.
"""
import json

from django.test import SimpleTestCase, override_settings
from django.urls import reverse

from beetlesgallery.beetles_app import game
from beetlesgallery.beetles_app.models import GameAnswer, GameRound, Taxon
from beetlesgallery.beetles_app.testing import PageBehaviourCase, make_beetle, make_image, make_taxon


def taxon(subfamily="Scolytinae", tribe="Xyleborini", genus="Xyleborus", species="affinis"):
    return Taxon(subfamily=subfamily, tribe=tribe, genus=genus, species=species)


class ScoringTests(SimpleTestCase):
    def test_classification_scores_each_answered_rank(self):
        answer = {"subfamily": "scolytinae", "tribe": "Xyleborini", "genus": "Xylosandrus", "species": ""}
        self.assertEqual(game.score_classification(answer, taxon()), {
            "subfamily": True, "tribe": True, "genus": False, "species": None,
        })

    def test_species_needs_the_right_genus(self):
        answer = {"genus": "Xylosandrus", "species": "affinis"}
        self.assertFalse(game.score_classification(answer, taxon())["species"])

    def test_rank_missing_from_reference_is_not_scored(self):
        answer = {"subfamily": "Scolytinae", "tribe": "Anything"}
        self.assertIsNone(game.score_classification(answer, taxon(tribe=None))["tribe"])

    def test_pair_same_genus_claims_ranks_above_and_not_species(self):
        a, b = taxon(), taxon(species="ferrugineus")
        self.assertEqual(game.score_pair("genus", a, b), {
            "subfamily": True, "tribe": True, "genus": True, "species": True,
        })
        self.assertEqual(game.score_pair("species", a, b)["species"], False)
        self.assertEqual(game.score_pair("tribe", a, b), {
            "subfamily": True, "tribe": True, "genus": False, "species": True,
        })

    def test_pair_different_subfamily(self):
        a, b = taxon(), taxon("Platypodinae", "Platypodini", "Platypus", "cylindrus")
        self.assertTrue(all(game.score_pair("different", a, b).values()))
        self.assertEqual(game.score_pair("subfamily", a, b)["subfamily"], False)

    def test_pair_unsure_is_not_scored(self):
        self.assertEqual(set(game.score_pair("unsure", taxon(), taxon()).values()), {None})

    def test_shared_deeper_rank_implies_blank_higher_rank(self):
        a, b = taxon(tribe=None), taxon(tribe="Xyleborini")
        self.assertTrue(game.shared_ranks(a, b)["tribe"])


class GameCase(PageBehaviourCase):
    def setUp(self):
        super().setUp()
        self.t_affinis = make_taxon(subfamily="Scolytinae", tribe="Xyleborini", genus="Xyleborus",
                                    species="affinis", scientific_name="Xyleborus affinis")
        self.t_ferr = make_taxon(subfamily="Scolytinae", tribe="Xyleborini", genus="Xyleborus",
                                 species="ferrugineus", scientific_name="Xyleborus ferrugineus")
        self.t_plat = make_taxon(subfamily="Platypodinae", tribe="Platypodini", genus="Platypus",
                                 species="cylindrus", scientific_name="Platypus cylindrus")

    def roi(self, taxon=None, validated=True):
        image = make_image(image_file="originals/aa/bb/test.jpg")
        return make_beetle(image=image, taxon=taxon, bbox="validated" if validated else "unvalidated")

    def post(self, name, body, *args):
        return self.client.post(reverse(name, args=args), json.dumps(body), content_type="application/json")

    def play(self, mode):
        self.client.force_login(self.user)
        res = self.post("game_start", {"mode": mode})
        self.assertEqual(res.status_code, 200, res.content)
        data = res.json()
        return GameRound.objects.get(id=data["round"]), data["item"]


class GamePageTests(GameCase):
    def test_pages_need_login(self):
        for url in [reverse("game_home"), reverse("game_play", args=["classify"])]:
            with self.subTest(url=url):
                self.assertRedirectsToLogin(self.client.get(url))

    def test_pages_load_for_player(self):
        self.client.force_login(self.user)
        for url in [reverse("game_home"), reverse("game_home") + "?sort=accuracy",
                    reverse("game_play", args=["classify"]), reverse("game_play", args=["pair"])]:
            with self.subTest(url=url):
                self.assertEqual(self.client.get(url).status_code, 200)

    def test_unknown_mode_is_404(self):
        self.client.force_login(self.user)
        self.assertEqual(self.client.get(reverse("game_play", args=["nope"])).status_code, 404)

    def test_landing_links_to_game(self):
        self.assertContains(self.client.get(reverse("image_browser")), reverse("game_home"))

    def test_review_is_staff_only(self):
        self.client.force_login(self.user)
        self.assertRedirectsToLogin(self.client.get(reverse("game_review")))
        self.client.force_login(self.staff)
        self.assertEqual(self.client.get(reverse("game_review")).status_code, 200)
        for kind in ["labels", "players"]:
            res = self.client.get(reverse("game_export", args=[kind]))
            self.assertEqual(res["Content-Type"], "text/csv")
        self.assertEqual(self.client.get(reverse("game_export", args=["x"])).status_code, 404)


class RoundBuildingTests(GameCase):
    def test_no_playable_items(self):
        self.client.force_login(self.user)
        self.assertEqual(self.post("game_start", {"mode": "classify"}).status_code, 404)

    def test_rois_without_boxes_or_deleted_are_not_used(self):
        make_beetle(image=make_image(image_file="x.jpg"), taxon=self.t_affinis)  # no bbox
        gone = self.roi(self.t_affinis)
        gone.delete()
        self.assertFalse(game.playable_rois().exists())

    @override_settings(GAME_ROUND_SIZE=10)
    def test_new_player_gets_more_checks(self):
        for _ in range(10):
            self.roi(self.t_affinis)
            self.roi(validated=False)
        rnd = game.start_round(self.user, "classify")
        self.assertEqual(sum(i["check"] for i in rnd.items), 6)

    @override_settings(GAME_ROUND_SIZE=10, GAME_CALIBRATION_CHECKS=0)
    def test_calibrated_player_gets_fewer_checks(self):
        for _ in range(10):
            self.roi(self.t_affinis)
            self.roi(validated=False)
        rnd = game.start_round(self.user, "classify")
        self.assertEqual(sum(i["check"] for i in rnd.items), 2)

    @override_settings(GAME_ROUND_SIZE=4)
    def test_short_pool_is_topped_up_from_the_other(self):
        for _ in range(4):
            self.roi(self.t_affinis)
        rnd = game.start_round(self.user, "classify")
        self.assertEqual(len(rnd.items), 4)
        self.assertTrue(all(i["check"] for i in rnd.items))

    @override_settings(GAME_ROUND_SIZE=6)
    def test_pairs_use_a_validated_partner(self):
        validated = {str(self.roi(t).id) for t in [self.t_affinis, self.t_ferr, self.t_plat] * 2}
        for _ in range(3):
            self.roi(self.t_affinis, validated=False)
        rnd = game.start_round(self.user, "pair")
        self.assertTrue(rnd.items)
        for item in rnd.items:
            self.assertIn(item["b"], validated)
            self.assertNotEqual(item["a"], item["b"])
            self.assertEqual(item["a"] in validated, item["check"])


class ClassifyApiTests(GameCase):
    @override_settings(GAME_ROUND_SIZE=2, GAME_MIN_JUDGED_FOR_ACCURACY=1)
    def test_round_scores_checks_only_and_reveals_nothing(self):
        check = self.roi(self.t_affinis)
        open_roi = self.roi(self.t_ferr, validated=False)
        rnd, item = self.play("classify")

        # The payload must not say which ROI this is or whether it is scored.
        self.assertEqual(set(item), {"index", "position", "total", "images"})
        self.assertEqual(set(item["images"][0]), {"url", "box"})
        self.assertNotIn(str(check.id), json.dumps(item))

        answer = {"subfamily": "Scolytinae", "tribe": "Xyleborini", "genus": "Xyleborus", "species": "affinis"}
        res = self.post("game_answer", dict(answer, index=item["index"]), rnd.id)
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("correct", json.dumps(res.json()))
        nxt = res.json()["item"]

        res = self.post("game_answer", dict(answer, index=nxt["index"]), rnd.id)
        data = res.json()
        self.assertTrue(data["done"])
        self.assertEqual(data["summary"]["round_labelled"], 2)

        scored = GameAnswer.objects.get(roi=check)
        unscored = GameAnswer.objects.get(roi=open_roi)
        self.assertTrue(scored.is_check)
        self.assertTrue(scored.correct_species)
        self.assertFalse(unscored.is_check)
        self.assertIsNone(unscored.correct_species)
        self.assertEqual(data["summary"]["accuracy"], 1.0)
        rnd.refresh_from_db()
        self.assertIsNotNone(rnd.finished_at)

    @override_settings(GAME_ROUND_SIZE=1)
    def test_answer_must_match_the_taxonomy(self):
        self.roi(self.t_affinis)
        rnd, item = self.play("classify")
        for bad in [{"genus": "Madeupus"}, {"species": "affinis"}, {"genus": "Platypus", "tribe": "Xyleborini"}, {}]:
            with self.subTest(answer=bad):
                res = self.post("game_answer", dict(bad, index=item["index"]), rnd.id)
                self.assertEqual(res.status_code, 400)
        self.assertFalse(GameAnswer.objects.exists())

    @override_settings(GAME_ROUND_SIZE=1)
    def test_partial_answer_and_skip(self):
        self.roi(self.t_affinis)
        rnd, item = self.play("classify")
        res = self.post("game_answer", {"index": item["index"], "skipped": True}, rnd.id)
        self.assertTrue(res.json()["done"])
        self.assertTrue(GameAnswer.objects.get().skipped)

        rnd, item = self.play("classify")
        self.post("game_answer", {"index": item["index"], "subfamily": "Scolytinae"}, rnd.id)
        ans = GameAnswer.objects.get(round=rnd)
        self.assertEqual((ans.correct_subfamily, ans.correct_genus), (True, None))

    @override_settings(GAME_ROUND_SIZE=2)
    def test_out_of_step_and_other_players_rounds(self):
        self.roi(self.t_affinis)
        self.roi(self.t_ferr)
        rnd, item = self.play("classify")
        res = self.post("game_answer", {"index": item["index"] + 1, "genus": "Xyleborus"}, rnd.id)
        self.assertEqual(res.status_code, 409)

        self.client.force_login(self.staff)
        res = self.post("game_answer", {"index": item["index"], "genus": "Xyleborus"}, rnd.id)
        self.assertEqual(res.status_code, 404)


class PairApiTests(GameCase):
    @override_settings(GAME_ROUND_SIZE=1)
    def test_pair_check_is_scored(self):
        self.roi(self.t_affinis)
        self.roi(self.t_ferr)
        rnd, item = self.play("pair")
        self.assertEqual(len(item["images"]), 2)
        res = self.post("game_answer", {"index": item["index"], "pair_answer": "genus"}, rnd.id)
        self.assertTrue(res.json()["done"])
        ans = GameAnswer.objects.get()
        self.assertTrue(ans.is_check)
        self.assertEqual(
            [ans.correct_subfamily, ans.correct_tribe, ans.correct_genus, ans.correct_species],
            [True, True, True, True],
        )

    @override_settings(GAME_ROUND_SIZE=1)
    def test_pair_needs_a_valid_choice(self):
        self.roi(self.t_affinis)
        self.roi(self.t_ferr)
        rnd, item = self.play("pair")
        res = self.post("game_answer", {"index": item["index"], "pair_answer": "cousin"}, rnd.id)
        self.assertEqual(res.status_code, 400)


class TaxaApiTests(GameCase):
    def test_cascading_options(self):
        self.client.force_login(self.user)
        url = reverse("game_taxa")
        subfamilies = self.client.get(url, {"rank": "subfamily"}).json()["options"]
        self.assertEqual([o["value"] for o in subfamilies], ["Platypodinae", "Scolytinae"])

        genera = self.client.get(url, {"rank": "genus", "subfamily": "Scolytinae"}).json()["options"]
        self.assertEqual(genera, [{"value": "Xyleborus", "subfamily": "Scolytinae", "tribe": "Xyleborini"}])

        species = self.client.get(url, {"rank": "species", "genus": "Xyleborus"}).json()["options"]
        self.assertEqual([o["value"] for o in species], ["affinis", "ferrugineus"])
        self.assertEqual(self.client.get(url, {"rank": "species"}).json()["options"], [])
        self.assertEqual(self.client.get(url, {"rank": "kingdom"}).status_code, 400)

    def test_search(self):
        self.client.force_login(self.user)
        results = self.client.get(reverse("game_taxa_search"), {"q": "xyl"}).json()["results"]
        self.assertEqual(results[0]["kind"], "genus")
        self.assertEqual({r["label"] for r in results[1:]}, {"Xyleborus affinis", "Xyleborus ferrugineus"})


class ConsensusTests(GameCase):
    def test_classify_and_pair_votes_combine(self):
        target = self.roi(self.t_plat, validated=False)
        partner = self.roi(self.t_affinis)
        rnd = GameRound.objects.create(player=self.user, mode="classify", items=[])
        GameAnswer.objects.create(round=rnd, player=self.user, mode="classify", index=0, roi=target,
                                  subfamily="Scolytinae", tribe="Xyleborini", genus="Xyleborus", species="affinis")
        prnd = GameRound.objects.create(player=self.staff, mode="pair", items=[])
        GameAnswer.objects.create(round=prnd, player=self.staff, mode="pair", index=0, roi=target,
                                  roi_b=partner, pair_answer="genus")
        GameAnswer.objects.create(round=prnd, player=self.staff, mode="pair", index=1, roi=target,
                                  roi_b=partner, pair_answer="different")

        [entry] = game.consensus()
        self.assertEqual(entry["answers"], 3)
        self.assertEqual(entry["ranks"]["genus"]["value"], "Xyleborus")
        self.assertEqual(entry["ranks"]["genus"]["votes"], 2)
        self.assertEqual(entry["ranks"]["species"]["value"], "Xyleborus affinis")
        self.assertEqual(entry["ranks"]["species"]["votes"], 1)

    def test_reliable_player_outweighs_unreliable(self):
        target = self.roi(validated=False)
        check = self.roi(self.t_affinis)
        good = GameRound.objects.create(player=self.user, mode="classify", items=[])
        bad = GameRound.objects.create(player=self.staff, mode="classify", items=[])
        for i in range(5):
            GameAnswer.objects.create(round=good, player=self.user, mode="classify", index=i, roi=check,
                                      is_check=True, genus="Xyleborus", correct_genus=True)
            GameAnswer.objects.create(round=bad, player=self.staff, mode="classify", index=i, roi=check,
                                      is_check=True, genus="Platypus", correct_genus=False)
        GameAnswer.objects.create(round=good, player=self.user, mode="classify", index=10, roi=target, genus="Xyleborus")
        GameAnswer.objects.create(round=bad, player=self.staff, mode="classify", index=10, roi=target, genus="Platypus")

        [entry] = game.consensus()
        self.assertEqual(entry["ranks"]["genus"]["value"], "Xyleborus")
        self.assertGreater(entry["ranks"]["genus"]["support"], 0.8)

        self.client.force_login(self.staff)
        csv_text = self.client.get(reverse("game_export", args=["labels"])).content.decode()
        self.assertIn(str(target.id), csv_text)
