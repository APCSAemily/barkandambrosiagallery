"""
Reviewing proposed interactions.

A proposal is one claim from one source. Experts decide a *claim* (a beetle and a partner), looking at every source
that proposes it, so the same claim found in ten papers is one decision:

    accept     publishes one PathogenInteraction (origin "proposal"), cited from the best-scoring source, and marks
               every waiting proposal of the claim accepted and linked to it
    covered    the claim is already in the published dataset: the proposals are marked accepted and linked to that
               existing row, and nothing new is published
    reject     marks every waiting proposal of the claim rejected (kept, so it is not proposed again)
    reopen     puts a rejected claim back in the queue

Accepted claims can be changed or removed in the admin (PathogenInteraction). A claim is decided once: the rows are
locked while it is decided, so two reviewers cannot both act on it.
"""
import re
from dataclasses import dataclass, field

from django.db import transaction
from django.db.models import Count, F, Max, Q
from django.db.models.functions import Lower
from django.utils import timezone
from django.utils.html import escape
from django.utils.safestring import mark_safe

from .models import InteractionProposal, PathogenInteraction

ACCEPT, COVERED, REJECT, REOPEN = "accept", "covered", "reject", "reopen"
DECISIONS = (ACCEPT, COVERED, REJECT, REOPEN)
CATEGORIES = ["Fungi", "Nematode", "Bacteria", "Virus", "Protist", "Microsporidia", "Mite", "Host plant", "Insect", "Other"]
RELATIONSHIPS = ["pathogen", "parasite", "symbiont", "host plant", "predator", "associate"]


class ReviewError(Exception):
    """Something to tell the reviewer; nothing was changed."""


def claim_key(beetle_valid_species_id, partner_name):
    return f"{beetle_valid_species_id}|{' '.join(partner_name.split()).lower()}"


def split_claim_key(key):
    beetle_id, _, partner = (key or "").partition("|")
    if not beetle_id or not partner:
        raise ReviewError("That claim was not recognised.")
    return beetle_id, partner


def safe_url(url):
    """Only web links are ever shown as links."""
    return url if re.match(r"^https?://\S+$", url or "", re.I) else ""


def citation_for(authors, year, journal="", title=""):
    """'Andrei et al., 2013' (the published dataset's style), from Europe PMC-style authors 'Andrei AM, Lupăştean D.'"""
    names = []
    for part in re.split(r",\s*", (authors or "").strip().rstrip(".")):
        tokens = part.split()
        if tokens and re.fullmatch(r"[A-Z]{1,4}\.?", tokens[-1]) and len(tokens) > 1:
            tokens = tokens[:-1]
        if tokens:
            names.append(" ".join(tokens))
    if not names:
        lead = journal or (title[:60] + "..." if len(title) > 60 else title)
    elif len(names) == 1:
        lead = names[0]
    elif len(names) == 2:
        lead = f"{names[0]} and {names[1]}"
    else:
        lead = f"{names[0]} et al."
    return f"{lead}, {year}" if year and lead else (lead or (str(year) if year else ""))


def highlight(text, terms):
    """``text`` as safe HTML with every occurrence of ``terms`` wrapped in <mark>. Everything else is escaped."""
    terms = sorted({t for t in terms if t}, key=len, reverse=True)
    if not terms:
        return escape(text)
    pattern = re.compile("(" + "|".join(re.escape(t) for t in terms) + ")", re.I)
    out = []
    for i, piece in enumerate(pattern.split(text or "")):
        out.append(f"<mark>{escape(piece)}</mark>" if i % 2 else str(escape(piece)))
    return mark_safe("".join(out))


@dataclass
class Claim:
    key: str
    beetle_name: str
    beetle_id: str
    partner_name: str
    category: str
    relationship: str
    best_score: float | None
    proposals: list = field(default_factory=list)
    existing: list = field(default_factory=list)      # published dataset rows for the same beetle and partner
    covered_by: object = None                          # the exact same pair, already published

    @property
    def sources(self):
        return len(self.proposals)


