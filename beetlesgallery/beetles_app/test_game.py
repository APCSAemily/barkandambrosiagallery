"""
Tests for the Beetle ID game: scoring rules, round building, the round API
(including that it never reveals which items are scored), and staff consensus.
"""
import json

from django.test import SimpleTestCase, override_settings
from django.urls import reverse
from django.utils import timezone

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
        self.assertEqual(set(item), {"index", "position", "total", "images", "prefetch"})
        self.assertEqual(set(item["images"][0]), {"url", "box"})
        self.assertNotIn(str(check.id), json.dumps(item))
        self.assertEqual(len(item["prefetch"]), 1)  # the other item's photo

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


# ---------------------------------------------------------------------------
# Expertise, trusted proposals, difficulty and rounds
# ---------------------------------------------------------------------------
from unittest import mock  # noqa: E402

from beetlesgallery.beetles_app import game_trust  # noqa: E402
from beetlesgallery.beetles_app.models import ImageLock, LabelReview, PlayerSkill, RoiDifficulty  # noqa: E402

# Small thresholds so a handful of answers proves competence: answers_needed() == 3.
SMALL_TRUST = dict(GAME_TRUST_MIN_JUDGED=3, GAME_TRUST_MIN_LOWER_BOUND=0.4)


class WilsonTests(SimpleTestCase):
    def test_default_thresholds_need_35_perfect_answers(self):
        self.assertEqual(game_trust.answers_needed(), 35)
        self.assertTrue(game_trust.is_proven(35, 35))
        self.assertFalse(game_trust.is_proven(34, 35))
        self.assertFalse(game_trust.is_proven(14, 14))

    @override_settings(**SMALL_TRUST)
    def test_small_thresholds(self):
        self.assertEqual(game_trust.answers_needed(), 3)


@override_settings(**SMALL_TRUST)
class TrustCase(GameCase):
    def setUp(self):
        super().setUp()
        self.t_xylo = make_taxon(subfamily="Scolytinae", tribe="Xyleborini", genus="Xylosandrus",
                                 species="crassiusculus", scientific_name="Xylosandrus crassiusculus")
        self.t_ambro = make_taxon(subfamily="Scolytinae", tribe="Xyleborini", genus="Ambrosiodmus",
                                  species="minor", scientific_name="Ambrosiodmus minor")

    def answer(self, player, roi, taxon=None, check=True, correct=True, **given):
        rnd = GameRound.objects.create(player=player, mode="classify", items=[])
        fields = dict(given)
        if check:
            ref = roi.taxon
            fields.update(ref_subfamily=ref.subfamily, ref_tribe=ref.tribe, ref_genus=ref.genus, ref_species=ref.species)
            for r in game.RANKS:
                fields[f"correct_{r}"] = correct
        return GameAnswer.objects.create(round=rnd, player=player, mode="classify", index=0, roi=roi,
                                         is_check=check, **fields)

    def prove(self, player, taxon, n=3, correct=True):
        for _ in range(n):
            self.answer(player, self.roi(taxon), correct=correct)
        game_trust.recompute_skills(player)

    def label(self, player, roi, taxon):
        return self.answer(player, roi, check=False, subfamily=taxon.subfamily, tribe=taxon.tribe,
                           genus=taxon.genus, species=taxon.species)



