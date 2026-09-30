"""
The interactions page, from the database.

Everything the page shows is built here from the PathogenInteraction table: the records, the per-beetle summary,
the reference list and the headline numbers. The published v1.0 dataset is loaded into that table once
(``manage.py import_pathogen_interactions``, run on every deploy); after that the table is the source, so rows added
on the review page or by CSV upload, and corrections made by CSV update, appear on the page straight away.
"""
from collections import Counter, defaultdict

from .models import PathogenInteraction

UNKNOWN_PATHOGEN = "Unknown Pathogen"
UNKNOWN_CATEGORY = "Unknown"
GROUP_LABELS = {
    "Fungi": "Fungi", "Nematode": "Nematodes", "Protist": "Protists", "Microsporidia": "Microsporidia",
    "Bacteria": "Bacteria", "Virus": "Viruses",
}


def _number(value):
    """A record number as a number when it is one, so they sort 2, 10, 100 and not 10, 100, 2."""
    text = (value or "").strip()
    return int(text) if text.isdigit() else None


def _year(value):
    text = (value or "").strip()
    return int(text) if text.isdigit() else (text or None)


def _rows():
    """Every interaction, published dataset first in its own numbering, then the rest as they were added."""
    rows = list(PathogenInteraction.objects.all())

    def order(r):
        number = _number(r.record_number)
        return (r.origin != PathogenInteraction.Origin.DATASET, number if number is not None else 10 ** 9, r.created_at, str(r.id))

    rows.sort(key=order)
    return rows


def _stated(value, placeholder):
    """The importer stores "Unknown ..." where the published file has nothing; the page shows nothing, as it always did."""
    return None if value == placeholder else value


def display_id(row):
    """The number shown for a record: the dataset's own number, or A plus the start of the id for later additions."""
    number = _number(row.record_number)
    if number is not None:
        return number
    return row.record_number or f"A{str(row.id)[:8]}"


def master_records(rows=None):
    """The records in the shape the page's table reads (the columns of the published master file)."""
    records = []
    for r in rows if rows is not None else _rows():
        records.append({
            "Records ID": display_id(r), "Beetle Host": _stated(r.beetle_host, "Unknown Host"),
            "Beetle Host IDs": r.beetle_host_id or "",
            "categories": _stated(r.category, UNKNOWN_CATEGORY), "pathogens": _stated(r.pathogen, UNKNOWN_PATHOGEN), "country or region": r.country_or_region,
            "ecological relationship": r.ecological_relationship, "experimental conditions": r.experimental_conditions,
            "identification method": r.identification_method, "infection site": r.infection_site,
            "organism source": r.organism_source, "validation type": r.validation_type, "source": r.source,
            "year": _year(r.year), "full text": r.full_text_status, "title": r.title,
            "doi or full text": r.doi_or_full_text, "origin": r.origin,
        })
    return records


def hosts_summary(rows=None):
    """One line per beetle: how many records, how many pathogens and countries, most recorded first."""
    groups = defaultdict(list)
    for r in rows if rows is not None else _rows():
        groups[r.beetle_host_id or r.beetle_host].append(r)
    summary = []
    for members in groups.values():
        pathogens = sorted({m.pathogen for m in members if m.pathogen and m.pathogen != UNKNOWN_PATHOGEN})
        countries = sorted({m.country_or_region for m in members if m.country_or_region})
        summary.append({
            "Beetle Host ID": next((m.beetle_host_id for m in members if m.beetle_host_id), ""),
            "Beetle Species": members[0].beetle_host,
            "No. records": len(members), "No. pathogen taxa": len(pathogens), "No. countries": len(countries),
            "Associated Pathogens": "; ".join(pathogens), "Associated Countries": "; ".join(countries),
            "Pathogen Record IDs": ", ".join(str(display_id(m)) for m in members),
        })
    summary.sort(key=lambda s: (-s["No. records"], s["Beetle Species"]))
    return summary


def references(rows=None):
    """The publications the records cite, alphabetically and numbered."""
    titles = sorted({(r.title or "").strip() for r in (rows if rows is not None else _rows())} - {""})
    return [{"Citation": title, "Reference No.": n} for n, title in enumerate(titles, start=1)]


def group_stats(rows=None):
    """[(category, label, records, taxa)] for the category filter, the most recorded first."""
    records, taxa = Counter(), defaultdict(set)
    for r in rows if rows is not None else _rows():
        if r.category == UNKNOWN_CATEGORY:
            continue   # records the paper did not sort into a group are in "all", not in a group of their own
        records[r.category] += 1
        if r.pathogen and r.pathogen != UNKNOWN_PATHOGEN:
            taxa[r.category].add(r.pathogen.strip())
    return [(c, GROUP_LABELS.get(c, c), n, len(taxa[c])) for c, n in sorted(records.items(), key=lambda kv: (-kv[1], kv[0]))]


def page_stats(rows=None):
    """The headline numbers, as numbers (the template formats them)."""
    rows = list(rows if rows is not None else _rows())
    hosts = {r.beetle_host for r in rows}
    years = [y for y in (_number(r.year) for r in rows) if y]
    validated = sum(1 for r in rows if r.validation_type)
    return {
        "records": len(rows),
        "hosts": len(hosts),
        "genera": len({h.split()[0] for h in hosts if h.split()}),
        "taxa": len({r.pathogen.strip() for r in rows if r.pathogen and r.pathogen != UNKNOWN_PATHOGEN}),
        "sources": len({(r.title or "").strip() for r in rows} - {""}),
        "validated": validated,
        "validated_pct": round(100 * validated / len(rows), 1) if rows else 0,
        "first_year": min(years) if years else None,
        "last_year": max(years) if years else None,
        "groups": len({r.category for r in rows} - {UNKNOWN_CATEGORY}),
    }
