"""
New species found through the game.

When a player names a species for a beetle and the gallery has no validated images of that species yet, the answer
is marked ``new_species``. If a curator later validates that beetle as exactly that species, the player found a
new species for the database: they get a one-time pop-up and a badge on their profile. No extra points, so new
players are not left behind by early finds.
"""
from django.db import IntegrityError
from django.utils import timezone

from .game import check_rois
from .models import GameAnswer, SpeciesDiscovery


def is_new_species(genus, species):
    """True when the gallery has no validated images of this species yet."""
    if not genus or not species:
        return False
    return not check_rois().filter(taxon__genus__iexact=genus, taxon__species__iexact=species).exists()


def find(player_ids=None):
    """Credit every answer that named a then-unknown species which a curator has since validated. Returns new rows."""
    answers = GameAnswer.objects.filter(
        new_species=True, mode="classify", skipped=False, roi__bbox_is_validated=True, roi__is_deleted=False,
    ).select_related("roi__taxon")
    if player_ids is not None:
        answers = answers.filter(player_id__in=list(player_ids))
    have = set(SpeciesDiscovery.objects.filter(answer__in=answers).values_list("player_id", "roi_id"))
    made = []
    for ans in answers:
        taxon = ans.roi.taxon
        if (ans.player_id, ans.roi_id) in have or taxon is None:
            continue
        if (taxon.genus or "").lower() != ans.genus.lower() or (taxon.species or "").lower() != ans.species.lower():
            continue
        try:
            made.append(SpeciesDiscovery.objects.create(
                player_id=ans.player_id, roi=ans.roi, answer=ans, genus=taxon.genus, species=taxon.species,
            ))
        except IntegrityError:
            pass
        have.add((ans.player_id, ans.roi_id))
    return made


def for_player(player):
    return list(SpeciesDiscovery.objects.filter(player=player))


def pop_unseen(player):
    """The discoveries the player hasn't been told about yet, marked as seen."""
    unseen = list(SpeciesDiscovery.objects.filter(player=player, seen_at__isnull=True).order_by("created_at"))
    if unseen:
        SpeciesDiscovery.objects.filter(id__in=[d.id for d in unseen]).update(seen_at=timezone.now())
    return unseen