class TrustTests(TrustCase):
    def test_skills_are_per_rank_and_branch(self):
        self.prove(self.user, self.t_affinis)
        skills = {(s.rank, s.branch): s for s in PlayerSkill.objects.filter(player=self.user)}
        self.assertTrue(skills[("species", "Xyleborus")].proven)
        self.assertTrue(skills[("genus", "Xyleborini")].proven)
        self.assertTrue(skills[("tribe", "Scolytinae")].proven)
        self.assertTrue(skills[("subfamily", "")].proven)
        self.assertIsNotNone(skills[("species", "Xyleborus")].proven_at)

    def test_wrong_answers_do_not_prove(self):
        self.prove(self.user, self.t_affinis, correct=False)
        self.assertFalse(PlayerSkill.objects.filter(player=self.user, proven=True).exists())

    def test_replaying_the_same_roi_counts_once(self):
        roi = self.roi(self.t_affinis)
        for _ in range(5):
            self.answer(self.user, roi)
        game_trust.recompute_skills(self.user)
        skill = PlayerSkill.objects.get(player=self.user, rank="species", branch="Xyleborus")
        self.assertEqual((skill.judged, skill.proven), (1, False))

    def test_expert_label_is_trusted_down_to_species(self):
        self.prove(self.user, self.t_affinis)
        target = self.roi(validated=False)
        self.label(self.user, target, self.t_ferr)
        [entry] = game.consensus(roi_ids=[target.id])
        self.assertEqual(entry["trusted_rank"], "species")
        self.assertEqual(entry["taxon"], self.t_ferr)

    def test_non_expert_label_is_not_trusted(self):
        target = self.roi(validated=False)
        self.label(self.user, target, self.t_ferr)
        [entry] = game.consensus(roi_ids=[target.id])
        self.assertEqual(entry["trusted_rank"], "")
        self.assertFalse(entry["ranks"]["genus"]["trusted"])

    def test_expertise_in_another_testable_genus_does_not_carry_over(self):
        self.prove(self.user, self.t_plat)  # Platypus expert only
        for _ in range(3):
            self.roi(self.t_affinis)  # Xyleborus is testable
        target = self.roi(validated=False)
        self.label(self.user, target, self.t_affinis)
        [entry] = game.consensus(roi_ids=[target.id])
        # Subfamily-level skill is overall, so that part is trusted; nothing below it.
        self.assertEqual(entry["trusted_rank"], "subfamily")

    def test_expert_disagreement_blocks_trust(self):
        self.prove(self.user, self.t_affinis)
        self.prove(self.staff, self.t_affinis)
        target = self.roi(validated=False)
        self.label(self.user, target, self.t_affinis)
        self.label(self.staff, target, self.t_ferr)
        [entry] = game.consensus(roi_ids=[target.id])
        self.assertEqual(entry["trusted_rank"], "genus")

    def test_untestable_genus_trusted_via_sibling_genera(self):
        # Xylosandrus has no validated ROIs, so it can't be tested directly.
        self.prove(self.user, self.t_affinis)  # Xyleborus
        target = self.roi(validated=False)
        self.label(self.user, target, self.t_xylo)
        [entry] = game.consensus(roi_ids=[target.id])
        self.assertEqual(entry["trusted_rank"], "genus")  # one sibling genus is not enough

        self.prove(self.user, self.t_ambro)  # second genus in Xyleborini
        from django.core.cache import cache
        cache.clear()
        [entry] = game.consensus(roi_ids=[target.id])
        self.assertEqual(entry["trusted_rank"], "species")


class ProposalApiTests(TrustCase):
    def test_proposals_for_image_and_accept(self):
        self.prove(self.user, self.t_affinis)
        target = self.roi(validated=False)
        self.label(self.user, target, self.t_ferr)
        url = reverse("game_proposals")

        self.client.force_login(self.user)
        self.assertRedirectsToLogin(self.client.get(url, {"image_asset": target.image_asset_id}))

        self.client.force_login(self.staff)
        data = self.client.get(url, {"image_asset": target.image_asset_id}).json()["proposals"]
        proposal = data[str(target.id)]
        self.assertEqual(proposal["trusted_rank"], "species")
        self.assertEqual(proposal["taxon"]["valid_species_id"], self.t_ferr.valid_species_id)
        self.assertIsNone(proposal["review"])

        res = self.post("game_proposal_review", {"decision": "accept"}, target.id)
        self.assertEqual(res.status_code, 200, res.content)
        target.refresh_from_db()
        self.assertEqual(target.taxon, self.t_ferr)
        self.assertFalse(target.bbox_is_validated)
        review = LabelReview.objects.get()
        self.assertEqual((review.decision, review.reviewed_by, review.trusted_rank), ("accepted", self.staff, "species"))

        proposal = self.client.get(url, {"image_asset": target.image_asset_id}).json()["proposals"][str(target.id)]
        self.assertEqual(proposal["review"]["decision"], "accepted")

    def test_dismiss_and_genus_only(self):
        target = self.roi(validated=False)
        self.answer(self.user, target, check=False, subfamily="Scolytinae", tribe="Xyleborini", genus="Xyleborus")
        self.client.force_login(self.staff)
        self.assertEqual(self.post("game_proposal_review", {"decision": "accept"}, target.id).status_code, 400)
        self.assertEqual(self.post("game_proposal_review", {"decision": "dismiss"}, target.id).status_code, 200)
        self.assertEqual(LabelReview.objects.get().decision, "dismissed")

    def test_locked_image_is_refused(self):
        target = self.roi(validated=False)
        self.label(self.user, target, self.t_ferr)
        ImageLock.objects.create(image_asset=target.image_asset, locked_by=self.superuser)
        self.client.force_login(self.staff)
        self.assertEqual(self.post("game_proposal_review", {"decision": "accept"}, target.id).status_code, 409)

    def test_annotation_page_loads_proposal_ui(self):
        self.client.force_login(self.staff)
        self.assertContains(self.client.get(reverse("tool_annotate")), "loadGameProposals")


