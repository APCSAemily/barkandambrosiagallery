"""The quick loop: what others said after each answer, beetles others named, participation points."""
import json

from django.test import override_settings

from beetlesgallery.beetles_app import game, game_scoring as scoring
from beetlesgallery.beetles_app.models import AnswerPoints, GameAnswer
from beetlesgallery.beetles_app.test_game import AFFINIS, FERR
from beetlesgallery.beetles_app.test_game_scoring import PLAT, ScoringCase


class CommunityTests(ScoringCase):
    def answer_in_feed(self, fields):
        self.client.force_login(self.user)
        rnd, item = self.play("classify")
        return item, self.post("game_answer", dict(fields, index=item["index"]), rnd.id).json()

    @override_settings(GAME_ROUND_SIZE=1)
    def test_after_answering_you_see_what_most_others_said_and_whether_you_agree(self):
        target = self.roi(self.t_affinis, validated=False)
        for name, fields in (("a", AFFINIS), ("b", AFFINIS), ("c", FERR)):
            self.answer(self.player(name), target, fields)
        item, res = self.answer_in_feed(AFFINIS)
        self.assertEqual(item["others"], 3)          # before answering: only how many
        c = res["community"]
        self.assertEqual((c["players"], c["rank"], c["name"], c["count"], c["of"], c["agree"]),
                         (3, "species", "Xyleborus affinis", 2, 3, True))

    @override_settings(GAME_ROUND_SIZE=1)
    def test_it_falls_back_to_the_rank_most_of_them_reached(self):
        target = self.roi(self.t_affinis, validated=False)
        genus_only = dict(AFFINIS, species="")
        for name in ("a", "b", "c"):
            self.answer(self.player(name), target, genus_only)
        _, res = self.answer_in_feed(PLAT)
        c = res["community"]
        self.assertEqual((c["rank"], c["name"], c["agree"]), ("genus", "Xyleborus", False))

    @override_settings(GAME_ROUND_SIZE=1)
    def test_the_first_to_name_a_beetle_is_told_so(self):
        self.roi(self.t_affinis, validated=False)
        item, res = self.answer_in_feed(AFFINIS)
        self.assertNotIn("others", item)
        self.assertEqual(res["community"], {"players": 0})

    @override_settings(GAME_ROUND_SIZE=1)
    def test_it_never_carries_the_truth(self):
        target = self.roi(self.t_affinis)   # validated
        self.answer(self.player("a"), target, FERR)
        _, res = self.answer_in_feed(AFFINIS)
        text = json.dumps(res["community"]).lower()
        self.assertIn("ferrugineus", text)    # what the other player said, even though it is wrong
        for word in ("correct", "truth", "valid"):
            self.assertNotIn(word, text)


class PeerBeetleTests(ScoringCase):
    @override_settings(GAME_PEER_SHARE=1.0, GAME_ROUND_SIZE=5, GAME_CHECK_RATIO_NEW=0.2)
    def test_beetles_others_named_come_first(self):
        named = [self.roi(self.t_affinis, validated=False) for _ in range(3)]
        for roi in named:
            self.answer(self.player(f"p{roi.id.hex[:6]}"), roi, AFFINIS)
        for _ in range(10):
            self.roi(self.t_affinis, validated=False)
        self.roi(self.t_affinis)
        rnd = game.start_round(self.user, "classify")
        opens = {i["a"] for i in rnd.items if not i["check"]}
        self.assertTrue({str(r.id) for r in named} <= opens)

    @override_settings(GAME_PEER_MAX_OTHERS=2)
    def test_beetles_with_plenty_of_opinions_already_are_not_pushed(self):
        crowded = self.roi(self.t_affinis, validated=False)
        for name in ("a", "b", "c"):
            self.answer(self.player(name), crowded, AFFINIS)
        self.assertFalse(game.peer_rois(self.user, game.open_rois()).filter(id=crowded.id).exists())

    def test_not_ones_you_answered_yourself(self):
        roi = self.roi(self.t_affinis, validated=False)
        self.answer(self.player("a"), roi, AFFINIS)
        self.answer(self.user, roi, AFFINIS)
        self.assertFalse(game.peer_rois(self.user, game.open_rois()).exists())


@override_settings(GAME_POINTS_PARTICIPATION=0.5)
class ParticipationTests(ScoringCase):
    def test_every_real_answer_earns_a_little_but_skips_do_not(self):
        right = self.answer(self.user, self.roi(self.t_affinis), AFFINIS)
        unknown = self.answer(self.user, self.roi(self.t_affinis, validated=False), AFFINIS)
        skip = self.answer(self.user, self.roi(self.t_affinis), skipped=True)
        scoring.recompute([self.user.id])
        got = {a.pk: AnswerPoints.objects.get(answer=a).points for a in (right, unknown, skip)}
        self.assertEqual((got[right.pk], got[unknown.pk], got[skip.pk]), (15.5, 0.5, -0.25))

    def test_wrong_answers_still_cost_overall(self):
        wrong = self.answer(self.user, self.roi(self.t_affinis), PLAT)
        scoring.recompute([self.user.id])
        self.assertEqual(AnswerPoints.objects.get(answer=wrong).points, -10.75)


class PageTests(ScoringCase):
    def test_the_feed_has_the_combo_and_the_reveal(self):
        self.client.force_login(self.user)
        page = self.client.get("/game/play/classify/").content.decode()
        for text in ('id="combo"', "showCommunity", "in a row", "Named by", "Last beetle"):
            self.assertIn(text, page)
