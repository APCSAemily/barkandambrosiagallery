"""
Downloads of the interactions, built from the database. Viewing the page is free; these need an account.

    dataset.csv       every record (the columns of the published master file)
    validated.csv     the records with an experimental validation
    fungi.csv ...     one pathogen group (fungi, nematodes, microsporidia, protists, bacteria, viruses)
    hosts.csv         one line per beetle
    references.csv    the publications cited
    workbook.xlsx     all of the above in one Excel file (dataset, beetle hosts, references)
"""
import csv
import io

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse
from django.utils import timezone
from django.views.decorators.http import require_GET

from . import interaction_data as data

RECORD_COLUMNS = [
    "Records ID", "Beetle Host", "Beetle Host IDs", "categories", "pathogens", "country or region", "ecological relationship",
    "experimental conditions", "identification method", "infection site", "organism source", "validation type", "source",
    "year", "full text", "title",
]
HOST_COLUMNS = [
    "Beetle Host ID", "Beetle Species", "No. records", "No. pathogen taxa", "No. countries", "Associated Pathogens",
    "Associated Countries", "Pathogen Record IDs",
]
REFERENCE_COLUMNS = ["Reference No.", "Citation"]
GROUPS = {
    "fungi": "Fungi", "nematodes": "Nematode", "microsporidia": "Microsporidia", "protists": "Protist",
    "bacteria": "Bacteria", "viruses": "Virus",
}


def tables():
    """{file name without extension: (columns, rows)}, each built from the database once."""
    records = data.master_records()
    out = {
        "dataset": (RECORD_COLUMNS, records),
        "validated": (RECORD_COLUMNS, [r for r in records if r["validation type"]]),
        "hosts": (HOST_COLUMNS, data.hosts_summary()),
        "references": (REFERENCE_COLUMNS, data.references()),
    }
    for slug, category in GROUPS.items():
        out[slug] = (RECORD_COLUMNS, [r for r in records if r["categories"] == category])
    return out


def _csv(columns, rows):
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\r\n")
    writer.writerow(columns)
    for row in rows:
        writer.writerow(["" if row.get(c) is None else row.get(c) for c in columns])
    return "﻿" + out.getvalue()   # the byte order mark lets Excel read the accents


def _workbook(all_tables):
    import xlsxwriter

    buffer = io.BytesIO()
    book = xlsxwriter.Workbook(buffer, {"in_memory": True})
    for sheet, key in (("Dataset", "dataset"), ("Beetle Hosts", "hosts"), ("References", "references")):
        columns, rows = all_tables[key]
        ws = book.add_worksheet(sheet)
        ws.write_row(0, 0, columns, book.add_format({"bold": True}))
        for i, row in enumerate(rows, start=1):
            ws.write_row(i, 0, ["" if row.get(c) is None else row.get(c) for c in columns])
        ws.freeze_panes(1, 0)
    book.close()
    return buffer.getvalue()


@login_required
@require_GET
def interactions_download(request, name, ext):
    all_tables = tables()
    stamp = timezone.localtime().strftime("%Y%m%d")
    if name == "workbook" and ext == "xlsx":
        response = HttpResponse(_workbook(all_tables),
                                content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    elif ext == "csv" and name in all_tables:
        columns, rows = all_tables[name]
        response = HttpResponse(_csv(columns, rows), content_type="text/csv; charset=utf-8")
    else:
        raise Http404("No such download")
    filename = f"beetle_interactions_{name}_{stamp}.{ext}"
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    return response