def _filtered(status, category="", min_score=None, q=""):
    qs = InteractionProposal.objects.filter(status=status)
    if category:
        qs = qs.filter(category__iexact=category)
    if min_score is not None:
        qs = qs.filter(score__gte=min_score)
    if q:
        qs = qs.filter(Q(beetle_name__icontains=q) | Q(partner_name__icontains=q))
    return qs


def claim_groups(status, category="", min_score=None, q=""):
    """A queryset of claims (one row each), best score first, for a Paginator."""
    return (
        _filtered(status, category, min_score, q)
        .values("beetle_valid_species_id", partner_lower=Lower("partner_name"))
        .annotate(best=Max("score"), n=Count("id"), latest=Max("created_at"))
        .order_by(F("best").desc(nulls_last=True), "-latest", "beetle_valid_species_id", "partner_lower")
    )


def load_claims(groups, status, category="", min_score=None, q=""):
    """The claims for one page of ``claim_groups``, with their sources and what the dataset already holds."""
    groups = list(groups)
    if not groups:
        return []
    beetle_ids = {g["beetle_valid_species_id"] for g in groups}
    wanted = {(g["beetle_valid_species_id"], g["partner_lower"]) for g in groups}
    by_claim = {}
    proposals = (
        _filtered(status, category, min_score, q).filter(beetle_valid_species_id__in=beetle_ids)
        .select_related("reviewed_by").annotate(partner_lower=Lower("partner_name"))
        .order_by(F("score").desc(nulls_last=True), "-created_at")
    )
    for p in proposals:
        if (p.beetle_valid_species_id, p.partner_lower) in wanted:
            by_claim.setdefault((p.beetle_valid_species_id, p.partner_lower), []).append(p)

    dataset = {}
    for row in PathogenInteraction.objects.filter(beetle_host_id__in=beetle_ids).order_by("record_number", "created_at"):
        dataset.setdefault(row.beetle_host_id, []).append(row)

    claims = []
    for g in groups:
        rows = by_claim.get((g["beetle_valid_species_id"], g["partner_lower"]), [])
        if not rows:
            continue
        best = rows[0]
        partner_words = best.partner_name.lower().split()
        existing, exact = [], None
        for row in dataset.get(best.beetle_valid_species_id, []):
            pathogen = (row.pathogen or "").lower()
            if pathogen == " ".join(partner_words):
                exact = exact or row
                existing.append(row)
            elif partner_words and pathogen.split()[:1] == partner_words[:1]:
                existing.append(row)          # the same genus: worth a look
        claims.append(Claim(
            key=claim_key(best.beetle_valid_species_id, best.partner_name), beetle_name=best.beetle_name,
            beetle_id=best.beetle_valid_species_id, partner_name=best.partner_name, category=best.category,
            relationship=best.relationship, best_score=g["best"], proposals=rows, existing=existing, covered_by=exact,
        ))
    return claims


def decorate(claim):
    """Add what the page shows for each source: a safe link and the evidence with the names highlighted."""
    beetle_terms = [claim.beetle_name] + [f"{claim.beetle_name[0]}. {claim.beetle_name.split()[-1]}"]
    for p in claim.proposals:
        p.link = safe_url(p.source_url)
        p.doi_link = f"https://doi.org/{p.source_doi}" if p.source_doi else ""
        p.evidence_html = highlight(p.evidence, beetle_terms + [claim.partner_name])
    return claim


