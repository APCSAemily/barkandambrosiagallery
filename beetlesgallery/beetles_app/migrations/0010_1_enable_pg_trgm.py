from django.contrib.postgres.operations import TrigramExtension
from django.db import migrations


class Migration(migrations.Migration):
    """Enable the Postgres pg_trgm extension.

    Migration 0011 builds GIN indexes with opclasses=['gin_trgm_ops'], which
    require this extension. Nothing previously created it, so a clean database
    (including Django's test database) failed on 0011 with:
        operator class "gin_trgm_ops" does not exist for access method "gin"
    This is ordered before 0011 via that migration's `dependencies`.
    pg_trgm is a Postgres "trusted" extension (since PG13), so this does not
    require superuser -- the database owner can create it.
    """

    dependencies = [
        ('beetles_app', '0010_categorymapping_taxon_synonym_beetles_taxon_and_more'),
    ]

    operations = [
        TrigramExtension(),
    ]