class DifficultyTests(GameCase):
    def test_game_difficulty_from_answers(self):
        roi = self.roi(self.t_affinis)
        rnd = GameRound.objects.create(player=self.user, mode="classify", items=[])
        for i in range(4):
            GameAnswer.objects.create(round=rnd, player=self.user, mode="classify", index=i, roi=roi,
                                      is_check=True, genus="Platypus", correct_genus=False)
        game.update_difficulty([roi.id])
        self.assertGreater(RoiDifficulty.objects.get(roi=roi).game_difficulty, 0.7)

    def test_model_difficulty_wins(self):
        roi = self.roi(self.t_affinis)
        diff = RoiDifficulty.objects.create(roi=roi, model_difficulty=0.1, game_difficulty=0.9)
        self.assertEqual(diff.value, 0.1)

    def test_target_rises_with_rounds(self):
        start = game.target_difficulty(self.user)
        for _ in range(5):
            GameRound.objects.create(player=self.user, mode="classify", items=[], finished_at=timezone.now())
        self.assertGreater(game.target_difficulty(self.user), start)

    def test_pick_near_prefers_matching_difficulty(self):
        easy = [self.roi(self.t_affinis) for _ in range(5)]
        hard = [self.roi(self.t_affinis) for _ in range(5)]
        for r in easy:
            RoiDifficulty.objects.create(roi=r, model_difficulty=0.05)
        for r in hard:
            RoiDifficulty.objects.create(roi=r, model_difficulty=0.95)
        ids = [r.id for r in easy + hard]
        picked = game._pick_near(ids, 5, target=0.1)
        self.assertEqual(set(picked), {r.id for r in easy})


class RoundFlowTests(GameCase):
    @override_settings(GAME_ROUND_SIZE=3)
    def test_reload_resumes_the_round(self):
        for _ in range(3):
            self.roi(self.t_affinis)
        rnd, item = self.play("classify")
        self.post("game_answer", {"index": item["index"], "genus": "Xyleborus"}, rnd.id)
        again, item2 = self.play("classify")
        self.assertEqual(again.id, rnd.id)
        self.assertEqual(item2["position"], 2)

    def test_focus_targets_branches_the_player_labels(self):
        self.assertIsNone(game.focus_filter(self.user))
        rnd = GameRound.objects.create(player=self.user, mode="classify", items=[])
        GameAnswer.objects.create(round=rnd, player=self.user, mode="classify", index=0,
                                  roi=self.roi(validated=False), tribe="Xyleborini", genus="Xyleborus")
        focused = game.check_rois().filter(game.focus_filter(self.user))
        inside, outside = self.roi(self.t_affinis), self.roi(self.t_plat)
        self.assertIn(inside, focused)
        self.assertNotIn(outside, focused)

    def test_malformed_taxa_are_not_offered(self):
        # A shifted row from the real species list: epithet in the subfamily column.
        make_taxon(subfamily="alienus", tribe="", genus="", species="", scientific_name="Glochiphorus alienus")
        self.client.force_login(self.user)
        options = self.client.get(reverse("game_taxa"), {"rank": "subfamily"}).json()["options"]
        self.assertNotIn("alienus", [o["value"] for o in options])

    def test_response_time_is_recorded(self):
        self.roi(self.t_affinis)
        with override_settings(GAME_ROUND_SIZE=1):
            rnd, item = self.play("classify")
        self.post("game_answer", {"index": item["index"], "genus": "Xyleborus", "elapsed_ms": 4200}, rnd.id)
        self.assertEqual(GameAnswer.objects.get().response_ms, 4200)


class ReportTests(GameCase):
    def test_own_report(self):
        self.client.force_login(self.user)
        self.assertRedirectsToLogin(self.client.get(reverse("game_player_report", args=[self.staff.id])))
        res = self.client.get(reverse("game_report"))
        self.assertContains(res, "My performance")

    def test_staff_can_view_any_report(self):
        self.client.force_login(self.staff)
        res = self.client.get(reverse("game_player_report", args=[self.user.id]))
        self.assertContains(res, self.user.username)
        self.assertEqual(self.client.get(reverse("game_export", args=["skills"]))["Content-Type"], "text/csv")


@override_settings(**SMALL_TRUST, GAME_REPORT_MIN_JUDGED=2)
class ReportPrivacyTests(TrustCase):
    def test_report_only_shows_groups_the_player_named(self):
        # Scored items were Xyleborus; the player called them Platypus every time.
        for _ in range(2):
            self.answer(self.user, self.roi(self.t_affinis), correct=False, subfamily="Platypodinae",
                        tribe="Platypodini", genus="Platypus")
        game_trust.recompute_skills(self.user)
        progressing = game_trust.player_report(self.user)["progressing"]
        self.assertNotIn("Xyleborus", [s.branch for s in progressing])
        self.assertEqual([s.rank for s in progressing], ["subfamily"])