def decide(key, decision, user, note="", partner_name="", category="", relationship=""):
    """
    Apply a decision to the claim ``key``. Returns (decision, published PathogenInteraction or None, proposals changed).
    ``partner_name``, ``category`` and ``relationship`` let the reviewer correct what is published (accept only).
    """
    if decision not in DECISIONS:
        raise ReviewError("Choose accept, already in the dataset, reject or reopen.")
    beetle_id, partner_lower = split_claim_key(key)
    note = (note or "").strip()[:1000]
    wanted_status = InteractionProposal.Status.REJECTED if decision == REOPEN else InteractionProposal.Status.PROPOSED

    with transaction.atomic():
        proposals = list(
            InteractionProposal.objects.select_for_update()
            .annotate(partner_lower=Lower("partner_name"))
            .filter(beetle_valid_species_id=beetle_id, partner_lower=partner_lower, status=wanted_status)
            .order_by(F("score").desc(nulls_last=True), "-created_at")
        )
        if not proposals:
            raise ReviewError("That claim was already decided (or has gone). Reload the page.")
        best, ids, now = proposals[0], [p.pk for p in proposals], timezone.now()
        updates = InteractionProposal.objects.filter(pk__in=ids)

        if decision == REOPEN:
            updates.update(status=InteractionProposal.Status.PROPOSED, reviewed_by=None, reviewed_at=None, review_note="")
            return decision, None, len(ids)

        if decision == REJECT:
            updates.update(status=InteractionProposal.Status.REJECTED, reviewed_by=user, reviewed_at=now, review_note=note)
            return decision, None, len(ids)

        if decision == COVERED:
            row = (PathogenInteraction.objects.filter(beetle_host_id=beetle_id, pathogen__iexact=best.partner_name)
                   .order_by("record_number", "created_at").first())
            if row is None:
                raise ReviewError("The published dataset has no record of that beetle and partner.")
        else:
            partner = " ".join((partner_name or best.partner_name).split())
            category = (category or best.category).strip()
            relationship = (relationship or best.relationship).strip()
            if not partner or len(partner) > 255 or len(category) > 64 or len(relationship) > 128:
                raise ReviewError("The partner must be given (255 characters at most), the category 64 and the relationship 128.")
            row = PathogenInteraction.objects.create(
                origin=PathogenInteraction.Origin.PROPOSAL, added_by=user,
                beetle_host=best.beetle_name, beetle_host_id=best.beetle_valid_species_id,
                pathogen=partner, category=category or "Unknown", ecological_relationship=relationship or None,
                year=str(best.source_year or ""),
                source=citation_for(best.source_authors, best.source_year, best.source_journal, best.source_title) or None,
                title=best.source_title or None,
                doi_or_full_text=(f"https://doi.org/{best.source_doi}" if best.source_doi else safe_url(best.source_url)) or None,
                full_text_status="abstract only" if best.evidence_location in ("abstract", "title") else None,
                validation_type="expert review of a literature proposal",
            )
        updates.update(status=InteractionProposal.Status.ACCEPTED, reviewed_by=user, reviewed_at=now,
                       review_note=note, published_as=row)
        return decision, row, len(ids)


def additions_as_records():
    """Published rows that are not part of the v1.0 dataset, in the shape of bark_beetle_pathogens_master.json."""
    records = []
    rows = PathogenInteraction.objects.exclude(origin=PathogenInteraction.Origin.DATASET).order_by("created_at")
    for r in rows:
        records.append({
            "Records ID": f"A{str(r.id)[:8]}", "Beetle Host": r.beetle_host, "Beetle Host IDs": r.beetle_host_id or "",
            "categories": r.category, "pathogens": r.pathogen, "country or region": r.country_or_region or "",
            "ecological relationship": r.ecological_relationship or "", "experimental conditions": r.experimental_conditions or "",
            "identification method": r.identification_method or "", "infection site": r.infection_site or "",
            "organism source": r.organism_source or "", "validation type": r.validation_type or "",
            "source": r.source or "", "year": int(r.year) if (r.year or "").isdigit() else (r.year or ""),
            "full text": r.full_text_status or "", "title": r.title or "", "doi or full text": r.doi_or_full_text or "",
            "origin": r.origin,
        })
    return records
