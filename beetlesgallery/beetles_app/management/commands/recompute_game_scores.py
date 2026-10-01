"""
Re-score every Beetle ID game answer and refresh every player's score (see game_scoring.py).

    manage.py recompute_game_scores               everyone
    manage.py recompute_game_scores --player ada  one player

This is how beetles validated (or corrected) since an answer was given change that answer's points, and how
consensus points follow other players' later answers. The server runs it every night from the
"Nightly game scores" workflow; a player's own answers are also re-scored whenever they leave the game.
It only writes the game's own score tables.
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from beetlesgallery.beetles_app import game_scoring
from beetlesgallery.beetles_app.game_trust import recompute_skills


class Command(BaseCommand):
    help = "Re-score every game answer and refresh players' scores."

    def add_arguments(self, parser):
        parser.add_argument("--player", help="Only this username")

    def handle(self, *args, **options):
        ids = None
        if options["player"]:
            user = get_user_model().objects.filter(username=options["player"]).first()
            if user is None:
                raise CommandError(f"No user called {options['player']}")
            ids = [user.id]
        players = get_user_model().objects.filter(game_answers__isnull=False).distinct()
        if ids:
            players = players.filter(id__in=ids)
        for player in players:   # expertise first: it decides which judges count as experts
            recompute_skills(player)
        n = game_scoring.recompute(ids)
        self.stdout.write(self.style.SUCCESS(f"Re-scored {n} player{'s' if n != 1 else ''}."))
